"""Made-to-order goods (`custom_order` modules): cakes, flowers, catering trays.

The fulfilment day and the module's date-typed intake field (the bakery's `pickup_date`) are the same
fact: the `day` argument fills that field when the customer has not answered it, and wins when both
are given, so the read-back and the stored commitment can never disagree. A module with no date field
gets a synthetic required `date` field in `missing`, which the tools layer asks like any other.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from shopfront_server.domain.approval_rules import evaluate_approval
from shopfront_server.domain.idempotency import idempotency_key
from shopfront_server.domain.intake import field_specs, is_blank, nonce_payload, normalize_intake
from shopfront_server.domain.models import CapacityHold
from shopfront_server.domain.planning import CommitPlan, IntakeFieldSpec, PlanProblem, SlotOption
from shopfront_server.domain.pricing import price_for
from shopfront_server.domain.services.base import (
    CommitmentServiceBase,
    approval_facts,
    build_read_back,
    choice_refs,
    dedupe,
    first_module_id,
    group_answers,
    minutes_until,
    owner_module_for,
    parse_day,
    pick_module,
    price_builtin,
    raise_if_disputed,
    rejected_plan,
    resolve_choices,
    resolve_item,
    say_before_confirm,
    with_disputes,
)
from shopfront_server.errors import UnknownItem, UnknownModule
from shopfront_server.profile.model import Profile

DATE_FIELD = IntakeFieldSpec(key="date", label="Date", type="date", required=True)
"""Asked when a custom_order module has no date-typed intake field of its own."""


def date_field(specs: tuple[IntakeFieldSpec, ...]) -> IntakeFieldSpec | None:
    """The module's fulfilment-date field: the first required date field, else the first date field."""
    dated = [spec for spec in specs if spec.type == "date"]
    return next((spec for spec in dated if spec.required), dated[0] if dated else None)


class CustomOrderService(CommitmentServiceBase):
    async def open_days(
        self,
        profile: Profile,
        *,
        item: str,
        module_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 6,
    ) -> list[SlotOption]:
        """Days a custom order for `item` can be fulfilled (lead time, max advance, closures, per-day capacity).

        Raises `errors.UnknownItem` (unknown item, or not custom-orderable), `errors.UnknownModule` or
        `errors.DisputedFact` (a rule the days depend on is disputed).
        """
        match = resolve_item(profile, item, module_type="custom_order", module_id=module_id)
        if match is None:
            raise UnknownItem("unknown item")
        picked = pick_module(profile, match.ref, "custom_order", module_id)
        if isinstance(picked, PlanProblem):
            raise UnknownModule(picked.detail) if picked.code == "unknown_module" else UnknownItem(picked.detail)
        raise_if_disputed(self.ctx.availability, profile, picked[0])
        days = await self.ctx.availability.open_days(
            profile, picked[0], self.ctx.capacity, date_from=date_from, date_to=date_to, limit=limit
        )
        return [slot for slot, _ in days]

    async def plan(
        self,
        profile: Profile,
        *,
        item: str,
        choices: Mapping[str, str],
        day: str | None,
        intake: Mapping[str, Any],
        principal: str,
        module_id: str | None = None,
        nonce: str | None = None,
    ) -> CommitPlan:
        """Plan a custom order.

        `choices` maps option group ids (or names/spoken names) to choice ids (or spoken names/synonyms);
        required groups must be answered (problem `missing_choice`). `day` is the fulfilment date (ISO);
        when absent it may come from an intake field of type date (e.g. `pickup_date`). Price comes from
        `pricing.price_for`; approval from `approval_rules.evaluate_approval` with built-ins.
        `nonce` (unauthenticated callers) only enters the key when the module collects no contact
        field (`intake.nonce_payload`).
        """
        now = self.ctx.clock.now()
        raw_payload = {"item": item, "choices": dict(choices or {}), "day": day}
        match = resolve_item(profile, item, module_type="custom_order", module_id=module_id)
        picked: tuple[str, Any] | PlanProblem
        if match is None:
            picked = PlanProblem(code="unknown_item", detail="no such item", field="item")
        else:
            picked = pick_module(profile, match.ref, "custom_order", module_id)
        if isinstance(picked, PlanProblem) or match is None:
            assert isinstance(picked, PlanProblem)
            return rejected_plan(
                profile,
                module_id=module_id or first_module_id(profile, "custom_order"),
                kind="custom_order",
                operation="custom_order",
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

        dated = date_field(specs)
        requested_day: date | None = None
        if day is not None and str(day).strip():
            requested_day = parse_day(day)
            if requested_day is None:
                problems.append(PlanProblem(code="invalid_intake", detail="expected a date like 2026-10-18", field="date"))
            elif dated is not None:
                raw[dated.key] = requested_day.isoformat()
        values, intake_problems, missing = normalize_intake(specs, raw, locale=profile.locale)
        problems.extend(intake_problems)
        if requested_day is None and dated is not None and values.get(dated.key):
            requested_day = parse_day(values[dated.key])
        if requested_day is None and dated is None and not is_blank(raw.get("date")):
            requested_day = parse_day(raw["date"])
            if requested_day is None:
                problems.append(PlanProblem(code="invalid_intake", detail="expected a date like 2026-10-18", field="date"))
        if requested_day is None and dated is None and not any(p.field == "date" for p in problems):
            missing.append(DATE_FIELD)

        holds: tuple[CapacityHold, ...] = ()
        day_refs: tuple[str, ...] = ()
        lead_minutes: int | None = None
        if requested_day is not None:
            checked = self.ctx.availability.check_day(profile, mid, requested_day)
            if isinstance(checked, PlanProblem):
                problems.append(await self.with_open_days(profile, mid, checked, requested_day))
            else:
                slot, holds = checked
                day_refs = slot.fact_refs
                if slot.window is not None:
                    lead_minutes = minutes_until(now, slot.window.start)
                full = await self.ctx.availability.is_free(holds, self.ctx.capacity)
                if full is not None:
                    problems.append(
                        PlanProblem(
                            code="capacity_full",
                            detail="that day is fully booked",
                            field="date",
                            fact_ref=f"{mid}#availability.capacity_per_day",
                            alternatives=await self.open_days_after(profile, mid, requested_day),
                        )
                    )

        price = price_for(profile, ref, choices=chosen)
        # A disputed price is never read back or stored (L6): the plan waits for the owner's answer.
        problems = with_disputes(profile, problems, [*(price.fact_refs if price else ()), *day_refs], field="price")
        selected = choice_refs(ref, match.item, chosen)
        owner = owner_module_for(profile, mid)
        when = requested_day.isoformat() if requested_day else None
        approval = evaluate_approval(
            mid,
            module,
            approval_facts(
                values, item_ref=ref, choice_refs=selected, when=when, price=price, lead_minutes=lead_minutes
            ),
            owner_module_id=owner[0] if owner else None,
        )
        key = idempotency_key(
            business_id=profile.profile_id,
            module_id=mid,
            operation="custom_order",
            principal=principal,
            payload={"item": ref, "choices": chosen, "day": when, "intake": values, **nonce_payload(specs, nonce)},
        )
        read_back = build_read_back(
            module,
            builtins={
                "item": (ref, [f"{ref}#spoken_name"]),
                "choices": (dict(chosen) or None, [f"{r}#spoken_name" for r in selected]),
                "when": (when, day_refs),
                "price": price_builtin(price),
                "order_total_cents": (price.amount_cents if price else None, price.fact_refs if price else ()),
            },
            intake=values,
            intake_refs={dated.key: day_refs} if dated is not None else None,
        )
        say = say_before_confirm(module)
        plan = CommitPlan(
            business_id=profile.profile_id,
            module_id=mid,
            kind="custom_order",
            operation="custom_order",
            principal=principal,
            profile_revision=profile.revision,
            idempotency_key=key,
            item_ref=ref,
            choices=chosen,
            day=requested_day,
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
                    *day_refs,
                    *approval.fact_refs,
                    *(r for item_ in read_back for r in item_.fact_refs),
                    *(f"{entry}#answer_spoken" for entry in say),
                ]
            ),
        )
        return await self.settle_replay(plan)
