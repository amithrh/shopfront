"""Jobs priced per request (`quote_request` modules): the owner replies with a quote through a Task.

Conventions beyond the schema (generic, none business-specific):
  * Visit windows: the `visit_window` argument (id or spoken name) fills a `choice` intake field whose
    options are the module's visit-window ids (the gold profile's `time_window`), and vice versa. Other
    options of that field ("first available") mean no preferred window.
  * Job type: a `choice` intake field whose options are the module's item ids (`job_type`) is filled
    from the `item` argument when unanswered.
  * Service area: an intake value under a postal-code key (`zip`, `postal_code`, `postcode`,
    `zip_code`) is checked against the fulfilment service areas; a definite miss is the problem
    `outside_service_area`, an undecidable one is left to the owner. A check that relied on a disputed
    area fact is the problem `disputed_fact` instead (L6), whichever way it went.
  * What counts as a definite miss: a listed area is not a promise to refuse everything else, so a
    location the areas do not list is only a "no" when the profile says so, through a knowledge entry
    whose `facts.service_zone` is `outside` (its postal-code facts, under a key containing `postal` or
    `zip`, and the places its variants and spoken answer name: "We don't take jobs in Wheat Ridge,
    Arvada, ... or Boulder County"), or, for a postal code, when it is in another region altogether
    (no listed or referred-out code shares its first three characters: 90210 for a Denver plumber).
    An unlisted code in the same region (80239) or an unlisted town ("Denver", "Stapleton") cannot be
    decided (`served=None`), and the owner decides.
  * Place names match whatever their case, punctuation, accents or plural ("highlands"), with a
    state or region after a comma or matching the business's own ("Lakewood, CO", "Lakewood CO"),
    and as a short form whose every word starts the listed name's ("Cap Hill", "Wash Park").
  * Visit window: the requested arrival window must overlap the requested day's opening lines
    (problem `outside_hours` otherwise), so the owner is never sent an impossible request.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from askonce_server.domain.approval_rules import evaluate_approval
from askonce_server.domain.availability import boundary, parse_hhmm, schedule_for
from askonce_server.domain.idempotency import idempotency_key
from askonce_server.domain.intake import field_specs, is_blank, nonce_payload, normalize_intake
from askonce_server.domain.models import ApprovalCheck, CapacityHold, PriceQuote, TimeWindow
from askonce_server.domain.planning import CommitPlan, PlanProblem, SlotOption
from askonce_server.domain.pricing import price_for
from askonce_server.domain.services.base import (
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
from askonce_server.errors import NotFound, UnknownItem, UnknownModule
from askonce_server.profile.model import JsonObj, Profile

POSTAL_KEYS: tuple[str, ...] = ("postal_code", "zip", "zip_code", "postcode", "post_code")
"""Intake keys whose value is checked against the service area."""

_US_ZIP_PLUS_FOUR = re.compile(r"^(\d{5})-?\d{4}$")
REGION_PREFIX = 3
"""Postal codes sharing this many leading characters are in the same region (a US ZIP's sectional
centre: 802xx is Denver)."""
OUTSIDE_ZONE = "outside"


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


def place_key(text: str) -> str:
    """`normalize_name` with simple plurals folded: "Highlands" -> "highland"."""
    words = [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")) else w for w in normalize_name(text).split()]
    return " ".join(words)


def _wanted_places(profile: Profile, locality: str) -> list[str]:
    """The place keys `locality` may name: as said, before a comma ("Lakewood, CO") and without a
    trailing region of the business's own addresses ("Lakewood CO")."""
    regions = {
        place_key(str((location.get("address") or {}).get("region") or ""))
        for location in profile.locations()
        if isinstance(location.get("address"), dict)
    } - {""}
    keys = [place_key(locality), place_key(locality.split(",")[0])]
    for key in list(keys):
        words = key.split()
        if len(words) > 1 and words[-1] in regions:
            keys.append(" ".join(words[:-1]))
    return [k for k in dict.fromkeys(keys) if k]


def _short_form(wanted: str, known: str) -> bool:
    """'cap hill' for 'capitol hill': same number of words, each the start (3+ letters) of the listed
    word, at least one of them whole."""
    said, listed = wanted.split(), known.split()
    if len(said) != len(listed) or len(said) < 2:
        return False
    pairs = list(zip(said, listed, strict=True))
    return all(len(a) >= 3 and b.startswith(a) for a, b in pairs) and any(a == b for a, b in pairs)


def _match_place(wanted: list[str], places: dict[str, str]) -> str | None:
    """The profile's own spelling of the listed place `wanted` names, if any."""
    for key in wanted:
        if key in places:
            return places[key]
    for key in wanted:
        for known, spelled in places.items():
            if _short_form(key, known):
                return spelled
    return None


def _contains_place(text: str, place: str) -> bool:
    return bool(place) and f" {place} " in f" {place_key(text)} "


def outside_statements(profile: Profile) -> list[tuple[str, JsonObj]]:
    """`(entry ref, entry)` of every knowledge entry stating places the business does not serve."""
    return [
        (f"{module_id}/{entry['id']}", entry)
        for module_id, entry in profile.knowledge_entries()
        if str((entry.get("facts") or {}).get("service_zone") or "") == OUTSIDE_ZONE
    ]


def _outside_codes(entry: JsonObj) -> dict[str, set[str]]:
    """Postal-code facts of an outside statement: fact key -> normalised codes."""
    out: dict[str, set[str]] = {}
    for key, value in (entry.get("facts") or {}).items():
        if "postal" not in key and "zip" not in key:
            continue
        values = value if isinstance(value, list) else [value]
        codes = {normalize_postal_code(m) for v in values if isinstance(v, str) for m in re.split(r"[,;]", v) if m.strip()}
        out[key] = {c for c in codes if c}
    return out


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
        """Match against the module's fulfilment `location_ids` service areas (postal codes, localities),
        then against the profile's outside statements (see the module docstring).

        A radius-only area cannot be decided without geocoding: `served=None` (hand off to the owner).
        No postal code and no locality also gives `served=None`, and so does a location neither listed
        nor stated to be outside. Raises `errors.UnknownModule` when the business has no (such)
        quote_request module.
        """
        mid, module = self._module(profile, module_id)
        locations = service_area_locations(profile, module)
        wanted_code = normalize_postal_code(postal_code) if postal_code and postal_code.strip() else None
        wanted_places = _wanted_places(profile, locality) if locality and locality.strip() else []
        checked_refs: list[str] = []
        undecidable = False
        listed_codes: set[str] = set()
        for location in locations:
            area = location.get("area") or {}
            loc_id = location["id"]
            codes = {normalize_postal_code(str(code)) for code in area.get("postal_codes", ()) or ()}
            places = {place_key(str(place)): str(place) for place in area.get("localities", ()) or ()}
            listed_codes |= codes
            if wanted_code and wanted_code in codes:
                return ServiceAreaCheck(
                    served=True,
                    module_id=mid,
                    location_id=loc_id,
                    matched=wanted_code,
                    fact_refs=[f"locations/{loc_id}#area.postal_codes"],
                )
            matched = _match_place(wanted_places, places) if wanted_places else None
            if matched is not None:
                return ServiceAreaCheck(
                    served=True,
                    module_id=mid,
                    location_id=loc_id,
                    matched=matched,  # the profile's own spelling, never the caller's text
                    fact_refs=[f"locations/{loc_id}#area.localities"],
                )
            if area.get("radius"):
                undecidable = True
                checked_refs.append(f"locations/{loc_id}#area.radius")
            if wanted_code and codes:
                checked_refs.append(f"locations/{loc_id}#area.postal_codes")
            if wanted_places and places:
                checked_refs.append(f"locations/{loc_id}#area.localities")
        if (wanted_code is None and not wanted_places) or undecidable or not locations:
            return ServiceAreaCheck(served=None, module_id=mid, fact_refs=list(dedupe(checked_refs)))
        referred: set[str] = set()
        for entry_ref, entry in outside_statements(profile):
            for key, codes in _outside_codes(entry).items():
                referred |= codes
                if wanted_code and wanted_code in codes:
                    return ServiceAreaCheck(served=False, module_id=mid, fact_refs=list(dedupe([*checked_refs, f"{entry_ref}#facts.{key}"])))
            texts = [*(str(v) for v in entry.get("question_variants") or ()), str(entry.get("answer_spoken") or "")]
            if any(_contains_place(text, key) for key in wanted_places for text in texts):
                return ServiceAreaCheck(served=False, module_id=mid, fact_refs=list(dedupe([*checked_refs, f"{entry_ref}#answer_spoken"])))
        if wanted_code and listed_codes:
            region = wanted_code[:REGION_PREFIX]
            if len(wanted_code) > REGION_PREFIX and not any(code[:REGION_PREFIX] == region for code in listed_codes | referred):
                return ServiceAreaCheck(served=False, module_id=mid, fact_refs=list(dedupe(checked_refs)))
        return ServiceAreaCheck(served=None, module_id=mid, fact_refs=list(dedupe(checked_refs)))

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
