"""Jobs priced per request (`quote_request` modules): the owner replies with a quote through a Task.

Conventions beyond the schema (generic, none business-specific):
  * Visit windows: the `visit_window` argument (id or spoken name) fills a `choice` intake field whose
    options are the module's visit-window ids (the gold profile's `time_window`), and vice versa. Other
    options of that field ("first available") mean no preferred window.
  * Job type: a `choice` intake field whose options are the module's item ids (`job_type`) is filled
    from the `item` argument when unanswered.
  * Service area: an intake value under a postal-code key (`zip`, `postal_code`, `postcode`,
    `zip_code`) is checked against the fulfilment service areas; a definite miss is the problem
    `outside_service_area`, an undecidable one (radius-only areas) is left to the owner. A check that
    relied on a disputed area fact is the problem `disputed_fact` instead (L6), whichever way it went.
  * Visit window: the requested arrival window must overlap the requested day's opening lines
    (problem `outside_hours` otherwise), so the owner is never sent an impossible request.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from shopfront_server.domain.approval_rules import evaluate_approval
from shopfront_server.domain.availability import boundary, parse_hhmm, schedule_for
from shopfront_server.domain.idempotency import idempotency_key
from shopfront_server.domain.intake import field_specs, is_blank, nonce_payload, normalize_intake
from shopfront_server.domain.models import ApprovalCheck, CapacityHold, PriceQuote, TimeWindow
from shopfront_server.domain.planning import CommitPlan, PlanProblem, SlotOption
from shopfront_server.domain.pricing import price_for
from shopfront_server.domain.services.base import (
    CommitmentServiceBase,
    approval_facts,
    bound_field,
    build_read_back,
    dedupe,
    first_module_id,
    match_entity,
    minutes_until,
    normalize_name,
    owner_module_for,
    parse_day,
    pick_module,
    price_builtin,
    raise_if_disputed,
    rejected_plan,
    resolve_item,
    say_before_confirm,
    with_disputes,
)
from shopfront_server.errors import NotFound, UnknownItem, UnknownModule
from shopfront_server.profile.model import JsonObj, Profile

POSTAL_KEYS: tuple[str, ...] = ("postal_code", "zip", "zip_code", "postcode", "post_code")
"""Intake keys whose value is checked against the service area."""

_US_ZIP_PLUS_FOUR = re.compile(r"^(\d{5})-?\d{4}$")


class ServiceAreaCheck(BaseModel):
    """Whether a customer location is inside a service area. `served=None` = cannot tell (unknown)."""

    served: bool | None
    module_id: str
    location_id: str | None = None
    matched: str | None = None
    """The postal code or locality that matched."""
    fact_refs: list[str] = []


def normalize_postal_code(text: str) -> str:
    """Upper-case, no spaces; a US ZIP+4 keeps its five-digit ZIP."""
    compact = re.sub(r"\s+", "", text).upper()
    plus_four = _US_ZIP_PLUS_FOUR.match(compact)
    return plus_four.group(1) if plus_four else compact


def service_area_locations(profile: Profile, module: JsonObj) -> list[JsonObj]:
    """The module's fulfilment locations that define an `area`; all service-area locations as fallback."""
    located: list[JsonObj] = []
    for location_id in (module.get("fulfillment") or {}).get("location_ids", ()) or ():
        try:
            location = profile.location(location_id)
        except NotFound:
            continue
        if location.get("area"):
            located.append(location)
    if located:
        return located
    return [location for location in profile.locations() if location.get("area")]


class QuoteService(CommitmentServiceBase):
    def check_service_area(
        self, profile: Profile, *, postal_code: str | None, locality: str | None, module_id: str | None = None
    ) -> ServiceAreaCheck:
        """Match against the module's fulfilment `location_ids` service areas (postal codes, localities).

        A radius-only area cannot be decided without geocoding: `served=None` (hand off to the owner).
        No postal code and no locality also gives `served=None`. Raises `errors.UnknownModule` when the
        business has no (such) quote_request module.
        """
        mid, module = self._module(profile, module_id)
        locations = service_area_locations(profile, module)
        wanted_code = normalize_postal_code(postal_code) if postal_code and postal_code.strip() else None
        wanted_place = normalize_name(locality) if locality and locality.strip() else None
        checked_refs: list[str] = []
        undecidable = False
        for location in locations:
            area = location.get("area") or {}
            loc_id = location["id"]
            codes = {normalize_postal_code(str(code)) for code in area.get("postal_codes", ()) or ()}
            places = {normalize_name(str(place)): str(place) for place in area.get("localities", ()) or ()}
            if wanted_code and wanted_code in codes:
                return ServiceAreaCheck(
                    served=True,
                    module_id=mid,
                    location_id=loc_id,
                    matched=wanted_code,
                    fact_refs=[f"locations/{loc_id}#area.postal_codes"],
                )
            if wanted_place and wanted_place in places:
                return ServiceAreaCheck(
                    served=True,
                    module_id=mid,
                    location_id=loc_id,
                    matched=places[wanted_place],  # the profile's own spelling, never the caller's text
                    fact_refs=[f"locations/{loc_id}#area.localities"],
                )
            if area.get("radius"):
                undecidable = True
                checked_refs.append(f"locations/{loc_id}#area.radius")
            if wanted_code and codes:
                checked_refs.append(f"locations/{loc_id}#area.postal_codes")
            if wanted_place and places:
                checked_refs.append(f"locations/{loc_id}#area.localities")
        if (wanted_code is None and wanted_place is None) or undecidable or not locations:
            return ServiceAreaCheck(served=None, module_id=mid, fact_refs=list(dedupe(checked_refs)))
        # A locality-only miss is not definite when postal codes could still match (and vice versa).
        decidable = any(
            (wanted_code and (loc.get("area") or {}).get("postal_codes"))
            or (wanted_place and (loc.get("area") or {}).get("localities"))
            for loc in locations
        )
        return ServiceAreaCheck(served=False if decidable else None, module_id=mid, fact_refs=list(dedupe(checked_refs)))

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
        """Days a visit for a quote could be requested (the service area's working days, closures).

        Raises `errors.UnknownItem`, `errors.UnknownModule` or `errors.DisputedFact`.
        """
        match = resolve_item(profile, item, module_type="quote_request", module_id=module_id)
        if match is None:
            raise UnknownItem("unknown item")
        picked = pick_module(profile, match.ref, "quote_request", module_id)
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
        intake: Mapping[str, Any],
        principal: str,
        day: str | None = None,
        visit_window: str | None = None,
        module_id: str | None = None,
        nonce: str | None = None,
    ) -> CommitPlan:
        """Plan a quote request. Never prices the job (`price` only if the item has a statable `from`
        price, which the assistant may mention as 'starting at'). Approval is always required.
        `nonce` (unauthenticated callers) only enters the key when the module collects no contact
        field (`intake.nonce_payload`)."""
        now = self.ctx.clock.now()
        raw_payload = {"item": item, "day": day, "visit_window": visit_window}
        match = resolve_item(profile, item, module_type="quote_request", module_id=module_id)
        picked: tuple[str, JsonObj] | PlanProblem = (
            PlanProblem(code="unknown_item", detail="no such job type", field="item")
            if match is None
            else pick_module(profile, match.ref, "quote_request", module_id)
        )
        if isinstance(picked, PlanProblem) or match is None:
            assert isinstance(picked, PlanProblem)
            return rejected_plan(
                profile,
                module_id=module_id or first_module_id(profile, "quote_request"),
                kind="quote_request",
                operation="quote_request",
                principal=principal,
                problem=picked,
                payload=raw_payload,
            )
        mid, module = picked
        ref = match.ref
        specs = field_specs(profile.intake_fields(mid))
        raw = dict(intake)
        problems: list[PlanProblem] = []

        item_ids = {str(r).rsplit("/", 1)[-1] for r in module.get("items", ()) or ()}
        job_field = bound_field(specs, item_ids)
        item_id = ref.rsplit("/", 1)[-1]
        if job_field is not None and is_blank(raw.get(job_field.key)) and any(v == item_id for v, _ in job_field.options):
            raw[job_field.key] = item_id

        windows = list(module.get("visit_windows", ()) or ())
        window_field = bound_field(specs, {w["id"] for w in windows})
        window_id: str | None = None
        if visit_window is not None and str(visit_window).strip():
            found = match_entity(windows, visit_window)
            if found is None and window_field is not None:
                found = _window_from_option(window_field.options, windows, visit_window)
            if found is None:
                problems.append(
                    PlanProblem(
                        code="unknown_visit_window", detail="not one of the visit windows", field="visit_window"
                    )
                )
            else:
                window_id = found["id"]
                if window_field is not None and is_blank(raw.get(window_field.key)):
                    raw[window_field.key] = window_id

        values, intake_problems, missing = normalize_intake(specs, raw, locale=profile.locale)
        problems.extend(intake_problems)
        if window_id is None and window_field is not None and values.get(window_field.key) in {w["id"] for w in windows}:
            window_id = str(values[window_field.key])

        area_refs: list[str] = []
        postal_key = next((key for key in POSTAL_KEYS if key in values), None)
        if postal_key is not None:
            area = self.check_service_area(profile, postal_code=str(values[postal_key]), locality=None, module_id=mid)
            area_refs = list(area.fact_refs)
            disputed_area = with_disputes(profile, [], area_refs, field=postal_key)
            if disputed_area:
                problems = [*disputed_area, *problems]
            elif area.served is False:
                problems.append(
                    PlanProblem(
                        code="outside_service_area",
                        detail="outside the service area",
                        field=postal_key,
                        fact_ref=area.fact_refs[0] if area.fact_refs else None,
                    )
                )

        requested_day: date | None = None
        holds: tuple[CapacityHold, ...] = ()
        day_refs: tuple[str, ...] = ()
        lead_minutes: int | None = None
        if day is not None and str(day).strip():
            requested_day = parse_day(day)
            if requested_day is None:
                problems.append(PlanProblem(code="invalid_intake", detail="expected a date like 2026-10-18", field="date"))
        else:
            dated = next((spec for spec in specs if spec.type == "date" and values.get(spec.key)), None)
            requested_day = parse_day(values[dated.key]) if dated is not None else None
        if requested_day is not None:
            checked = self.ctx.availability.check_day(profile, mid, requested_day)
            if isinstance(checked, PlanProblem):
                problems.append(await self.with_open_days(profile, mid, checked, requested_day))
            else:
                slot, holds = checked
                day_refs = slot.fact_refs
                if slot.window is not None:
                    lead_minutes = minutes_until(now, slot.window.start)
                if await self.ctx.availability.is_free(holds, self.ctx.capacity) is not None:
                    problems.append(
                        PlanProblem(
                            code="capacity_full",
                            detail="no more visits that day",
                            field="date",
                            alternatives=await self.open_days_after(profile, mid, requested_day),
                        )
                    )

        window = _visit_window(profile, windows, window_id, requested_day)
        if window is not None and requested_day is not None and not _within_hours(profile, mid, module, window, requested_day):
            problems.append(
                PlanProblem(
                    code="outside_hours",
                    detail="that arrival window is outside the hours on that day",
                    field="visit_window",
                    fact_ref=schedule_for(profile, mid, module).hours_ref,
                )
            )
        statable = price_for(profile, ref)
        price: PriceQuote | None = statable if statable is not None and statable.kind == "from" else None
        if price is not None and with_disputes(profile, [], price.fact_refs):
            price = None  # a disputed "starting at" price is simply not mentioned (quotes are never priced)
        owner = owner_module_for(profile, mid)
        evaluated = evaluate_approval(
            mid,
            module,
            approval_facts(
                values,
                item_ref=ref,
                when=requested_day.isoformat() if requested_day else None,
                price=price,
                visit_window=window_id,
                lead_minutes=lead_minutes,
            ),
            owner_module_id=owner[0] if owner else None,
        )
        approval = evaluated if evaluated.required else ApprovalCheck(
            required=True,
            owner_module_id=evaluated.owner_module_id or (owner[0] if owner else None),
            reasons_spoken=evaluated.reasons_spoken,
            matched_rule_keys=evaluated.matched_rule_keys,
            fact_refs=evaluated.fact_refs,
        )
        when = requested_day.isoformat() if requested_day else None
        key = idempotency_key(
            business_id=profile.profile_id,
            module_id=mid,
            operation="quote_request",
            principal=principal,
            payload={
                "item": ref,
                "day": when,
                "visit_window": window_id,
                "intake": values,
                **nonce_payload(specs, nonce),
            },
        )
        window_refs = [f"{mid}/{window_id}#spoken_name"] if window_id else []
        intake_refs: dict[str, list[str]] = {}
        if window_field is not None and window_id:
            intake_refs[window_field.key] = window_refs
        if job_field is not None and values.get(job_field.key) in item_ids:
            intake_refs[job_field.key] = [f"{ref.rsplit('/', 1)[0]}/{values[job_field.key]}#spoken_name"]
        if postal_key is not None:
            intake_refs[postal_key] = area_refs
        read_back = build_read_back(
            module,
            builtins={
                "item": (ref, [f"{ref}#spoken_name"]),
                "when": (when, day_refs),
                "visit_window": (window_id, window_refs),
                "price": price_builtin(price),
            },
            intake=values,
            intake_refs=intake_refs,
        )
        say = say_before_confirm(module)
        plan = CommitPlan(
            business_id=profile.profile_id,
            module_id=mid,
            kind="quote_request",
            operation="quote_request",
            principal=principal,
            profile_revision=profile.revision,
            idempotency_key=key,
            item_ref=ref,
            window=window,
            day=requested_day,
            visit_window_id=window_id,
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
                    *window_refs,
                    *area_refs,
                    *approval.fact_refs,
                    *(r for item_ in read_back for r in item_.fact_refs),
                    *(f"{entry}#answer_spoken" for entry in say),
                ]
            ),
        )
        return await self.settle_replay(plan)

    # ---- internals ---------------------------------------------------------------------------

    def _module(self, profile: Profile, module_id: str | None) -> tuple[str, JsonObj]:
        if module_id:
            module = profile.module(module_id)
            if module.get("type") != "quote_request":
                raise UnknownModule(f"{module_id!r} is not a quote_request module")
            return module_id, module
        modules = profile.modules_of_type("quote_request")
        if not modules:
            raise UnknownModule("this business takes no quote requests")
        return modules[0]


def _window_from_option(
    options: tuple[tuple[str, str], ...], windows: list[JsonObj], text: str
) -> JsonObj | None:
    """A visit window named by the label of its intake option ("Weekday morning (arrival 8 am-12 pm)")."""
    wanted = normalize_name(text)
    for value, label in options:
        if wanted and wanted in (normalize_name(label), normalize_name(value)):
            return next((w for w in windows if w["id"] == value), None)
    return None


def _within_hours(profile: Profile, module_id: str, module: JsonObj, window: TimeWindow, day: date) -> bool:
    """True if the arrival window overlaps one of the day's opening lines (no lines: cannot tell, True)."""
    schedule = schedule_for(profile, module_id, module)
    lines = schedule.day_lines(day)
    if not lines:
        return True  # a closed day is reported by check_day itself
    tz = profile.timezone
    return any(
        window.start < boundary(day, closes, tz) and boundary(day, opens, tz) < window.end for opens, closes in lines
    )


def _visit_window(profile: Profile, windows: list[JsonObj], window_id: str | None, day: date | None) -> TimeWindow | None:
    """The requested arrival window on the requested day, as local times."""
    if window_id is None or day is None:
        return None
    found = next((w for w in windows if w["id"] == window_id), None)
    if found is None:
        return None
    tz = profile.timezone
    start: datetime = boundary(day, parse_hhmm(found["start"]), tz)
    end: datetime = boundary(day, parse_hhmm(found["end"]), tz)
    if end <= start:
        end = end + timedelta(days=1)
    return TimeWindow(start=start, end=end)
