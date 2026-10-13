"""Shared plumbing for the commitment services.

* `DomainContext`: the collaborators every service needs.
* Resolution helpers: items, transactional modules, option choices, resources and visit windows by id,
  name, spoken name or synonym (case-, accent- and punctuation-insensitive), so a voice host can pass
  what the customer said.
* Plan helpers: read-back, approval facts, rejected plans.
* `CommitmentServiceBase`: the one commit path (and replay) every commitment kind goes through.

Determinism: every helper here is a pure function of its arguments (and the injected clock), so a plan
rebuilt on a later MRTR round from the same arguments is identical.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import JsonValue

from shopfront_server.clock import Clock
from shopfront_server.domain.availability import MAX_ALTERNATIVES, AvailabilityEngine
from shopfront_server.domain.disputes import disputed_problem
from shopfront_server.domain.idempotency import MAX_GENERATIONS, generation_key, idempotency_key, new_record
from shopfront_server.domain.intake import is_blank
from shopfront_server.domain.models import (
    SETTLED_STATUSES,
    Commitment,
    CommitmentKind,
    PriceQuote,
)
from shopfront_server.domain.planning import (
    CommitOutcome,
    CommitPlan,
    IntakeFieldSpec,
    Operation,
    PlanProblem,
    ReadBackItem,
    SlotOption,
)
from shopfront_server.domain.ports import (
    CapacityExhausted,
    CapacityStore,
    CommitmentStore,
    Committed,
    Duplicate,
    IdempotencyStore,
    OwnerNotifier,
)
from shopfront_server.domain.pricing import selected_choices
from shopfront_server.errors import DisputedFact, NotFound, ShopfrontError, UnknownItem, UnknownModule
from shopfront_server.observability import id_digest
from shopfront_server.ids import IdFactory
from shopfront_server.profile.model import JsonObj, Profile

log = logging.getLogger("shopfront.domain")

OWNER_KINDS: frozenset[str] = frozenset({"quote_request", "owner_question"})
"""Kinds that always wait for the owner, whatever the approval block says."""

TOO_MANY_ATTEMPTS = PlanProblem(
    code="too_many_attempts", detail="this exact request was already made and settled too many times"
)
"""`with_generation`'s problem once all `MAX_GENERATIONS` attempts at one request are settled."""

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_APOSTROPHES = re.compile(r"['\u2019\u02bc]")


@dataclass(frozen=True)
class DomainContext:
    """Collaborators every service needs. Built once by `app.wiring`."""

    commitments: CommitmentStore
    capacity: CapacityStore
    idempotency: IdempotencyStore
    availability: AvailabilityEngine
    ids: IdFactory
    clock: Clock
    notifier: OwnerNotifier
    idempotency_ttl: timedelta = timedelta(hours=24)
    keep_waiting_max: timedelta = timedelta(days=30)
    """The longest a request under `on_expiry: keep_waiting` waits for the owner (`keep_waiting_horizon`)."""


# ---------------------------------------------------------------------------- name matching


def normalize_name(text: str) -> str:
    """Case-, accent- and punctuation-insensitive form: `"Inés"` -> `"ines"`, `"10-inch"` -> `"10 inch"`,
    `"Women's cut & style"` -> `"womens cut and style"`."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = _APOSTROPHES.sub("", stripped.casefold()).replace("&", " and ")
    return _NON_ALNUM.sub(" ", folded).strip()


def entity_names(entity: JsonObj, *extra_fields: str) -> set[str]:
    """Normalised id, name, spoken name, synonyms (and any `extra_fields`) of a profile entity."""
    values: list[Any] = [entity.get("id"), entity.get("name"), entity.get("spoken_name"), *(entity.get("synonyms") or ())]
    attributes = entity.get("attributes") or {}
    for name in extra_fields:
        values.append(entity.get(name) if name in entity else attributes.get(name))
    return {normalize_name(str(value)) for value in values if isinstance(value, str) and value.strip()}


def name_matches(entity: JsonObj, text: str, *extra_fields: str) -> bool:
    """True if `text` names the entity (a trailing plural `s` is forgiven)."""
    wanted = normalize_name(text)
    if not wanted:
        return False
    names = entity_names(entity, *extra_fields)
    return wanted in names or (wanted.endswith("s") and wanted[:-1] in names)


def dedupe(refs: Iterable[str | None]) -> tuple[str, ...]:
    """Drop empties and duplicates, keeping first-seen order (deterministic fact-ref lists)."""
    return tuple(dict.fromkeys(ref for ref in refs if ref))


# ---------------------------------------------------------------------------- items & modules


@dataclass(frozen=True)
class ItemMatch:
    catalog_module_id: str
    item: JsonObj
    ref: str


def resolve_item(
    profile: Profile, text: str, *, module_type: str | None = None, module_id: str | None = None
) -> ItemMatch | None:
    """Resolve an item ref, bare id, name, spoken name or synonym.

    When several catalog items share a name, items a module of `module_type` (or exactly `module_id`)
    accepts win; ties keep profile order. None if nothing matches.
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        catalog_id, item = profile.item(cleaned)
        return ItemMatch(catalog_id, item, profile.item_ref(catalog_id, item))
    except (UnknownItem, NotFound):
        pass
    matches = [
        ItemMatch(catalog_id, item, profile.item_ref(catalog_id, item))
        for catalog_id, item in profile.catalog_items()
        if name_matches(item, cleaned)
    ]
    if len(matches) > 1 and module_type is not None:
        preferred = [
            match
            for match in matches
            if (accepting := profile.transactional_modules_for(match.ref, module_type))  # type: ignore[arg-type]
            and (module_id is None or module_id in accepting)
        ]
        matches = preferred or matches
    return matches[0] if matches else None


def pick_module(
    profile: Profile, item_ref: str, module_type: str, module_id: str | None
) -> tuple[str, JsonObj] | PlanProblem:
    """The transactional module of `module_type` that accepts `item_ref` (the requested one, or the
    first in document order)."""
    accepting = profile.transactional_modules_for(item_ref, module_type)  # type: ignore[arg-type]
    if module_id:
        try:
            module = profile.module(module_id)
        except UnknownModule:
            return PlanProblem(code="unknown_module", detail=f"no module {module_id!r}", field="module_id")
        if module.get("type") != module_type:
            return PlanProblem(code="unknown_module", detail=f"{module_id!r} is not a {module_type} module", field="module_id")
        if module_id not in accepting:
            return PlanProblem(code="not_offered", detail=f"{item_ref} is not offered through {module_id}", field="item")
        return module_id, module
    if not accepting:
        return PlanProblem(code="not_offered", detail=f"{item_ref} cannot be requested this way", field="item")
    return accepting[0], profile.module(accepting[0])


# ---------------------------------------------------------------------------- option choices


def _option_groups(item: JsonObj) -> list[JsonObj]:
    return list(item.get("options", ()) or ())


def _find_group(item: JsonObj, key: str) -> JsonObj | None:
    wanted = normalize_name(key)
    for group in _option_groups(item):
        if wanted in {normalize_name(group["id"]), normalize_name(str(group.get("name", "")))}:
            return group
    return None


def _match_choice(group: JsonObj, text: str) -> JsonObj | None:
    choices: Sequence[JsonObj] = group.get("choices", ())
    return next((choice for choice in choices if name_matches(choice, text)), None)


def _split_multi(text: str) -> list[str]:
    return [part for part in re.split(r",|&|\+|\band\b", text) if part.strip()]


def _resolve_in_group(group: JsonObj, value: Any) -> list[str] | None:
    """Choice ids for `value` within one group (several for a multi-select group), or None."""
    texts = [str(v) for v in value] if isinstance(value, list | tuple) else [str(value)]
    multi = int(group.get("max_selections") or 1) > 1
    ids: list[str] = []
    for text in texts:
        match = _match_choice(group, text)
        if match is not None:
            ids.append(match["id"])
            continue
        if not multi:
            return None
        parts = [_match_choice(group, part) for part in _split_multi(text)]
        if not parts or any(part is None for part in parts):
            return None
        ids.extend(part["id"] for part in parts if part is not None)
    order = [choice["id"] for choice in group.get("choices", ())]
    return sorted(set(ids), key=order.index)


def resolve_choices(
    item_ref: str, item: JsonObj, requested: Mapping[str, Any]
) -> tuple[dict[str, str], list[PlanProblem]]:
    """Map `{group (id or name): choice (id, name, spoken name or synonym)}` to `{group id: choice id}`.

    A key that names no group is matched by its value against every group (unique match wins), so
    `{"sponge": "gluten-free vanilla"}` still lands in the `flavor` group. Multi-select groups take
    several choices (list, or "a, b and c"), stored comma-joined in choice order. Problems:
    `invalid_choice` (unknown or ambiguous value, too many selections) and `missing_choice` (a required
    group with no answer). The result's keys follow option-group order.
    """
    chosen: dict[str, list[str]] = {}
    problems: list[PlanProblem] = []
    groups = _option_groups(item)
    for key, value in requested.items():
        if is_blank(value):
            continue
        group = _find_group(item, str(key))
        if group is None:
            text = str(value) if not isinstance(value, list | tuple) else str(value[0])
            hits = [g for g in groups if _match_choice(g, text) is not None]
            if len(hits) != 1:
                problems.append(
                    PlanProblem(code="invalid_choice", detail=f"cannot tell which option {key!r} means", field=str(key))
                )
                continue
            group = hits[0]
        ids = _resolve_in_group(group, value)
        limit = int(group.get("max_selections") or 1)
        if ids is None:
            problems.append(
                PlanProblem(
                    code="invalid_choice",
                    detail=f"not a {group.get('name') or group['id']} we offer",
                    field=group["id"],
                    fact_ref=f"{item_ref}/{group['id']}#choices",
                )
            )
            continue
        if len(ids) > limit:
            problems.append(
                PlanProblem(
                    code="invalid_choice",
                    detail=f"choose at most {limit} for {group.get('name') or group['id']}",
                    field=group["id"],
                    fact_ref=f"{item_ref}/{group['id']}#max_selections",
                )
            )
            continue
        chosen[group["id"]] = ids
    for group in groups:
        if group.get("required") and group["id"] not in chosen and not any(p.field == group["id"] for p in problems):
            problems.append(
                PlanProblem(
                    code="missing_choice",
                    detail=f"choose a {group.get('name') or group['id']}",
                    field=group["id"],
                    fact_ref=f"{item_ref}/{group['id']}#spoken_question" if group.get("spoken_question") else None,
                )
            )
    ordered = {g["id"]: ",".join(chosen[g["id"]]) for g in groups if g["id"] in chosen}
    return ordered, problems


def choice_refs(item_ref: str, item: JsonObj, choices: Mapping[str, str]) -> list[str]:
    """Full choice refs (`<item ref>/<group>/<choice>`) in option-group order (approval built-in)."""
    return [sel.ref for sel in selected_choices(item_ref, item, choices)]


def group_answers(item: JsonObj, intake: Mapping[str, Any], specs: Sequence[IntakeFieldSpec]) -> dict[str, Any]:
    """Option-group answers passed as intake/details keys (keys equal to a group id that are not
    intake fields), for tools whose signature has no `choices` argument."""
    intake_keys = {spec.key for spec in specs}
    return {
        group["id"]: intake[group["id"]]
        for group in _option_groups(item)
        if group["id"] in intake and group["id"] not in intake_keys and not is_blank(intake[group["id"]])
    }


# ---------------------------------------------------------------------------- bound intake fields


def bound_field(specs: Sequence[IntakeFieldSpec], ids: Collection[str]) -> IntakeFieldSpec | None:
    """The first `choice` intake field whose option values include one of `ids`.

    Such a field selects a profile entity (e.g. `stylist` -> resources, `time_window` -> visit
    windows, `job_type` -> items); its other options ("any", "first available") mean no preference.
    """
    if not ids:
        return None
    for spec in specs:
        if spec.type == "choice" and any(value in ids for value, _ in spec.options):
            return spec
    return None


def match_entity(entities: Sequence[JsonObj], text: str | None, *extra_fields: str) -> JsonObj | None:
    """The entity (resource, visit window, ...) named by `text`, by id, name, spoken name or synonym."""
    if text is None or not str(text).strip():
        return None
    for entity in entities:
        if entity.get("id") == text:
            return entity
    for entity in entities:
        if name_matches(entity, str(text), *extra_fields):
            return entity
    return None


# ---------------------------------------------------------------------------- dates


def parse_day(text: Any) -> date | None:
    """An ISO date (`2026-10-18`), or the date part of an ISO datetime; None if unparseable."""
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    try:
        return date.fromisoformat(cleaned[:10]) if re.match(r"^\d{4}-\d{2}-\d{2}", cleaned) else None
    except ValueError:
        return None


def minutes_until(now: datetime, moment: datetime) -> int:
    """Whole minutes from `now` to `moment` (negative if past): the `lead_minutes` built-in."""
    return int((moment - now).total_seconds() // 60)


# ---------------------------------------------------------------------------- plan assembly


def say_before_confirm(module: JsonObj) -> tuple[str, ...]:
    """Knowledge entry refs read before the confirmation question (`confirmation.say_before_confirm`)."""
    return tuple((module.get("confirmation") or {}).get("say_before_confirm", ()) or ())


def build_read_back(
    module: JsonObj,
    *,
    builtins: Mapping[str, tuple[JsonValue, Sequence[str]]],
    intake: Mapping[str, JsonValue],
    intake_refs: Mapping[str, Sequence[str]] | None = None,
) -> tuple[ReadBackItem, ...]:
    """One `ReadBackItem` per `confirmation.read_back` key that has a value (intake keys win over a
    built-in of the same name; keys without a value are skipped)."""
    items: list[ReadBackItem] = []
    for key in (module.get("confirmation") or {}).get("read_back", ()) or ():
        if key in intake:
            value, refs = intake[key], tuple((intake_refs or {}).get(key, ()))
        elif key in builtins:
            value, refs = builtins[key][0], tuple(builtins[key][1])
        else:
            continue
        if value is None or value == [] or value == {}:
            continue
        items.append(ReadBackItem(key=key, value=value, fact_refs=dedupe(refs)))
    return tuple(items)


def approval_facts(
    intake: Mapping[str, JsonValue],
    *,
    item_ref: str | None,
    choice_refs: Sequence[str] = (),
    when: str | None = None,
    price: PriceQuote | None = None,
    party_size: int | None = None,
    visit_window: str | None = None,
    lead_minutes: int | None = None,
) -> dict[str, Any]:
    """Intake values plus the approval-rule built-ins (see `approval_rules`)."""
    amount = price.amount_cents if price is not None and price.kind != "quote_only" else None
    facts: dict[str, Any] = dict(intake)
    builtins: dict[str, Any] = {
        "item": item_ref,
        "choices": list(choice_refs),
        "when": when,
        "price": amount,
        "order_total_cents": amount,
        "party_size": party_size,
        "visit_window": visit_window,
        "lead_minutes": lead_minutes,
    }
    for key, value in builtins.items():
        if value is not None or key not in facts:
            facts[key] = value
    return facts


def price_builtin(price: PriceQuote | None) -> tuple[JsonValue, tuple[str, ...]]:
    """The `price` read-back value: the quote's JSON form, with its fact refs."""
    if price is None:
        return None, ()
    return price.model_dump(mode="json"), price.fact_refs


def rejected_plan(
    profile: Profile,
    *,
    module_id: str,
    kind: CommitmentKind,
    operation: Operation,
    principal: str,
    problem: PlanProblem,
    payload: Mapping[str, Any],
) -> CommitPlan:
    """A plan that cannot go further (unknown item or module): just the problem, still deterministic."""
    key = idempotency_key(
        business_id=profile.profile_id, module_id=module_id, operation=operation, principal=principal, payload=payload
    )
    return CommitPlan(
        business_id=profile.profile_id,
        module_id=module_id,
        kind=kind,
        operation=operation,
        principal=principal,
        profile_revision=profile.revision,
        idempotency_key=key,
        problems=(problem,),
    )


def with_disputes(
    profile: Profile, problems: Sequence[PlanProblem], refs: Iterable[str | None], *, field: str | None = None
) -> list[PlanProblem]:
    """`problems` with a `disputed_fact` problem first when one of `refs` is disputed (L6).

    Used for the facts a plan reads back and stores (prices, the slot): the customer must never be
    asked to commit to, or be refused on, a value the owner is still double-checking. The dispute
    always comes first (it is what the reply is about); a plan that already carries one only has it
    moved to the front.
    """
    existing = [p for p in problems if p.code == "disputed_fact"]
    if existing:
        return [*existing, *(p for p in problems if p.code != "disputed_fact")]
    disputed = disputed_problem(profile, refs, field=field)
    return [disputed, *problems] if disputed is not None else list(problems)


def raise_if_disputed(engine: AvailabilityEngine, profile: Profile, module_id: str, item_ref: str | None = None,
                      choices: Mapping[str, str] | None = None) -> None:
    """Raise `errors.DisputedFact` when the module's availability rules are disputed (listing tools
    then withhold instead of saying nothing is open)."""
    problem = engine.rules_problem(profile, module_id, item_ref, choices)
    if problem is not None and problem.fact_ref is not None:
        raise DisputedFact(problem.detail, fact_ref=problem.fact_ref)


def first_module_id(profile: Profile, module_type: str) -> str:
    """Id of the first enabled module of a type ("" if none): the module of a rejected plan."""
    modules = profile.modules_of_type(module_type)  # type: ignore[arg-type]
    return modules[0][0] if modules else ""


# ---------------------------------------------------------------------------- the commit path


class CommitmentServiceBase:
    """Implements the shared commit path; subclasses implement `plan` for their module type."""

    def __init__(self, ctx: DomainContext) -> None:
        self.ctx = ctx

    async def replay(self, plan: CommitPlan) -> CommitOutcome | None:
        """Fast path: if the request already produced a *live* commitment, return it (replayed=True).

        A settled commitment (`SETTLED_STATUSES`: cancelled, declined, expired, counter-offered, other
        time suggested, quoted, answered) is never replayed: the request is over, so the same request
        may be made again (see `with_generation`)."""
        existing = await self.existing_commitment(plan.business_id, plan.idempotency_key)
        return CommitOutcome(commitment=existing, replayed=True) if existing is not None else None

    async def _stored(self, business_id: str, key: str) -> Commitment | None:
        """The commitment one exact key produced, if any: the marker first, then the id derived from
        the key. The derived id is what dedupes a live commitment for its whole life; the marker's TTL
        only bounds the marker row (see `domain.idempotency`)."""
        marker = await self.ctx.idempotency.get(key)
        if marker is not None and marker.business_id == business_id:
            found = await self.ctx.commitments.get(business_id, marker.commitment_id)
            if found is not None:
                return found
        return await self.ctx.commitments.get(business_id, self.ctx.ids.commitment_id(key))

    async def current_generation(self, business_id: str, key: str) -> tuple[str | None, Commitment | None]:
        """(key of the request's current attempt, its live commitment or None).

        Walks `generation_key(key, 0..)` past settled commitments (`SETTLED_STATUSES`): the first
        generation with no commitment is where a new attempt commits; the first live one is what a
        retry replays. `(None, None)` once all `MAX_GENERATIONS` attempts are settled: the request
        cannot be made again, and a settled outcome is never replayed instead."""
        for generation in range(MAX_GENERATIONS):
            current = generation_key(key, generation)
            found = await self._stored(business_id, current)
            if found is None or found.status not in SETTLED_STATUSES:
                return current, found
        return None, None

    async def existing_commitment(self, business_id: str, key: str) -> Commitment | None:
        """The live commitment of the request with idempotency key `key`, if any (settled ones excluded)."""
        _current, found = await self.current_generation(business_id, key)
        return found

    async def with_generation(self, plan: CommitPlan) -> CommitPlan:
        """`plan` re-keyed to the request's current attempt, so a request whose earlier commitment was
        settled commits afresh (new commitment and task ids) instead of colliding with the old one.

        When every attempt is used up (`MAX_GENERATIONS` settled ones) the plan comes back with a
        `too_many_attempts` problem: callers must check `plan.ok` again before committing."""
        current, _found = await self.current_generation(plan.business_id, plan.idempotency_key)
        if current is None:
            return plan.model_copy(update={"problems": (*plan.problems, TOO_MANY_ATTEMPTS)})
        return plan if current == plan.idempotency_key else plan.model_copy(update={"idempotency_key": current})

    async def settle_replay(self, plan: CommitPlan) -> CommitPlan:
        """A request that already produced a live commitment stays committable whatever capacity or the
        clock say now (its own holds fill the slot): problems are cleared so the commit replays it."""
        if not plan.problems:
            return plan
        existing = await self.existing_commitment(plan.business_id, plan.idempotency_key)
        if existing is None:
            return plan
        return plan.model_copy(update={"problems": (), "holds": existing.holds, "resource_id": existing.resource_id})

    async def commit(self, profile: Profile, plan: CommitPlan, *, task_id: str | None) -> CommitOutcome:
        """Create the commitment for an `ok` plan in one atomic unit.

        Steps: refuse a plan that is not `ok` (ValueError: callers must check first); build the
        `Commitment` (id = `ids.commitment_id(plan.idempotency_key)`; status `pending_owner` when
        `plan.approval.required` or the kind always needs the owner, else `confirmed`; `expires_at` from
        the owner_approval module's `expires_after_minutes`); `commitments.commit(...)` with the plan's
        holds and an idempotency record; on `Duplicate` return the existing commitment with
        `replayed=True`; on `CapacityExhausted` return `problem=capacity_full`; on a fresh
        `pending_owner` commitment call `notifier.notify` (failures are logged, never raised: the owner
        inbox still lists the request).
        """
        if not plan.ok:
            raise ValueError("plan is not ok: check plan.problems and plan.missing before committing")
        now = self.ctx.clock.now()
        needs_owner = plan.approval.required or plan.kind in OWNER_KINDS
        expires_at: datetime | None = None
        if needs_owner:
            owner = owner_module_for(profile, plan.approval.owner_module_id or plan.module_id)
            minutes = (owner[1] if owner else {}).get("expires_after_minutes")
            if minutes:
                expires_at = now + timedelta(minutes=int(minutes))
        commitment = Commitment(
            commitment_id=self.ctx.ids.commitment_id(plan.idempotency_key),
            business_id=plan.business_id,
            module_id=plan.module_id,
            kind=plan.kind,
            status="pending_owner" if needs_owner else "confirmed",
            principal=plan.principal,
            idempotency_key=plan.idempotency_key,
            profile_revision=plan.profile_revision,
            item_ref=plan.item_ref,
            choices=dict(plan.choices),
            window=plan.window,
            day=plan.day,
            party_size=plan.party_size,
            visit_window_id=plan.visit_window_id,
            resource_id=plan.resource_id,
            intake=dict(plan.intake),
            price=plan.price,
            approval=plan.approval,
            holds=plan.holds,
            question=plan.question,
            fact_ref=plan.fact_ref,
            task_id=task_id,
            created_at=now,
            updated_at=now,
            expires_at=expires_at,
        )
        record = new_record(
            plan.idempotency_key,
            business_id=plan.business_id,
            commitment_id=commitment.commitment_id,
            now=now,
            ttl=self.ctx.idempotency_ttl,
        )
        result = await self.ctx.commitments.commit(commitment, holds=plan.holds, idempotency=record)
        match result:
            case Committed(commitment=stored):
                if stored.status == "pending_owner":
                    await self._notify(profile, stored)
                return CommitOutcome(commitment=stored)
            case Duplicate(existing=existing):
                return CommitOutcome(commitment=existing, replayed=True)
            case CapacityExhausted(key=key):
                problem = PlanProblem(
                    code="capacity_full",
                    detail="someone else just took the last place",
                    alternatives=await self.capacity_alternatives(profile, plan, full_key=key),
                )
                return CommitOutcome(commitment=None, problem=problem)
        raise AssertionError(f"unexpected commit result {result!r}")  # pragma: no cover

    async def capacity_alternatives(
        self, profile: Profile, plan: CommitPlan, *, full_key: str | None = None
    ) -> tuple[SlotOption, ...]:
        """Up to 3 open alternatives (current usage) for a plan whose slot or day is full."""
        engine, capacity = self.ctx.availability, self.ctx.capacity
        try:
            if plan.kind == "booking" and plan.window is not None and plan.item_ref:
                slots = await engine.open_booking_slots(
                    profile,
                    plan.module_id,
                    plan.item_ref,
                    capacity,
                    date_from=plan.window.start.date(),
                    party_size=plan.party_size or 1,
                    choices=plan.choices,
                    limit=4,
                    not_before=plan.window.start,
                )
                return tuple(
                    slot
                    for slot, _ in slots
                    if slot.window
                    and not (slot.window.start == plan.window.start and slot.resource_id == plan.resource_id)
                )[:3]
            if plan.day is not None:
                days = await engine.open_days(profile, plan.module_id, capacity, date_from=plan.day, limit=4)
                return tuple(slot for slot, _ in days if slot.day != plan.day)[:3]
        except ShopfrontError:
            log.warning("could not compute alternatives", extra={"module_id": plan.module_id, "full_key": full_key})
        return ()

    async def open_days_after(self, profile: Profile, module_id: str, day: date) -> tuple[SlotOption, ...]:
        """Up to 3 days after `day` that are open and still have room (current usage)."""
        days = await self.ctx.availability.open_days(
            profile, module_id, self.ctx.capacity, date_from=day, limit=MAX_ALTERNATIVES + 1
        )
        return tuple(slot for slot, _ in days if slot.day != day)[:MAX_ALTERNATIVES]

    async def with_open_days(self, profile: Profile, module_id: str, problem: PlanProblem, day: date) -> PlanProblem:
        """A day problem whose alternatives are re-checked against current usage (the engine's own
        alternatives only follow the rules)."""
        if problem.code == "unknown_module":
            return problem
        today = self.ctx.clock.now().astimezone(profile.timezone).date()
        start = today if problem.code in ("in_the_past", "too_far_ahead", "lead_time") else max(day, today)
        days = await self.ctx.availability.open_days(
            profile, module_id, self.ctx.capacity, date_from=start, limit=MAX_ALTERNATIVES + 1
        )
        alternatives = tuple(slot for slot, _ in days if slot.day != day)[:MAX_ALTERNATIVES]
        return problem.model_copy(update={"alternatives": alternatives})

    async def _notify(self, profile: Profile, commitment: Commitment) -> None:
        try:
            await self.ctx.notifier.notify(profile, commitment)
        except Exception:  # noqa: BLE001 - the owner inbox still lists the request
            log.exception(
                "owner notification failed",
                extra={"business_id": commitment.business_id, "commitment_id": id_digest(commitment.commitment_id)},
            )


def owner_module_for(profile: Profile, module_id: str | None) -> tuple[str, JsonObj] | None:
    """The owner_approval module responsible for `module_id` (itself if it is one), else the first."""
    if module_id:
        try:
            if profile.module_type(module_id) == "owner_approval":
                return module_id, profile.module(module_id)
        except UnknownModule:
            pass
        try:
            return profile.owner_approval(module_id)
        except UnknownModule:
            pass
    try:
        return profile.owner_approval(None)
    except UnknownModule:
        return None


# ---------------------------------------------------------------------------- owner deadlines

TASK_TTL_SLACK = timedelta(hours=24)
"""An owner task outlives its request's last deadline by a day, so the final result can still be polled."""


def owner_module_of(profile: Profile, request: Commitment | CommitPlan) -> tuple[str, JsonObj] | None:
    """The owner_approval module that decides `request` and sets its expiry (the one `commit` reads)."""
    return owner_module_for(profile, request.approval.owner_module_id or request.module_id)


def service_deadline(profile: Profile, request: Commitment | CommitPlan) -> datetime | None:
    """When the service `request` asks for is due, after which approving it no longer makes sense.

    A booking's slot start; the end of a custom order's day in the business timezone. None for requests
    whose date is only a preference (quote requests: a quote stays useful after the preferred visit day)
    or that have no date at all (owner questions)."""
    if request.kind == "booking" and request.window is not None:
        return request.window.start
    if request.kind == "custom_order" and request.day is not None:
        return datetime.combine(request.day + timedelta(days=1), time.min, tzinfo=profile.timezone).astimezone(UTC)
    return None


def keep_waiting_horizon(
    profile: Profile, request: Commitment | CommitPlan, *, since: datetime, max_wait: timedelta
) -> datetime:
    """How long a request under `on_expiry: keep_waiting` may wait for the owner: until its service time
    (`service_deadline`), and never more than `max_wait` after `since` (when it was made). After that it
    ends like `notify_customer` (expired, holds released, the customer told)."""
    cap = since + max_wait
    deadline = service_deadline(profile, request)
    return min(deadline, cap) if deadline is not None else cap


def owner_deadline(
    profile: Profile, request: Commitment | CommitPlan, *, since: datetime, keep_waiting_max: timedelta
) -> datetime | None:
    """The last moment the owner can answer a request made at `since` before the expiry sweep ends it:
    `since + expires_after_minutes`, or under `keep_waiting` the later `keep_waiting_horizon`. None when
    the owner module sets no expiry. Its owner task must stay addressable until this plus `TASK_TTL_SLACK`."""
    owner = owner_module_of(profile, request)
    module = owner[1] if owner else {}
    minutes = module.get("expires_after_minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
        return None
    deadline = since + timedelta(minutes=minutes)
    if module.get("on_expiry") == "keep_waiting":
        deadline = max(deadline, keep_waiting_horizon(profile, request, since=since, max_wait=keep_waiting_max))
    return deadline


def whole_ms(delta: timedelta) -> int:
    """`delta` in whole milliseconds, rounded up (never negative): a task TTL."""
    return max(math.ceil(delta / timedelta(milliseconds=1)), 0)

