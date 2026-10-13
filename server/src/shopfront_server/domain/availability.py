"""Rule-based availability: honest slots computed from the profile (no calendar sync).

Inputs per transactional module (`availability`, `slotting`, `services`, `resources`):

  weekly lines      local `HH:MM` open/close per weekday; absent -> the hours of the fulfilment /
                    session / primary storefront location. Two lines on a day = split shift.
  closures          module closures plus the location's `hours.closures` (inclusive date ranges).
  lead_time_minutes slot start >= now + lead time.
  max_advance_min   slot start <= now + max advance.
  capacity_per_slot max concurrent bookings per time cell (interval mode). Default: number of eligible
                    resources, or 1 without resources.
  capacity_per_day  max commitments per local date (custom orders: "two cakes a day").
  slotting          interval: candidate starts every `step_minutes` from each open time; a booking
                    occupies [start, start + item duration + service buffer) and must end by close.
                    fixed: `sessions` (days + start) with a seat `capacity` (party_size units).

Time handling: every computation happens in the business timezone (`ZoneInfo(business.timezone)`)
and is converted to UTC only for capacity keys. DST: a local start that does not exist (spring
forward) is skipped; an ambiguous one (fall back) uses the first occurrence (`fold=0`).

Capacity model (keys are shared with `CommitmentStore.commit`, which enforces the limits atomically):

  interval  one key per covered cell and resource:
            `CAP#<biz>#<module>#CELL#<utc start of cell>#<resource id or '-'>`  limit 1 per resource,
            or capacity_per_slot without resources. A 90-minute booking on a 15-minute grid holds 6 cells.
            Cells are `step_minutes` long unless that would let two overlapping bookings miss each
            other: the cell is the gcd of the step, the opening minutes and the zone's clock-change
            size, so a 45-minute grid across a 60-minute DST jump uses 15-minute cells (`cell_minutes`).
  fixed     `CAP#<biz>#<module>#SESSION#<session id>#<local date>`  units = party_size, limit = capacity.
  per day   `CAP#<biz>#<module>#DAY#<local date>`  units = 1, limit = capacity_per_day.

Resources: if a service lists `resource_ids`, only those can serve it. A resource attribute
`works_days` (weekday tokens), when present, limits the days that resource is offered. When the
customer names no resource, the first free eligible resource (profile order) is assigned.

Search bounds: one availability search covers at most `MAX_SEARCH_DAYS` days, never goes past
`now + max_advance_minutes`, and reads capacity a week at a time, stopping once `limit` open slots are
found (an unbounded `date_to` must not pin the event loop or burn read capacity).

Disputed rules (L6): when an open contradiction/confirm question targets a rule a module relies on
(weekly lines, closures, step, duration, lead time, max advance, capacity), the module offers nothing
and every check returns the problem `disputed_fact` (`rules_problem` tells the caller which fact).

Further conventions (documented here because the schema leaves them open):
  * With resources *and* an explicit `capacity_per_slot`, every cell also takes an aggregate `-` hold
    (limit `capacity_per_slot`): "three stylists, two chairs".
  * Duration: the last selected choice with `duration_minutes` (option-group order) replaces the
    item's; with neither, the longest choice duration of an unanswered group (so an offered slot fits
    any answer); with nothing at all, one step. Occupancy is rounded up to whole steps.
  * A day-granular module's day is valid if its first opening is at least `lead_time_minutes` away
    (and not beyond `max_advance_minutes`) and the day has not already closed.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from shopfront_server.clock import Clock, utc_iso
from shopfront_server.domain.disputes import disputed_problem
from shopfront_server.domain.models import CapacityHold, TimeWindow
from shopfront_server.domain.planning import PlanProblem, ProblemCode, SlotOption
from shopfront_server.domain.ports import CapacityStore
from shopfront_server.domain.pricing import selected_choices
from shopfront_server.errors import NotFound, UnknownItem, UnknownModule
from shopfront_server.profile.model import JsonObj, Profile

DEFAULT_SEARCH_DAYS = 14
"""How far `open_*` searches when the caller gives no end date."""

MAX_SEARCH_DAYS = 62
"""The widest range one `open_*` search covers (about two months); longer ranges are clamped."""

SEARCH_CHUNK_DAYS = 7
"""`open_*` evaluate and read capacity this many days at a time, stopping once `limit` is reached."""

ALTERNATIVE_SEARCH_DAYS = 31
"""How far ahead a problem looks for alternatives."""

MAX_ALTERNATIVES = 3
WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
NO_RESOURCE = "-"
DAY_MODULE_TYPES: frozenset[str] = frozenset({"custom_order", "quote_request", "booking"})

SlotWithHolds = tuple[SlotOption, tuple[CapacityHold, ...]]


def cell_key(business_id: str, module_id: str, cell_start_utc: datetime, resource_id: str | None) -> str:
    """Counter for one interval cell (`CAP#<biz>#<module>#CELL#<utc start>#<resource or ->`)."""
    if cell_start_utc.tzinfo is None:
        raise ValueError("cell start must be timezone-aware")
    return f"CAP#{business_id}#{module_id}#CELL#{utc_iso(cell_start_utc)}#{resource_id or NO_RESOURCE}"


def session_key(business_id: str, module_id: str, session_id: str, day: date) -> str:
    """Counter for one fixed session on one local date (units = seats)."""
    return f"CAP#{business_id}#{module_id}#SESSION#{session_id}#{day.isoformat()}"


def day_key(business_id: str, module_id: str, day: date) -> str:
    """Counter for a module's commitments on one local date (`capacity_per_day`)."""
    return f"CAP#{business_id}#{module_id}#DAY#{day.isoformat()}"


def weekday_token(day: date) -> str:
    """`mon` .. `sun`, as used by `weekly[].days` and `works_days`."""
    return WEEKDAYS[day.weekday()]


def parse_hhmm(text: str) -> int:
    """Minutes after midnight for `HH:MM` (`24:00` -> 1440)."""
    hours, minutes = text.split(":")
    return int(hours) * 60 + int(minutes)


def wall_time(day: date, minutes: int, tz: ZoneInfo) -> datetime | None:
    """The local instant `day + minutes` in `tz`, or None if that wall time does not exist (spring
    forward). Ambiguous wall times (fall back) resolve to the first occurrence (`fold=0`)."""
    extra_days, minute_of_day = divmod(minutes, 1440)
    target = day + timedelta(days=extra_days)
    naive = datetime(target.year, target.month, target.day, minute_of_day // 60, minute_of_day % 60)
    aware = naive.replace(tzinfo=tz, fold=0)
    if aware.astimezone(UTC).astimezone(tz).replace(tzinfo=None) != naive:
        return None
    return aware


def boundary(day: date, minutes: int, tz: ZoneInfo) -> datetime:
    """Like `wall_time`, but never None: a nonexistent boundary maps to the instant after the gap."""
    existing = wall_time(day, minutes, tz)
    if existing is not None:
        return existing
    extra_days, minute_of_day = divmod(minutes, 1440)
    target = day + timedelta(days=extra_days)
    naive = datetime(target.year, target.month, target.day, minute_of_day // 60, minute_of_day % 60)
    return naive.replace(tzinfo=tz).astimezone(UTC).astimezone(tz)


def is_existing_local(moment: datetime, tz: ZoneInfo) -> bool:
    """False if `moment` is a wall time in `tz` that a clock change skipped."""
    if getattr(moment.tzinfo, "key", None) != tz.key:
        return True
    return moment.astimezone(UTC).astimezone(tz).replace(tzinfo=None) == moment.replace(tzinfo=None)


def _days(first: date, last: date) -> Iterable[date]:
    for offset in range((last - first).days + 1):
        yield first + timedelta(days=offset)


def add_days(day: date, days: int) -> date:
    """`day + days`, saturating at `date.min` / `date.max` instead of raising OverflowError."""
    try:
        return day + timedelta(days=days)
    except OverflowError:
        return date.max if days > 0 else date.min


def horizon_date(now: datetime, max_advance_minutes: int | None, tz: ZoneInfo) -> date | None:
    """The last local date a start can fall on under `max_advance_minutes` (None = no limit)."""
    if max_advance_minutes is None:
        return None
    try:
        return (now + timedelta(minutes=int(max_advance_minutes))).astimezone(tz).date()
    except OverflowError:
        return None


def search_range(
    today: date, date_from: date | None, date_to: date | None, horizon: date | None = None
) -> tuple[date, date] | None:
    """The clamped `[first, last]` of a search: not before today, at most `MAX_SEARCH_DAYS` long and
    not past `horizon`. None when nothing is left to search."""
    first = max(date_from or today, today)
    last = date_to or add_days(first, DEFAULT_SEARCH_DAYS - 1)
    last = min(last, add_days(first, MAX_SEARCH_DAYS - 1))
    if horizon is not None:
        last = min(last, horizon)
    return (first, last) if first <= last else None


def _chunks(first: date, last: date) -> Iterable[tuple[date, date]]:
    start = first
    while start <= last:
        end = min(add_days(start, SEARCH_CHUNK_DAYS - 1), last)
        yield start, end
        if end == date.max:
            return
        start = end + timedelta(days=1)


def clock_change_minutes(tz: ZoneInfo, year: int) -> int:
    """gcd of the zone's UTC-offset changes during `year` in minutes (0 for a zone without DST)."""
    offsets = []
    for month in range(1, 13):
        offset = datetime(year, month, 1, 12, tzinfo=tz).utcoffset()
        offsets.append(int(offset.total_seconds() // 60) if offset is not None else 0)
    shift = 0
    for offset in offsets:
        shift = math.gcd(shift, abs(offset - offsets[0]))
    return shift


def cell_minutes(step: int, opening_minutes: Iterable[int], clock_change: int) -> int:
    """Capacity cell length: the gcd of the step, every opening minute and the clock-change size.

    Every valid start is then a whole number of cells from every other one (also across a DST jump),
    so two bookings that overlap in time always share a cell key. Usually equal to the step.
    """
    cell = step
    for minute in opening_minutes:
        cell = math.gcd(cell, minute)
    if clock_change:
        cell = math.gcd(cell, clock_change)
    return max(cell, 1)


def _dedupe(refs: Iterable[str | None]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(ref for ref in refs if ref))


def _problem(
    code: ProblemCode, detail: str, *, field: str | None = None, fact_ref: str | None = None
) -> PlanProblem:
    return PlanProblem(code=code, detail=detail, field=field, fact_ref=fact_ref)


# ---------------------------------------------------------------------------- schedules


@dataclass(frozen=True)
class Schedule:
    """Opening lines and closures for one module, after the location-hours fallback."""

    lines: tuple[tuple[frozenset[str], int, int], ...]
    """(weekday tokens, open minute, close minute); close <= open means the line runs past midnight."""
    closures: tuple[tuple[date, date], ...]
    hours_ref: str | None
    closure_refs: tuple[str, ...]
    location_id: str | None
    weekly_ref: str | None = None
    """The weekly lines themselves (`...#hours.weekly` for location hours): what a dispute is checked on."""

    def day_lines(self, day: date) -> list[tuple[int, int]]:
        token = weekday_token(day)
        out = []
        for days, opens, closes in self.lines:
            if token in days:
                out.append((opens, closes if closes > opens else closes + 1440))
        return sorted(out)

    def closed_on(self, day: date) -> bool:
        return any(first <= day <= last for first, last in self.closures)

    def line_at(self, day: date, minute: int) -> tuple[date, int, tuple[int, int]] | None:
        """`(schedule day, minute on that day's clock, line)` of the line covering `day` + `minute`.

        A line running past midnight belongs to the day it opened: Saturday 00:30 under a Friday
        20:00-02:00 line is Friday at minute 1470. The day's own lines win over the previous day's.
        """
        for opens, closes in self.day_lines(day):
            if opens <= minute < closes:
                return day, minute, (opens, closes)
        previous = day - timedelta(days=1)
        for opens, closes in self.day_lines(previous):
            if closes > 1440 and opens <= minute + 1440 < closes:
                return previous, minute + 1440, (opens, closes)
        return None

    @property
    def fact_refs(self) -> tuple[str, ...]:
        return _dedupe([self.hours_ref, *self.closure_refs])

    @property
    def rule_refs(self) -> tuple[str, ...]:
        """The facts availability actually depends on (checked for disputes)."""
        return _dedupe([self.weekly_ref, *self.closure_refs])


def _lines(weekly: Sequence[JsonObj]) -> tuple[tuple[frozenset[str], int, int], ...]:
    return tuple((frozenset(line["days"]), parse_hhmm(line["open"]), parse_hhmm(line["close"])) for line in weekly)


def _closures(entries: Sequence[JsonObj]) -> tuple[tuple[date, date], ...]:
    out = []
    for entry in entries:
        first = date.fromisoformat(entry["from"])
        out.append((first, date.fromisoformat(entry["to"]) if entry.get("to") else first))
    return tuple(out)


def _location(profile: Profile, location_id: str) -> JsonObj | None:
    try:
        return profile.location(location_id)
    except NotFound:
        return None


def module_location(profile: Profile, module: JsonObj, location_id: str | None = None) -> JsonObj | None:
    """The location whose hours/closures back a module: an explicit (session) location, else the first
    fulfilment location with hours, else the primary storefront."""
    if location_id:
        found = _location(profile, location_id)
        if found is not None:
            return found
    for candidate in (module.get("fulfillment") or {}).get("location_ids", ()) or ():
        found = _location(profile, candidate)
        if found is not None and found.get("hours"):
            return found
    return profile.primary_location()


def schedule_for(profile: Profile, module_id: str, module: JsonObj, location_id: str | None = None) -> Schedule:
    """Weekly lines (module, or location fallback) plus module and location closures."""
    availability = module.get("availability") or {}
    location = module_location(profile, module, location_id)
    hours = (location or {}).get("hours") or {}
    loc_id = location.get("id") if location else None
    weekly_ref: str | None
    if availability.get("weekly"):
        lines, hours_ref = _lines(availability["weekly"]), f"{module_id}#availability.weekly"
        weekly_ref = hours_ref
    elif hours.get("weekly"):
        lines, hours_ref = _lines(hours["weekly"]), f"locations/{loc_id}#hours"
        weekly_ref = f"locations/{loc_id}#hours.weekly"
    else:
        lines, hours_ref, weekly_ref = (), None, None
    closures: list[tuple[date, date]] = []
    closure_refs: list[str] = []
    if availability.get("closures"):
        closures.extend(_closures(availability["closures"]))
        closure_refs.append(f"{module_id}#availability.closures")
    if hours.get("closures"):
        closures.extend(_closures(hours["closures"]))
        closure_refs.append(f"locations/{loc_id}#hours.closures")
    return Schedule(lines, tuple(closures), hours_ref, tuple(closure_refs), loc_id, weekly_ref)


# ---------------------------------------------------------------------------- booking rules


@dataclass(frozen=True)
class _BookingRules:
    business_id: str
    module_id: str
    module: JsonObj
    item_ref: str
    tz: ZoneInfo
    schedule: Schedule
    interval: bool
    step: int
    cell: int
    """Capacity cell length in minutes (see `cell_minutes`); equals `step` unless that is unsafe."""
    duration: int | None
    occupancy: int
    duration_ref: str | None
    resources: tuple[JsonObj, ...]
    sessions: tuple[JsonObj, ...]
    party_min: int
    party_max: int | None
    capacity_per_slot: int | None
    capacity_per_day: int | None
    lead_minutes: int
    max_advance_minutes: int | None
    fact_refs: tuple[str, ...]


@dataclass(frozen=True)
class _Option:
    slot: SlotOption
    holds: tuple[CapacityHold, ...]


def item_duration(
    item_ref: str, item: JsonObj, choices: Mapping[str, str] | None
) -> tuple[int | None, str | None]:
    """Minutes of service for an item as configured, and the fact ref they come from."""
    selected = list(selected_choices(item_ref, item, choices))
    for sel in reversed(selected):
        if sel.choice.get("duration_minutes"):
            return int(sel.choice["duration_minutes"]), f"{sel.ref}#duration_minutes"
    if item.get("duration_minutes"):
        return int(item["duration_minutes"]), f"{item_ref}#duration_minutes"
    best: tuple[int, str] | None = None
    answered = set((choices or {}).keys())
    for group in item.get("options", ()) or ():
        if group["id"] in answered:
            continue
        for choice in group.get("choices", ()):
            minutes = choice.get("duration_minutes")
            if minutes and (best is None or minutes > best[0]):
                best = (int(minutes), f"{item_ref}/{group['id']}/{choice['id']}#duration_minutes")
    return best if best is not None else (None, None)


def works_on(resource: JsonObj, day: date) -> bool:
    """False if the resource's `works_days` attribute excludes `day`."""
    days = (resource.get("attributes") or {}).get("works_days")
    return not isinstance(days, list) or weekday_token(day) in days


# ---------------------------------------------------------------------------- day rules


@dataclass(frozen=True)
class _DayRules:
    business_id: str
    module_id: str
    tz: ZoneInfo
    schedule: Schedule
    lead_minutes: int
    max_advance_minutes: int | None
    capacity_per_day: int | None
    fact_refs: tuple[str, ...]


class AvailabilityEngine:
    """Pure rule evaluation plus capacity filtering. Stateless apart from the injected clock."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    # ---- booking modules ----------------------------------------------------------------------

    def booking_candidates(
        self,
        profile: Profile,
        module_id: str,
        item_ref: str,
        *,
        date_from: date,
        date_to: date,
        party_size: int = 1,
        resource_id: str | None = None,
        choices: Mapping[str, str] | None = None,
    ) -> list[SlotWithHolds]:
        """Every rule-valid slot in [date_from, date_to] with the holds booking it would take.

        Ignores current usage (see `open_booking_slots`). Ordered by start time; with several eligible
        resources each start appears once per resource (profile order). An unknown module or item, or
        a disputed rule, yields no candidates. The range is clamped (`search_range`): never before
        today, at most `MAX_SEARCH_DAYS` days, never past the module's max advance.
        """
        rules = self._booking_rules(profile, module_id, item_ref, choices)
        if isinstance(rules, PlanProblem):
            return []
        now = self._clock.now()
        bounds = search_range(
            now.astimezone(rules.tz).date(), date_from, date_to, horizon_date(now, rules.max_advance_minutes, rules.tz)
        )
        if bounds is None:
            return []
        out: list[SlotWithHolds] = []
        for day in _days(*bounds):
            for start, session in self._starts(rules, day):
                options = self._evaluate(rules, start, party_size, resource_id, now, session)
                if not isinstance(options, PlanProblem):
                    out.extend((option.slot, option.holds) for option in options)
        return out

    def booking_options(
        self,
        profile: Profile,
        module_id: str,
        item_ref: str,
        start: datetime,
        *,
        party_size: int = 1,
        resource_id: str | None = None,
        choices: Mapping[str, str] | None = None,
    ) -> list[SlotWithHolds] | PlanProblem:
        """Every rule-valid way to book `start`: one entry per eligible resource that works that day
        (profile order), or just the requested resource. Problems as in `check_booking`."""
        rules = self._booking_rules(profile, module_id, item_ref, choices)
        if isinstance(rules, PlanProblem):
            return rules
        if start.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        now = self._clock.now()
        if not is_existing_local(start, rules.tz):
            problem = _problem(
                "not_on_schedule", "that local time does not exist (clock change)", field="start"
            )
            return self._with_booking_alternatives(problem, rules, start, party_size, resource_id, now)
        session = self._session_at(rules, start.astimezone(rules.tz))
        result = self._evaluate(rules, start, party_size, resource_id, now, session)
        if isinstance(result, PlanProblem):
            return self._with_booking_alternatives(result, rules, start, party_size, resource_id, now)
        return [(option.slot, option.holds) for option in result]

    def check_booking(
        self,
        profile: Profile,
        module_id: str,
        item_ref: str,
        start: datetime,
        *,
        party_size: int = 1,
        resource_id: str | None = None,
        choices: Mapping[str, str] | None = None,
    ) -> SlotWithHolds | PlanProblem:
        """Validate one requested start (local, tz-aware) against the rules (not capacity).

        Problems: closed, outside_hours, not_on_schedule, lead_time, too_far_ahead, in_the_past,
        party_size_out_of_range, not_offered (plus unknown_module / unknown_item for bad refs). Each
        problem carries up to 3 rule-valid alternatives (capacity is not consulted here).
        """
        options = self.booking_options(
            profile, module_id, item_ref, start, party_size=party_size, resource_id=resource_id, choices=choices
        )
        return options if isinstance(options, PlanProblem) else options[0]

    async def open_booking_slots(
        self,
        profile: Profile,
        module_id: str,
        item_ref: str,
        capacity: CapacityStore,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        party_size: int = 1,
        resource_id: str | None = None,
        limit: int = 6,
        choices: Mapping[str, str] | None = None,
        not_before: datetime | None = None,
    ) -> list[SlotWithHolds]:
        """Candidates minus full ones, with `remaining` filled in.

        One slot per start time: the requested resource, or the first free eligible one. `not_before`
        drops earlier starts (used to offer alternatives after a requested time). The range is
        clamped like `booking_candidates` (default: two weeks from `date_from` or today) and searched
        a week at a time (one `usage` batch read per week), stopping once `limit` slots are found.
        """
        today = self._clock.now().astimezone(profile.timezone).date()
        bounds = search_range(today, date_from, date_to)
        if bounds is None:
            return []
        out: list[SlotWithHolds] = []
        seen: set[datetime] = set()
        for first, last in _chunks(*bounds):
            candidates = self.booking_candidates(
                profile,
                module_id,
                item_ref,
                date_from=first,
                date_to=last,
                party_size=party_size,
                resource_id=resource_id,
                choices=choices,
            )
            if not_before is not None:
                candidates = [c for c in candidates if c[0].window is not None and c[0].window.start >= not_before]
            if not candidates:
                continue
            usage = await capacity.usage(sorted({hold.key for _, holds in candidates for hold in holds}))
            groups: dict[datetime, list[SlotWithHolds]] = {}
            for candidate in candidates:
                assert candidate[0].window is not None
                groups.setdefault(candidate[0].window.start, []).append(candidate)
            for start, group in groups.items():
                free = [c for c in group if fits(c[1], usage)]
                if not free or start in seen:
                    continue
                seen.add(start)
                slot, holds = free[0]
                remaining = len(free) if len(group) > 1 else _remaining(holds, usage)
                out.append((slot.model_copy(update={"remaining": remaining}), holds))
                if len(out) >= limit:
                    return out
        return out

    # ---- day-granular modules (custom_order, quote_request) -----------------------------------------

    def day_candidates(
        self, profile: Profile, module_id: str, *, date_from: date, date_to: date
    ) -> list[SlotWithHolds]:
        """Days on which the module accepts a fulfilment date (open, not closed, lead/max advance ok).

        Lead time is measured to the day's first opening time (an order for Saturday with 48h lead
        time must be placed 48h before Saturday's opening). The range is clamped like
        `booking_candidates`; a disputed rule yields no days.
        """
        rules = self._day_rules(profile, module_id)
        if isinstance(rules, PlanProblem):
            return []
        now = self._clock.now()
        bounds = search_range(
            now.astimezone(rules.tz).date(), date_from, date_to, horizon_date(now, rules.max_advance_minutes, rules.tz)
        )
        if bounds is None:
            return []
        out: list[SlotWithHolds] = []
        for day in _days(*bounds):
            result = self._evaluate_day(rules, day, now)
            if not isinstance(result, PlanProblem):
                out.append(result)
        return out

    def check_day(
        self, profile: Profile, module_id: str, day: date
    ) -> SlotWithHolds | PlanProblem:
        """Validate one fulfilment day. Problems: in_the_past, closed, lead_time, too_far_ahead (plus
        unknown_module), each with up to 3 rule-valid alternative days."""
        rules = self._day_rules(profile, module_id)
        if isinstance(rules, PlanProblem):
            return rules
        now = self._clock.now()
        result = self._evaluate_day(rules, day, now)
        if not isinstance(result, PlanProblem):
            return result
        today = now.astimezone(rules.tz).date()
        start = today if result.code in ("too_far_ahead", "in_the_past") else max(day + timedelta(days=1), today)
        alternatives = []
        for candidate in _days(start, start + timedelta(days=ALTERNATIVE_SEARCH_DAYS - 1)):
            found = self._evaluate_day(rules, candidate, now)
            if not isinstance(found, PlanProblem):
                alternatives.append(found[0])
                if len(alternatives) == MAX_ALTERNATIVES:
                    break
        return result.model_copy(update={"alternatives": tuple(alternatives)})

    async def open_days(
        self,
        profile: Profile,
        module_id: str,
        capacity: CapacityStore,
        *,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 6,
    ) -> list[SlotWithHolds]:
        """Rule-valid days minus full ones, with `remaining` filled in (clamped and searched a week at a
        time like `open_booking_slots`)."""
        today = self._clock.now().astimezone(profile.timezone).date()
        bounds = search_range(today, date_from, date_to)
        if bounds is None:
            return []
        out: list[SlotWithHolds] = []
        for first, last in _chunks(*bounds):
            candidates = self.day_candidates(profile, module_id, date_from=first, date_to=last)
            if not candidates:
                continue
            usage = await capacity.usage(sorted({hold.key for _, holds in candidates for hold in holds}))
            for slot, holds in candidates:
                if fits(holds, usage):
                    out.append((slot.model_copy(update={"remaining": _remaining(holds, usage)}), holds))
                    if len(out) >= limit:
                        return out
        return out

    def rules_problem(
        self, profile: Profile, module_id: str, item_ref: str | None = None, choices: Mapping[str, str] | None = None
    ) -> PlanProblem | None:
        """The `disputed_fact` problem when a rule the module (and item) relies on is disputed, else None.

        Callers that list availability use it to withhold ("the owner is double-checking") instead of
        saying nothing is open. Other rule problems (unknown module or item) are not reported here.
        """
        try:
            module_type = profile.module_type(module_id)
        except UnknownModule:
            return None
        if module_type == "booking" and item_ref is not None:
            rules: object = self._booking_rules(profile, module_id, item_ref, choices)
        else:
            rules = self._day_rules(profile, module_id)
        if isinstance(rules, PlanProblem) and rules.code == "disputed_fact":
            return rules
        return None

    async def is_free(self, holds: tuple[CapacityHold, ...], capacity: CapacityStore) -> str | None:
        """None if every hold fits under its limit right now, else the first full key (advisory only:
        the authoritative check is the conditional write in `CommitmentStore.commit`)."""
        if not holds:
            return None
        usage = await capacity.usage(sorted({hold.key for hold in holds}))
        return _first_full(holds, usage)

    # ---- internals: booking -------------------------------------------------------------------

    def _booking_rules(
        self, profile: Profile, module_id: str, item_ref: str, choices: Mapping[str, str] | None
    ) -> _BookingRules | PlanProblem:
        try:
            module = profile.module(module_id)
        except UnknownModule:
            return _problem("unknown_module", f"no module {module_id!r}", field="module_id")
        if module.get("type") != "booking":
            return _problem("unknown_module", f"module {module_id!r} does not take bookings", field="module_id")
        try:
            catalog_id, item = profile.item(item_ref)
        except UnknownItem:
            return _problem("unknown_item", "no such item", field="item")
        ref = profile.item_ref(catalog_id, item)
        service = next((s for s in module.get("services", ()) or () if s.get("item") == ref), None)
        slotting = module.get("slotting") or {}
        interval = slotting.get("mode", "interval") == "interval"
        sessions = tuple(s for s in slotting.get("sessions", ()) or () if s.get("item") == ref)
        if service is None and not sessions:
            return _problem("not_offered", f"{ref} is not booked through {module_id}", field="item")
        if not interval and not sessions:
            return _problem("not_offered", f"no scheduled sessions for {ref}", field="item")
        service = service or {}
        step = int(slotting.get("step_minutes") or 15) if interval else 0
        duration, duration_ref = item_duration(ref, item, choices)
        buffer = int(service.get("buffer_minutes") or 0)
        occupancy = (duration or step) + buffer
        if interval:
            occupancy = max(step, math.ceil(occupancy / step) * step)
        all_resources = tuple(module.get("resources", ()) or ())
        wanted = service.get("resource_ids")
        resources = tuple(r for r in all_resources if wanted is None or r["id"] in wanted)
        party = service.get("party_size") or {}
        availability = module.get("availability") or {}
        schedule = schedule_for(profile, module_id, module)
        rule_refs = [
            f"{module_id}#slotting.step_minutes" if interval else None,
            duration_ref,
            f"{module_id}#availability.lead_time_minutes" if availability.get("lead_time_minutes") else None,
            f"{module_id}#availability.max_advance_minutes" if availability.get("max_advance_minutes") else None,
        ]
        refs = [*schedule.fact_refs, *rule_refs]
        disputed = disputed_problem(
            profile,
            [
                *schedule.rule_refs,
                *rule_refs,
                f"{module_id}#availability.capacity_per_slot" if availability.get("capacity_per_slot") else None,
                f"{module_id}#availability.capacity_per_day" if availability.get("capacity_per_day") else None,
                *(f"{module_id}/{s['id']}#{f}" for s in sessions for f in ("days", "start", "capacity")),
            ],
            field="start",
        )
        if disputed is not None:
            return disputed
        cell = step
        if interval:
            opening = [opens for _days_, opens, _closes in schedule.lines]
            cell = cell_minutes(step, opening, clock_change_minutes(profile.timezone, self._clock.now().year))
        return _BookingRules(
            business_id=profile.profile_id,
            module_id=module_id,
            module=module,
            item_ref=ref,
            tz=profile.timezone,
            schedule=schedule,
            interval=interval,
            step=step,
            cell=cell,
            duration=duration,
            occupancy=occupancy,
            duration_ref=duration_ref,
            resources=resources,
            sessions=sessions,
            party_min=int(party.get("min") or 1),
            party_max=int(party["max"]) if party.get("max") else None,
            capacity_per_slot=availability.get("capacity_per_slot"),
            capacity_per_day=availability.get("capacity_per_day"),
            lead_minutes=int(availability.get("lead_time_minutes") or 0),
            max_advance_minutes=availability.get("max_advance_minutes"),
            fact_refs=_dedupe(refs),
        )

    def _starts(self, rules: _BookingRules, day: date) -> list[tuple[datetime, JsonObj | None]]:
        """Candidate local starts on `day` (grid or sessions), skipping nonexistent wall times."""
        if rules.interval:
            out: list[tuple[datetime, JsonObj | None]] = []
            for opens, closes in rules.schedule.day_lines(day):
                minute = opens
                while minute < closes:
                    start = wall_time(day, minute, rules.tz)
                    if start is not None:
                        out.append((start, None))
                    minute += rules.step
            return sorted(out, key=lambda pair: pair[0])
        token = weekday_token(day)
        sessions: list[tuple[datetime, JsonObj]] = []
        for session in rules.sessions:
            if token in session.get("days", ()):
                start = wall_time(day, parse_hhmm(session["start"]), rules.tz)
                if start is not None:
                    sessions.append((start, session))
        return [(start, session) for start, session in sorted(sessions, key=lambda pair: (pair[0], pair[1]["id"]))]

    def _session_at(self, rules: _BookingRules, local: datetime) -> JsonObj | None:
        if rules.interval:
            return None
        token = weekday_token(local.date())
        minute = local.hour * 60 + local.minute
        for session in rules.sessions:
            if token in session.get("days", ()) and parse_hhmm(session["start"]) == minute and not local.second:
                return session
        return None

    def _evaluate(
        self,
        rules: _BookingRules,
        start: datetime,
        party_size: int,
        resource_id: str | None,
        now: datetime,
        session: JsonObj | None,
    ) -> list[_Option] | PlanProblem:
        """All rule checks for one start (no alternatives, no capacity)."""
        local = start.astimezone(rules.tz)
        day = local.date()
        module_id = rules.module_id
        covering = rules.schedule.line_at(day, local.hour * 60 + local.minute) if rules.interval else None
        if covering is not None:
            day = covering[0]  # a start after midnight on a line that opened the day before
        if party_size < rules.party_min or (rules.party_max is not None and party_size > rules.party_max):
            return _problem(
                "party_size_out_of_range",
                f"party size must be between {rules.party_min} and {rules.party_max or 'any'}",
                field="party_size",
            )
        if local < now:
            return _problem("in_the_past", "that time has already passed", field="start")
        if rules.schedule.closed_on(day):
            return _problem("closed", "closed that day", field="start", fact_ref=_first(rules.schedule.closure_refs))
        if rules.interval:
            if covering is None:
                if not rules.schedule.day_lines(day):
                    return _problem("closed", "closed that day", field="start", fact_ref=rules.schedule.hours_ref)
                return _problem("outside_hours", "outside opening hours", field="start", fact_ref=rules.schedule.hours_ref)
            _day, minute, line = covering
            if local.second or local.microsecond or (minute - line[0]) % rules.step:
                return _problem(
                    "not_on_schedule",
                    f"appointments start every {rules.step} minutes",
                    field="start",
                    fact_ref=f"{module_id}#slotting.step_minutes",
                )
            end = local.astimezone(UTC) + timedelta(minutes=rules.occupancy)
            if end > boundary(day, line[1], rules.tz):
                return _problem(
                    "outside_hours", "it would end after closing time", field="start", fact_ref=rules.schedule.hours_ref
                )
        else:
            if session is None:
                return _problem("not_on_schedule", "no session starts then", field="start")
            if party_size > int(session["capacity"]):
                return _problem(
                    "party_size_out_of_range",
                    f"the session has {session['capacity']} places",
                    field="party_size",
                    fact_ref=f"{module_id}/{session['id']}#capacity",
                )
        if rules.lead_minutes and local < now + timedelta(minutes=rules.lead_minutes):
            return _problem(
                "lead_time",
                f"needs {rules.lead_minutes} minutes notice",
                field="start",
                fact_ref=f"{module_id}#availability.lead_time_minutes",
            )
        if rules.max_advance_minutes is not None and local > now + timedelta(minutes=rules.max_advance_minutes):
            return _problem(
                "too_far_ahead",
                "too far ahead to book",
                field="start",
                fact_ref=f"{module_id}#availability.max_advance_minutes",
            )

        candidates: list[JsonObj | None]
        if session is not None:
            # A fixed session is its own capacity unit; its instructor (if any) is part of the session.
            session_resource = session.get("resource_id")
            if resource_id and resource_id != session_resource:
                return _problem("not_offered", "that resource does not run this session", field="resource")
            found = next(
                (r for r in rules.module.get("resources", ()) or () if r["id"] == session_resource), None
            )
            candidates = [found]
        elif not rules.resources:
            if resource_id:
                return _problem("not_offered", "no resources to choose from", field="resource")
            candidates = [None]
        elif resource_id:
            chosen = next((r for r in rules.resources if r["id"] == resource_id), None)
            if chosen is None:
                return _problem("not_offered", "that resource does not offer this", field="resource")
            if not works_on(chosen, day):
                return _problem(
                    "not_on_schedule",
                    "that resource does not work that day",
                    field="resource",
                    fact_ref=f"{module_id}/{chosen['id']}#attributes.works_days",
                )
            candidates = [chosen]
        else:
            candidates = [r for r in rules.resources if works_on(r, day)]
            if not candidates:
                return _problem("not_on_schedule", "nobody who offers this works that day", field="start")
        return [self._option(rules, local, day, resource, session, party_size) for resource in candidates]

    def _option(
        self,
        rules: _BookingRules,
        local: datetime,
        day: date,
        resource: JsonObj | None,
        session: JsonObj | None,
        party_size: int,
    ) -> _Option:
        holds: list[CapacityHold] = []
        start_utc = local.astimezone(UTC)
        rid = resource["id"] if resource else None
        if rules.interval:
            for index in range(rules.occupancy // rules.cell):
                cell = start_utc + timedelta(minutes=index * rules.cell)
                if rid is not None:
                    holds.append(CapacityHold(key=cell_key(rules.business_id, rules.module_id, cell, rid), units=1, limit=1))
                if rid is None or rules.capacity_per_slot:
                    holds.append(
                        CapacityHold(
                            key=cell_key(rules.business_id, rules.module_id, cell, None),
                            units=1,
                            limit=int(rules.capacity_per_slot or 1),
                        )
                    )
            minutes = rules.duration if rules.duration is not None else rules.occupancy
        else:
            assert session is not None
            holds.append(
                CapacityHold(
                    key=session_key(rules.business_id, rules.module_id, session["id"], day),
                    units=party_size,
                    limit=int(session["capacity"]),
                )
            )
            minutes = rules.duration or 0
        if rules.capacity_per_day:
            holds.append(
                CapacityHold(
                    key=day_key(rules.business_id, rules.module_id, day), units=1, limit=int(rules.capacity_per_day)
                )
            )
        end = (start_utc + timedelta(minutes=minutes)).astimezone(rules.tz)
        refs = [
            *rules.fact_refs,
            f"{rules.module_id}/{session['id']}#start" if session else None,
            f"{rules.module_id}#availability.capacity_per_day" if rules.capacity_per_day else None,
            f"{rules.module_id}/{rid}#spoken_name" if rid else None,
            f"{rules.module_id}/{rid}#attributes.works_days" if resource and _has_works_days(resource) else None,
        ]
        slot = SlotOption(
            kind="time",
            window=TimeWindow(start=local, end=end),
            session_id=session["id"] if session else None,
            resource_id=rid,
            fact_refs=_dedupe(refs),
        )
        return _Option(slot=slot, holds=tuple(holds))

    def _with_booking_alternatives(
        self,
        problem: PlanProblem,
        rules: _BookingRules,
        start: datetime,
        party_size: int,
        resource_id: str | None,
        now: datetime,
    ) -> PlanProblem:
        if problem.code in ("party_size_out_of_range", "not_offered"):
            return problem
        begin = now if problem.code in ("too_far_ahead", "in_the_past") else max(start, now)
        first_day = begin.astimezone(rules.tz).date()
        alternatives: list[SlotOption] = []
        seen: set[datetime] = set()
        for day in _days(first_day, first_day + timedelta(days=ALTERNATIVE_SEARCH_DAYS - 1)):
            for candidate, session in self._starts(rules, day):
                if candidate < begin or candidate in seen:
                    continue
                options = self._evaluate(rules, candidate, party_size, resource_id, now, session)
                if isinstance(options, PlanProblem):
                    continue
                seen.add(candidate)
                alternatives.append(options[0].slot)
                if len(alternatives) == MAX_ALTERNATIVES:
                    return problem.model_copy(update={"alternatives": tuple(alternatives)})
        return problem.model_copy(update={"alternatives": tuple(alternatives)})

    # ---- internals: days ----------------------------------------------------------------------

    def _day_rules(self, profile: Profile, module_id: str) -> _DayRules | PlanProblem:
        try:
            module = profile.module(module_id)
        except UnknownModule:
            return _problem("unknown_module", f"no module {module_id!r}", field="module_id")
        if module.get("type") not in DAY_MODULE_TYPES:
            return _problem("unknown_module", f"module {module_id!r} does not take dated requests", field="module_id")
        availability = module.get("availability") or {}
        schedule = schedule_for(profile, module_id, module)
        refs = [
            *schedule.fact_refs,
            f"{module_id}#availability.lead_time_minutes" if availability.get("lead_time_minutes") else None,
            f"{module_id}#availability.max_advance_minutes" if availability.get("max_advance_minutes") else None,
            f"{module_id}#availability.capacity_per_day" if availability.get("capacity_per_day") else None,
        ]
        disputed = disputed_problem(profile, [*schedule.rule_refs, *refs[len(schedule.fact_refs) :]], field="date")
        if disputed is not None:
            return disputed
        return _DayRules(
            business_id=profile.profile_id,
            module_id=module_id,
            tz=profile.timezone,
            schedule=schedule,
            lead_minutes=int(availability.get("lead_time_minutes") or 0),
            max_advance_minutes=availability.get("max_advance_minutes"),
            capacity_per_day=availability.get("capacity_per_day"),
            fact_refs=_dedupe(refs),
        )

    def _evaluate_day(self, rules: _DayRules, day: date, now: datetime) -> SlotWithHolds | PlanProblem:
        today = now.astimezone(rules.tz).date()
        if day < today:
            return _problem("in_the_past", "that day has passed", field="date")
        lines = rules.schedule.day_lines(day)
        if rules.schedule.closed_on(day):
            return _problem("closed", "closed that day", field="date", fact_ref=_first(rules.schedule.closure_refs))
        if not lines:
            return _problem("closed", "closed that day", field="date", fact_ref=rules.schedule.hours_ref)
        first_open = boundary(day, lines[0][0], rules.tz)
        last_close = boundary(day, max(close for _, close in lines), rules.tz)
        if last_close <= now:
            return _problem("in_the_past", "already closed for the day", field="date", fact_ref=rules.schedule.hours_ref)
        if rules.lead_minutes and first_open < now + timedelta(minutes=rules.lead_minutes):
            return _problem(
                "lead_time",
                f"needs {rules.lead_minutes} minutes notice before opening",
                field="date",
                fact_ref=f"{rules.module_id}#availability.lead_time_minutes",
            )
        if rules.max_advance_minutes is not None and first_open > now + timedelta(minutes=rules.max_advance_minutes):
            return _problem(
                "too_far_ahead",
                "too far ahead",
                field="date",
                fact_ref=f"{rules.module_id}#availability.max_advance_minutes",
            )
        holds: tuple[CapacityHold, ...] = ()
        if rules.capacity_per_day:
            holds = (
                CapacityHold(
                    key=day_key(rules.business_id, rules.module_id, day), units=1, limit=int(rules.capacity_per_day)
                ),
            )
        slot = SlotOption(kind="day", day=day, window=TimeWindow(start=first_open, end=last_close), fact_refs=rules.fact_refs)
        return slot, holds


def _first(refs: Sequence[str]) -> str | None:
    return refs[0] if refs else None


def _has_works_days(resource: JsonObj) -> bool:
    return isinstance((resource.get("attributes") or {}).get("works_days"), list)


def _totals(holds: Iterable[CapacityHold]) -> dict[str, tuple[int, int]]:
    """Units and limit per key (a key listed twice needs the sum of its units)."""
    totals: dict[str, tuple[int, int]] = {}
    for hold in holds:
        units, limit = totals.get(hold.key, (0, hold.limit))
        totals[hold.key] = (units + hold.units, min(limit, hold.limit))
    return totals


def _first_full(holds: Iterable[CapacityHold], usage: Mapping[str, int]) -> str | None:
    for key, (units, limit) in _totals(holds).items():
        if usage.get(key, 0) + units > limit:
            return key
    return None


def fits(holds: Iterable[CapacityHold], usage: Mapping[str, int]) -> bool:
    """True if every hold (units summed per key) fits under its limit given `usage`."""
    return _first_full(holds, usage) is None


def _remaining(holds: Sequence[CapacityHold], usage: Mapping[str, int]) -> int | None:
    if not holds:
        return None
    return min(limit - usage.get(key, 0) for key, (_, limit) in _totals(holds).items())
