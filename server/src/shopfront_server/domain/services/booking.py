"""Appointments, tables and classes (`booking` modules).

Conventions beyond the schema (all generic, none business-specific):
  * Option groups of a bookable item (e.g. a balayage's hair length) come from `choices`, or from
    `intake`/`details` keys equal to the group id, because the `book` tool has no `choices` argument.
  * A `choice` intake field whose options are the module's resource ids (the salon's `stylist`) is the
    resource preference; its other options ("any") mean no preference. The `resource` argument fills
    that field when the customer named someone, so it is not asked again.
  * `party_size` defaults to 1; it fills an intake field called `party_size` if the module has one.
  * The assigned resource never enters the idempotency key (only the requested one does), so a retry
    after the first attempt took the slot still replays instead of booking a second stylist. When the
    customer named nobody and the assigned resource is taken between plan and commit, `commit`
    reassigns the slot to the next free eligible resource before reporting it as taken.
  * Cancellation notice (`facts.notice_minutes` of the cancellation policy) is a fee window, not a
    ban: a late cancel is allowed after the policy (which states the fee) is read back. A request the
    owner has not accepted yet can always be withdrawn.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from shopfront_server.clock import utc_iso
from shopfront_server.domain.approval_rules import evaluate_approval
from shopfront_server.domain.availability import MAX_ALTERNATIVES, SlotWithHolds, fits
from shopfront_server.domain.idempotency import idempotency_key
from shopfront_server.domain.intake import field_specs, is_blank, nonce_payload, normalize_intake
from shopfront_server.domain.models import OPEN_STATUSES, ApprovalCheck, CapacityHold, Commitment, can_transition
from shopfront_server.domain.planning import CommitOutcome, CommitPlan, PlanProblem, ReadBackItem, SlotOption
from shopfront_server.domain.pricing import price_for
from shopfront_server.domain.services.base import (
    CommitmentServiceBase,
    approval_facts,
    bound_field,
    build_read_back,
    choice_refs,
    dedupe,
    first_module_id,
    group_answers,
    match_entity,
    minutes_until,
    normalize_name,
    owner_module_for,
    pick_module,
    price_builtin,
    raise_if_disputed,
    rejected_plan,
    resolve_choices,
    resolve_item,
    say_before_confirm,
    with_disputes,
)
from shopfront_server.errors import ConcurrencyConflict, InvalidState, NotFound, UnknownItem, UnknownModule
from shopfront_server.profile.model import JsonObj, Profile

NO_PREFERENCE: frozenset[str] = frozenset(
    {"any", "anyone", "no preference", "first available", "whoever", "whoever is free", "either", "none"}
)
"""Normalised answers that mean 'no particular resource'."""

_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CANCEL_ATTEMPTS = 3
_REASSIGN_ATTEMPTS = 3
"""How many other resources a lost race may try before the slot is reported as taken."""


def parse_start(text: str | None, tz: ZoneInfo) -> tuple[datetime | None, PlanProblem | None, date | None]:
    """Parse a requested start: local ISO (business timezone, `fold=0`) or ISO with an offset.

    Returns `(start, problem, hint_day)`; `hint_day` is the date of a date-only request, so the
    alternatives can be offered on the day the customer asked for.
    """
    if text is None or not text.strip():
        return None, PlanProblem(code="not_on_schedule", detail="pick a start time", field="start"), None
    cleaned = text.strip()
    if _DATE_ONLY.match(cleaned):
        try:
            day = date.fromisoformat(cleaned)
        except ValueError:
            day = None
        return None, PlanProblem(code="not_on_schedule", detail="pick a time on that day", field="start"), day
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError:
        problem = PlanProblem(
            code="invalid_intake", detail="expected a local date and time like 2026-10-17T10:00", field="start"
        )
        return None, problem, None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz, fold=0), None, None
    return parsed.astimezone(tz), None, None


def cancellation_policy(profile: Profile, module_id: str, item_ref: str | None) -> tuple[str, JsonObj] | None:
    """The knowledge entry that states the cancellation policy for a module (or item): prefers topic
    `cancellation` with `facts.notice_minutes`. Returns `(entry ref, entry)`."""
    best: tuple[tuple[bool, bool], str, JsonObj] | None = None
    for knowledge_id, entry in profile.knowledge_entries():
        applies = set(entry.get("applies_to") or ())
        if module_id not in applies and (item_ref is None or item_ref not in applies):
            continue
        score = (entry.get("topic") == "cancellation", "notice_minutes" in (entry.get("facts") or {}))
        if any(score) and (best is None or score > best[0]):
            best = (score, f"{knowledge_id}/{entry['id']}", entry)
    return (best[1], best[2]) if best is not None else None


class BookingService(CommitmentServiceBase):
    async def open_slots(
        self,
        profile: Profile,
        *,
        item: str,
        module_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        party_size: int = 1,
        resource_id: str | None = None,
        limit: int = 6,
        choices: Mapping[str, str] | None = None,
    ) -> list[SlotOption]:
        """Open slots for an item (module inferred from the item when `module_id` is None).

        Raises `errors.UnknownItem` (unknown item, or not bookable), `errors.UnknownModule`,
        `errors.NotFound` (unknown resource) or `errors.DisputedFact` (a rule the slots depend on is
        disputed). Unanswered option groups are tolerated: slots are sized for the longest choice.
        """
        match = resolve_item(profile, item, module_type="booking", module_id=module_id)
        if match is None:
            raise UnknownItem("unknown item")
        picked = pick_module(profile, match.ref, "booking", module_id)
        if isinstance(picked, PlanProblem):
            raise UnknownModule(picked.detail) if picked.code == "unknown_module" else UnknownItem(picked.detail)
        mid, module = picked
        preferred: str | None = None
        if resource_id and normalize_name(resource_id) not in NO_PREFERENCE:
            resource = _match_resource(module, resource_id)
            if resource is None:
                raise NotFound("unknown resource")
            preferred = resource["id"]
        chosen, _ = resolve_choices(match.ref, match.item, choices or {})
        raise_if_disputed(self.ctx.availability, profile, mid, match.ref, chosen)
        slots = await self.ctx.availability.open_booking_slots(
            profile,
            mid,
            match.ref,
            self.ctx.capacity,
            date_from=date_from,
            date_to=date_to,
            party_size=party_size,
            resource_id=preferred,
            limit=limit,
            choices=chosen,
        )
        return [slot for slot, _ in slots]

    async def plan(
        self,
        profile: Profile,
        *,
        item: str,
        start: str | None,
        party_size: int | None,
        intake: Mapping[str, Any],
        principal: str,
        module_id: str | None = None,
        resource_id: str | None = None,
        choices: Mapping[str, str] | None = None,
        nonce: str | None = None,
    ) -> CommitPlan:
        """Plan a booking.

        `start` is ISO-8601 local time (`2026-10-17T10:00`, interpreted in the business timezone) or
        with an explicit offset. A missing or invalid start yields a problem with up to 3 alternative
        open slots. `party_size` defaults to 1 and must fit the service's `party_size` range.
        Capacity is checked against current usage (advisory) and the holds are attached for commit.
        `nonce` (unauthenticated callers) only enters the key when the module collects no contact
        field (`intake.nonce_payload`).
        """
        now = self.ctx.clock.now()
        tz = profile.timezone
        raw_payload = {"item": item, "start": start, "party_size": party_size, "resource": resource_id}
        match = resolve_item(profile, item, module_type="booking", module_id=module_id)
        if match is None:
            return rejected_plan(
                profile,
                module_id=module_id or first_module_id(profile, "booking"),
                kind="booking",
                operation="book",
                principal=principal,
                problem=PlanProblem(code="unknown_item", detail="no such service", field="item"),
                payload=raw_payload,
            )
        picked = pick_module(profile, match.ref, "booking", module_id)
        if isinstance(picked, PlanProblem):
            return rejected_plan(
                profile,
                module_id=module_id or first_module_id(profile, "booking"),
                kind="booking",
                operation="book",
                principal=principal,
                problem=picked,
                payload=raw_payload,
            )
        mid, module = picked
        ref = match.ref
        specs = field_specs(profile.intake_fields(mid))
        raw = dict(intake)
        problems: list[PlanProblem] = []

        chosen, choice_problems = resolve_choices(ref, match.item, {**group_answers(match.item, raw, specs), **(choices or {})})
        problems.extend(choice_problems)

        resources = list(module.get("resources", ()) or ())
        resource_field = bound_field(specs, {r["id"] for r in resources})
        preferred: str | None = None
        if resource_id and normalize_name(resource_id) not in NO_PREFERENCE:
            resource = _match_resource(module, resource_id)
            if resource is None:
                problems.append(PlanProblem(code="not_offered", detail="nobody by that name here", field="resource"))
            else:
                preferred = resource["id"]
                if resource_field is not None and is_blank(raw.get(resource_field.key)):
                    raw[resource_field.key] = preferred
        party_field = next((spec for spec in specs if spec.key == "party_size"), None)
        if party_size is None and party_field is not None and not is_blank(raw.get("party_size")):
            party_size = _int_or_none(raw["party_size"])
        if party_size is not None and party_field is not None and is_blank(raw.get("party_size")):
            raw["party_size"] = party_size
        party = party_size if party_size is not None else 1

        values, intake_problems, missing = normalize_intake(specs, raw, locale=profile.locale)
        problems.extend(intake_problems)
        if preferred is None and resource_field is not None and values.get(resource_field.key) in {r["id"] for r in resources}:
            preferred = str(values[resource_field.key])

        start_dt, start_problem, hint_day = parse_start(start, tz)
        window = None
        holds: tuple[CapacityHold, ...] = ()
        assigned: str | None = None
        slot_refs: tuple[str, ...] = ()
        if start_problem is not None:
            after = datetime.combine(hint_day, datetime.min.time(), tzinfo=tz) if hint_day else now
            problems.append(
                start_problem.model_copy(
                    update={"alternatives": await self._alternatives(profile, mid, ref, max(after, now), party, preferred, chosen)}
                )
            )
        else:
            assert start_dt is not None
            options = self.ctx.availability.booking_options(
                profile, mid, ref, start_dt, party_size=party, resource_id=preferred, choices=chosen
            )
            if isinstance(options, PlanProblem):
                problems.append(await self._capacity_aware(profile, options, mid, ref, start_dt, now, party, preferred, chosen))
            else:
                slot, holds, free = await self._pick_free(options)
                window, assigned, slot_refs = slot.window, slot.resource_id, slot.fact_refs
                if not free:
                    alternatives = await self._alternatives(profile, mid, ref, start_dt, party, preferred, chosen, exclude=start_dt)
                    problems.append(
                        PlanProblem(
                            code="capacity_full", detail="that time is already taken", field="start", alternatives=alternatives
                        )
                    )

        price = price_for(profile, ref, choices=chosen, party_size=party)
        problems = with_disputes(profile, problems, [*(price.fact_refs if price else ()), *slot_refs])
        when_local = window.start.isoformat() if window else (start_dt.isoformat() if start_dt else None)
        selected = choice_refs(ref, match.item, chosen)
        owner = owner_module_for(profile, mid)
        approval = evaluate_approval(
            mid,
            module,
            approval_facts(
                values,
                item_ref=ref,
                choice_refs=selected,
                when=when_local,
                price=price,
                party_size=party,
                lead_minutes=minutes_until(now, start_dt) if start_dt else None,
            ),
            owner_module_id=owner[0] if owner else None,
        )
        key = idempotency_key(
            business_id=profile.profile_id,
            module_id=mid,
            operation="book",
            principal=principal,
            payload={
                "item": ref,
                "choices": chosen,
                "start": utc_iso(start_dt) if start_dt else start,
                "party_size": party,
                "resource": preferred,
                "intake": values,
                **nonce_payload(specs, nonce),
            },
        )
        resource_ids = {r["id"] for r in resources}
        intake_refs = {
            spec.key: [f"{mid}/{values[spec.key]}#spoken_name"]
            for spec in specs
            if spec.type == "choice" and values.get(spec.key) in resource_ids
        }
        read_back = build_read_back(
            module,
            builtins={
                "item": (ref, [f"{ref}#spoken_name"]),
                "choices": (dict(chosen) or None, [f"{r}#spoken_name" for r in selected]),
                "when": (when_local, slot_refs),
                "price": price_builtin(price),
                "order_total_cents": (price.amount_cents if price else None, price.fact_refs if price else ()),
                "party_size": (party, ()),
            },
            intake=values,
            intake_refs=intake_refs,
        )
        say = say_before_confirm(module)
        plan = CommitPlan(
            business_id=profile.profile_id,
            module_id=mid,
            kind="booking",
            operation="book",
            principal=principal,
            profile_revision=profile.revision,
            idempotency_key=key,
            item_ref=ref,
            choices=chosen,
            window=window,
            day=window.start.date() if window else None,
            party_size=party,
            resource_id=assigned,
            resource_requested=preferred is not None,
            intake=values,
            missing=tuple(missing),
            price=price,
            approval=approval,
            holds=holds,
            read_back=read_back,
            say_before_confirm=say,
            problems=tuple(problems),
            fact_refs=dedupe(
                [
                    f"{ref}#spoken_name",
                    *(price.fact_refs if price else ()),
                    *slot_refs,
                    *approval.fact_refs,
                    *(ref_ for item_ in read_back for ref_ in item_.fact_refs),
                    *(f"{entry}#answer_spoken" for entry in say),
                ]
            ),
        )
        return await self.settle_replay(plan)

    async def plan_cancel(self, profile: Profile, *, booking_id: str, principal: str) -> CommitPlan:
        """Plan cancelling a booking (read-back of what is cancelled + the cancellation policy entry).

        Problems: unknown booking or not owned by `principal` (`unknown_item`, field `reference`), a
        status that cannot be cancelled (`not_offered`) or a start already past (`in_the_past`). Less
        notice than the cancellation policy's `facts.notice_minutes` is not a problem: the policy
        (which states any late fee) is read back before the customer confirms, and a request the owner
        has not accepted yet carries no fee at all. An already-cancelled booking is NOT a problem
        either: the plan stays ok so that re-cancelling replays (`cancel` returns it unchanged).

        A `pending_owner` booking can be cancelled too; the caller must then also cancel its task.
        """
        now = self.ctx.clock.now()
        found = await self.ctx.commitments.get(profile.profile_id, booking_id)
        if found is None or found.principal != principal or found.kind != "booking":
            return rejected_plan(
                profile,
                module_id=found.module_id if found else first_module_id(profile, "booking"),
                kind="booking",
                operation="cancel_booking",
                principal=principal,
                problem=PlanProblem(code="unknown_item", detail="no booking with that reference", field="reference"),
                payload={"commitment_id": booking_id},
            )
        problems: list[PlanProblem] = []
        policy = cancellation_policy(profile, found.module_id, found.item_ref)
        start = found.window.start if found.window else None
        if found.status != "cancelled":
            if not can_transition(found.status, "cancelled"):
                problems.append(
                    PlanProblem(code="not_offered", detail=f"the booking is {found.status}; nothing to cancel", field="reference")
                )
            elif start is not None and start <= now:
                problems.append(PlanProblem(code="in_the_past", detail="the appointment has already started", field="reference"))
        read_back: list[ReadBackItem] = []
        if found.item_ref:
            read_back.append(ReadBackItem(key="item", value=found.item_ref, fact_refs=(f"{found.item_ref}#spoken_name",)))
        if found.choices:
            read_back.append(ReadBackItem(key="choices", value=dict(found.choices)))
        if start is not None:
            read_back.append(ReadBackItem(key="when", value=start.isoformat()))
        # The policy is read back for accepted bookings (it states any late fee); a request the owner
        # has not accepted yet is simply withdrawn.
        say = (policy[0],) if policy and found.status not in OPEN_STATUSES else ()
        key = idempotency_key(
            business_id=profile.profile_id,
            module_id=found.module_id,
            operation="cancel_booking",
            principal=principal,
            payload={"commitment_id": booking_id},
        )
        return CommitPlan(
            business_id=profile.profile_id,
            module_id=found.module_id,
            kind="booking",
            operation="cancel_booking",
            principal=principal,
            profile_revision=profile.revision,
            idempotency_key=key,
            item_ref=found.item_ref,
            choices=dict(found.choices),
            window=found.window,
            day=found.day,
            party_size=found.party_size,
            resource_id=found.resource_id,
            approval=ApprovalCheck(required=False),
            read_back=tuple(read_back),
            say_before_confirm=say,
            problems=tuple(problems),
            fact_refs=dedupe(
                [
                    *(r for item_ in read_back for r in item_.fact_refs),
                    *(f"{entry}#answer_spoken" for entry in say),
                    *(p.fact_ref for p in problems),
                ]
            ),
        )

    async def cancel(self, profile: Profile, *, booking_id: str, principal: str) -> Commitment:
        """Transition to `cancelled` (releasing holds atomically). Idempotent: cancelling a cancelled
        booking returns it unchanged. Raises `errors.NotFound` (unknown or foreign booking) and
        `errors.InvalidState` (a status that cannot be cancelled)."""
        for _ in range(_CANCEL_ATTEMPTS):
            found = await self.ctx.commitments.get(profile.profile_id, booking_id)
            if found is None or found.principal != principal or found.kind != "booking":
                raise NotFound("unknown booking")
            if found.status == "cancelled":
                return found
            if not can_transition(found.status, "cancelled"):
                raise InvalidState(f"a {found.status} booking cannot be cancelled")
            updated = found.model_copy(
                update={"status": "cancelled", "updated_at": self.ctx.clock.now(), "expires_at": None}
            )
            try:
                return await self.ctx.commitments.update(updated, expected_version=found.version)
            except ConcurrencyConflict:
                continue
        raise ConcurrencyConflict("the booking changed while cancelling; try again")

    async def commit(self, profile: Profile, plan: CommitPlan, *, task_id: str | None) -> CommitOutcome:
        """`CommitmentServiceBase.commit`, plus reassignment after a lost race.

        The plan assigned the first free resource from an advisory read. If a concurrent commit took
        that resource's cells and the customer did not ask for anyone in particular, the next free
        eligible resource at the same start is tried (the idempotency key does not include the
        assigned resource, so a retry still replays whichever one won)."""
        outcome = await super().commit(profile, plan, task_id=task_id)
        tried = {plan.resource_id}
        for _ in range(_REASSIGN_ATTEMPTS):
            if not _reassignable(plan, outcome):
                break
            assert plan.window is not None and plan.item_ref is not None
            options = self.ctx.availability.booking_options(
                profile,
                plan.module_id,
                plan.item_ref,
                plan.window.start.astimezone(profile.timezone),
                party_size=plan.party_size or 1,
                choices=plan.choices,
            )
            if isinstance(options, PlanProblem):
                break
            others = [(slot, holds) for slot, holds in options if slot.resource_id not in tried]
            if not others:
                break
            slot, holds, free = await self._pick_free(others)
            if not free:
                break
            tried.add(slot.resource_id)
            plan = plan.model_copy(update={"resource_id": slot.resource_id, "holds": holds})
            outcome = await super().commit(profile, plan, task_id=task_id)
        return outcome

    # ---- internals ---------------------------------------------------------------------------

    async def _pick_free(self, options: list[SlotWithHolds]) -> tuple[SlotOption, tuple[CapacityHold, ...], bool]:
        """The first option whose holds fit current usage (one batch read); else the first option."""
        usage = await self.ctx.capacity.usage(sorted({hold.key for _, holds in options for hold in holds}))
        for slot, holds in options:
            if fits(holds, usage):
                return slot, holds, True
        return options[0][0], options[0][1], False

    async def _alternatives(
        self,
        profile: Profile,
        module_id: str,
        item_ref: str,
        after: datetime,
        party: int,
        preferred: str | None,
        chosen: Mapping[str, str],
        *,
        exclude: datetime | None = None,
    ) -> tuple[SlotOption, ...]:
        slots = await self.ctx.availability.open_booking_slots(
            profile,
            module_id,
            item_ref,
            self.ctx.capacity,
            date_from=after.astimezone(profile.timezone).date(),
            party_size=party,
            resource_id=preferred,
            limit=MAX_ALTERNATIVES + 1,
            choices=chosen,
            not_before=after,
        )
        return tuple(s for s, _ in slots if s.window is None or s.window.start != exclude)[:MAX_ALTERNATIVES]

    async def _capacity_aware(
        self,
        profile: Profile,
        problem: PlanProblem,
        module_id: str,
        item_ref: str,
        start: datetime,
        now: datetime,
        party: int,
        preferred: str | None,
        chosen: Mapping[str, str],
    ) -> PlanProblem:
        """Replace a rule problem's alternatives with ones that are free right now."""
        if problem.code in ("party_size_out_of_range", "not_offered", "unknown_item", "unknown_module", "disputed_fact"):
            return problem
        after = now if problem.code in ("in_the_past", "too_far_ahead") else max(start, now)
        alternatives = await self._alternatives(profile, module_id, item_ref, after, party, preferred, chosen)
        return problem.model_copy(update={"alternatives": alternatives})


def _reassignable(plan: CommitPlan, outcome: CommitOutcome) -> bool:
    """A booking that lost its auto-assigned resource to a race (not a resource the customer named)."""
    return (
        outcome.commitment is None
        and outcome.problem is not None
        and outcome.problem.code == "capacity_full"
        and plan.operation == "book"
        and not plan.resource_requested
        and plan.resource_id is not None
        and plan.window is not None
        and plan.item_ref is not None
    )


def _match_resource(module: JsonObj, text: str) -> JsonObj | None:
    return match_entity(list(module.get("resources", ()) or ()), text, "full_name")


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except ValueError:
        return None
    return number if number >= 1 else None

