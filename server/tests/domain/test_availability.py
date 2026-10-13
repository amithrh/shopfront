"""Availability engine against the gold profiles and synthetic variants (DST, fixed sessions, buffers).

Clock: Wed 2026-10-14 16:00Z = 11:00 in Chicago (salon), 09:00 in Los Angeles (bakery).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from shopfront_server.clock import FixedClock
from shopfront_server.domain.availability import (
    AvailabilityEngine,
    cell_key,
    day_key,
    schedule_for,
    session_key,
    wall_time,
)
from shopfront_server.domain.planning import PlanProblem
from shopfront_server.domain.services.booking import parse_start
from shopfront_server.profile.factref import resolve
from shopfront_server.profile.model import Profile

pytestmark = pytest.mark.anyio

CHICAGO = ZoneInfo("America/Chicago")
LA = ZoneInfo("America/Los_Angeles")
THU = date(2026, 10, 15)
SAT = date(2026, 10, 17)
SUN = date(2026, 10, 18)
CUT = "services/womens-cut"


class CountingCapacity:
    """CapacityStore fake: fixed usage, counts batch reads."""

    def __init__(self, used: dict[str, int] | None = None) -> None:
        self.used = dict(used or {})
        self.calls = 0

    async def usage(self, keys: Sequence[str]) -> dict[str, int]:
        self.calls += 1
        return {key: self.used.get(key, 0) for key in keys}


def local(day: date, hhmm: str, tz: ZoneInfo = CHICAGO) -> datetime:
    hours, minutes = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hours, minutes, tzinfo=tz)


def starts(candidates: list[Any]) -> list[str]:
    return [slot.window.start.strftime("%H:%M") for slot, _ in candidates]


# ---------------------------------------------------------------------------- schedules


def test_schedule_uses_module_lines_or_location_hours(bakery: Profile, salon: Profile) -> None:
    cakes = schedule_for(bakery, "custom-cakes", bakery.module("custom-cakes"))
    assert cakes.hours_ref == "custom-cakes#availability.weekly"
    assert cakes.day_lines(SAT) == [(600, 840)]
    salon_hours = schedule_for(salon, "appointments", salon.module("appointments"))
    assert salon_hours.hours_ref == "locations/studio#hours"
    assert salon_hours.day_lines(THU) == [(600, 1200)]
    assert salon_hours.day_lines(date(2026, 10, 19)) == []  # Monday: no line = closed
    assert set(salon_hours.closure_refs) == {"appointments#availability.closures", "locations/studio#hours.closures"}


def test_every_emitted_fact_ref_resolves(engine: AvailabilityEngine, salon: Profile, bakery: Profile) -> None:
    refs: set[str] = set()
    for slot, _ in engine.booking_candidates(salon, "appointments", CUT, date_from=THU, date_to=SUN):
        refs.update(slot.fact_refs)
    for slot, _ in engine.day_candidates(bakery, "custom-cakes", date_from=SAT, date_to=SUN):
        refs.update(slot.fact_refs)
    for ref in sorted(refs):
        profile = salon if not ref.startswith(("custom-cakes", "locations/main")) else bakery
        assert resolve(profile, ref).present, ref


# ---------------------------------------------------------------------------- interval grid


def test_interval_grid_runs_every_step_until_the_service_fits(engine: AvailabilityEngine, salon: Profile) -> None:
    candidates = engine.booking_candidates(salon, "appointments", CUT, date_from=THU, date_to=THU, resource_id="wren")
    times = starts(candidates)
    assert times[0] == "10:00" and times[-1] == "19:00"  # 60 minutes must end by 20:00
    assert len(times) == (19 * 60 - 10 * 60) // 15 + 1
    slot, holds = candidates[0]
    assert slot.resource_id == "wren" and slot.window.end == local(THU, "11:00")
    assert [h.key for h in holds] == [
        cell_key("quillfern-salon", "appointments", datetime(2026, 10, 15, 15, m, tzinfo=UTC), "wren") for m in (0, 15, 30, 45)
    ]
    assert {(h.units, h.limit) for h in holds} == {(1, 1)}


def test_overlapping_bookings_share_cells(engine: AvailabilityEngine, salon: Profile) -> None:
    ten = engine.check_booking(salon, "appointments", CUT, local(THU, "10:00"), resource_id="wren")
    half_past = engine.check_booking(salon, "appointments", CUT, local(THU, "10:30"), resource_id="wren")
    eleven = engine.check_booking(salon, "appointments", CUT, local(THU, "11:00"), resource_id="wren")
    assert not isinstance(ten, PlanProblem) and not isinstance(half_past, PlanProblem) and not isinstance(eleven, PlanProblem)
    keys = lambda option: {h.key for h in option[1]}  # noqa: E731
    assert len(keys(ten) & keys(half_past)) == 2
    assert keys(ten).isdisjoint(keys(eleven))


async def test_open_slots_skip_taken_cells_and_fall_back_to_another_stylist(
    engine: AvailabilityEngine, salon: Profile
) -> None:
    ten = engine.check_booking(salon, "appointments", CUT, local(THU, "10:00"), resource_id="wren")
    assert not isinstance(ten, PlanProblem)
    capacity = CountingCapacity({h.key: 1 for h in ten[1]})
    wren = await engine.open_booking_slots(
        salon, "appointments", CUT, capacity, date_from=THU, date_to=THU, resource_id="wren", limit=3
    )
    assert starts(wren) == ["11:00", "11:15", "11:30"]
    anyone = await engine.open_booking_slots(salon, "appointments", CUT, capacity, date_from=THU, date_to=THU, limit=2)
    assert [(s.window.start.strftime("%H:%M"), s.resource_id, s.remaining) for s, _ in anyone] == [
        ("10:00", "june", 1),
        ("10:15", "june", 1),
    ]
    assert capacity.calls == 2  # one batch read per call


async def test_open_slots_default_to_two_weeks_from_today(engine: AvailabilityEngine, salon: Profile) -> None:
    capacity = CountingCapacity()
    slots = await engine.open_booking_slots(salon, "appointments", CUT, capacity, limit=1)
    assert slots[0][0].window.start == local(date(2026, 10, 14), "11:00")  # the first grid time not in the past
    assert capacity.calls == 1


def test_buffer_extends_the_held_cells(
    raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile], clock: FixedClock
) -> None:
    doc = raw("quillfern-salon")
    service = next(s for s in doc["modules"]["appointments"]["services"] if s["item"] == CUT)
    service["buffer_minutes"] = 10  # 60 + 10 rounds up to 75 minutes = 5 cells
    engine = AvailabilityEngine(clock)
    candidates = engine.booking_candidates(build_profile(doc), "appointments", CUT, date_from=THU, date_to=THU, resource_id="wren")
    assert starts(candidates)[-1] == "18:45"
    assert len(candidates[0][1]) == 5
    assert candidates[0][0].window.end == local(THU, "11:00")  # the customer's appointment excludes the buffer


def test_capacity_per_slot_without_resources(
    raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile], clock: FixedClock
) -> None:
    doc = raw("quillfern-salon")
    module = doc["modules"]["appointments"]
    module.pop("resources")
    for service in module["services"]:
        service.pop("resource_ids", None)
    module["availability"]["capacity_per_slot"] = 2
    slot, holds = AvailabilityEngine(clock).booking_candidates(
        build_profile(doc), "appointments", CUT, date_from=THU, date_to=THU
    )[0]
    assert slot.resource_id is None
    assert {(h.key.rsplit("#", 1)[-1], h.limit) for h in holds} == {("-", 2)}


# ---------------------------------------------------------------------------- problems & alternatives


@pytest.mark.parametrize(
    ("when", "code", "first_alternative"),
    [
        (local(THU, "08:00"), "outside_hours", local(THU, "10:00")),
        (local(THU, "19:15"), "outside_hours", local(THU, "19:15") + timedelta(days=1, hours=-10, minutes=-15)),
        (local(THU, "10:05"), "not_on_schedule", local(THU, "10:15")),
        (local(date(2026, 10, 14), "09:00"), "in_the_past", local(date(2026, 10, 14), "11:00")),
        (local(date(2026, 10, 19), "10:00"), "closed", local(date(2026, 10, 20), "10:00")),
        (local(date(2026, 11, 26), "10:00"), "closed", local(date(2026, 11, 27), "09:00")),
        (local(date(2026, 12, 15), "10:00"), "too_far_ahead", local(date(2026, 10, 14), "11:00")),
    ],
)
def test_rule_problems_carry_up_to_three_alternatives(
    engine: AvailabilityEngine, salon: Profile, when: datetime, code: str, first_alternative: datetime
) -> None:
    result = engine.check_booking(salon, "appointments", CUT, when)
    assert isinstance(result, PlanProblem)
    assert result.code == code
    assert 1 <= len(result.alternatives) <= 3
    assert result.alternatives[0].window.start == first_alternative


def test_unbookable_requests(engine: AvailabilityEngine, salon: Profile, bakery: Profile) -> None:
    cases = {
        "unknown_module": engine.check_booking(salon, "nope", CUT, local(THU, "10:00")),
        "unknown_item": engine.check_booking(salon, "appointments", "services/moonwalk", local(THU, "10:00")),
        "not_offered": engine.check_booking(salon, "appointments", CUT, local(THU, "10:00"), resource_id="nobody"),
    }
    for code, result in cases.items():
        assert isinstance(result, PlanProblem) and result.code == code
    not_booking = engine.check_booking(bakery, "custom-cakes", "menu/custom-cake", local(THU, "10:00", LA))
    assert isinstance(not_booking, PlanProblem) and not_booking.code == "unknown_module"


def test_party_size_range_comes_from_the_service(
    raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile], clock: FixedClock
) -> None:
    doc = raw("quillfern-salon")
    service = next(s for s in doc["modules"]["appointments"]["services"] if s["item"] == "services/updo")
    service["party_size"] = {"min": 1, "max": 3}
    engine = AvailabilityEngine(clock)
    profile = build_profile(doc)
    too_many = engine.check_booking(profile, "appointments", "services/updo", local(THU, "10:00"), party_size=4)
    assert isinstance(too_many, PlanProblem) and too_many.code == "party_size_out_of_range"
    assert too_many.alternatives == ()
    assert not isinstance(engine.check_booking(profile, "appointments", "services/updo", local(THU, "10:00"), party_size=3), PlanProblem)


# ---------------------------------------------------------------------------- resources & works_days


def test_works_days_limit_each_stylist(engine: AvailabilityEngine, salon: Profile) -> None:
    # Balayage is Inés only, and she works Tue, Wed, Fri and Sat.
    assert engine.booking_candidates(salon, "appointments", "services/balayage", date_from=THU, date_to=THU) == []
    friday = engine.booking_candidates(
        salon, "appointments", "services/balayage", date_from=date(2026, 10, 16), date_to=date(2026, 10, 16)
    )
    assert friday and {slot.resource_id for slot, _ in friday} == {"ines"}
    thursday = engine.check_booking(salon, "appointments", "services/balayage", local(THU, "10:00"))
    assert isinstance(thursday, PlanProblem) and thursday.code == "not_on_schedule"
    assert thursday.alternatives[0].window.start == local(date(2026, 10, 16), "09:00")
    # Sunday: only June works, so a women's cut is hers; Wren is not offered.
    sunday = engine.booking_candidates(salon, "appointments", CUT, date_from=SUN, date_to=SUN)
    assert {slot.resource_id for slot, _ in sunday} == {"june"}
    wren = engine.check_booking(salon, "appointments", CUT, local(SUN, "11:00"), resource_id="wren")
    assert isinstance(wren, PlanProblem) and wren.code == "not_on_schedule" and wren.field == "resource"
    assert wren.fact_ref == "appointments/wren#attributes.works_days"


def test_candidates_list_every_free_resource_in_profile_order(engine: AvailabilityEngine, salon: Profile) -> None:
    options = engine.booking_options(salon, "appointments", CUT, local(SAT, "10:00"))
    assert not isinstance(options, PlanProblem)
    assert [slot.resource_id for slot, _ in options] == ["ines", "wren", "june"]


# ---------------------------------------------------------------------------- fixed sessions


@pytest.fixture
def classes(raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]) -> Profile:
    doc = raw("quillfern-salon")
    doc["modules"]["classes"] = {
        "type": "booking",
        "services": [{"item": "services/blowout", "party_size": {"min": 1, "max": 6}}],
        "resources": [{"id": "june", "kind": "stylist", "spoken_name": "June"}],
        "slotting": {
            "mode": "fixed",
            "sessions": [
                {"id": "sat-styling", "item": "services/blowout", "days": ["sat"], "start": "10:00", "capacity": 4},
                {"id": "sun-styling", "item": "services/blowout", "days": ["sun"], "start": "12:00", "capacity": 4, "resource_id": "june"},
            ],
        },
        "availability": {},
        "intake_fields": [{"key": "customer_name", "label": "Name", "type": "text", "required": True}],
        "confirmation": {"before_commit": True, "idempotency": "required", "read_back": ["item", "when", "party_size"]},
    }
    return build_profile(doc)


async def test_fixed_sessions_and_seats(engine: AvailabilityEngine, classes: Profile) -> None:
    candidates = engine.booking_candidates(classes, "classes", "services/blowout", date_from=SAT, date_to=SUN, party_size=3)
    assert [(slot.session_id, slot.window.start, slot.resource_id) for slot, _ in candidates] == [
        ("sat-styling", local(SAT, "10:00"), None),
        ("sun-styling", local(SUN, "12:00"), "june"),
    ]
    slot, holds = candidates[0]
    assert holds[0].key == session_key("quillfern-salon", "classes", "sat-styling", SAT)
    assert (holds[0].units, holds[0].limit) == (3, 4)
    assert slot.window.end == local(SAT, "10:45")  # blowout is 45 minutes
    capacity = CountingCapacity({holds[0].key: 3})
    two = await engine.open_booking_slots(classes, "classes", "services/blowout", capacity, date_from=SAT, date_to=SAT, party_size=2)
    one = await engine.open_booking_slots(classes, "classes", "services/blowout", capacity, date_from=SAT, date_to=SAT, party_size=1)
    assert two == [] and one[0][0].remaining == 1
    off_schedule = engine.check_booking(classes, "classes", "services/blowout", local(SAT, "11:00"))
    assert isinstance(off_schedule, PlanProblem) and off_schedule.code == "not_on_schedule"
    assert off_schedule.alternatives[0].session_id == "sun-styling"
    crowd = engine.check_booking(classes, "classes", "services/blowout", local(SAT, "10:00"), party_size=5)
    assert isinstance(crowd, PlanProblem) and crowd.code == "party_size_out_of_range"
    wrong_host = engine.check_booking(classes, "classes", "services/blowout", local(SUN, "12:00"), resource_id="wren")
    assert isinstance(wrong_host, PlanProblem) and wrong_host.code == "not_offered"


# ---------------------------------------------------------------------------- day-granular modules


def test_day_lead_time_is_measured_to_opening(bakery: Profile, clock: FixedClock) -> None:
    engine = AvailabilityEngine(clock)
    days = [slot.day for slot, _ in engine.day_candidates(bakery, "custom-cakes", date_from=date(2026, 10, 14), date_to=date(2026, 10, 20))]
    assert days == [SAT, SUN, date(2026, 10, 20)]  # Fri is < 72h before 10:00; Mon is closed
    friday = engine.check_day(bakery, "custom-cakes", date(2026, 10, 16))
    assert isinstance(friday, PlanProblem) and friday.code == "lead_time"
    assert friday.fact_ref == "custom-cakes#availability.lead_time_minutes"
    assert [a.day for a in friday.alternatives] == [SAT, SUN, date(2026, 10, 20)]
    slot, holds = engine.check_day(bakery, "custom-cakes", SAT)  # type: ignore[misc]
    assert slot.window.start == local(SAT, "10:00", LA) and slot.window.end == local(SAT, "14:00", LA)
    assert [(h.key, h.units, h.limit) for h in holds] == [(day_key("thimbleberry-bakehouse", "custom-cakes", SAT), 1, 2)]

    clock.set(datetime(2026, 10, 14, 17, 0, tzinfo=UTC))  # exactly 72h before Saturday 10:00 PDT
    assert not isinstance(engine.check_day(bakery, "custom-cakes", SAT), PlanProblem)
    clock.advance(timedelta(minutes=1))
    late = engine.check_day(bakery, "custom-cakes", SAT)
    assert isinstance(late, PlanProblem) and late.code == "lead_time"


def test_day_problems(engine: AvailabilityEngine, bakery: Profile) -> None:
    past = engine.check_day(bakery, "custom-cakes", date(2026, 10, 13))
    monday = engine.check_day(bakery, "custom-cakes", date(2026, 10, 19))
    thanksgiving = engine.check_day(bakery, "custom-cakes", date(2026, 11, 26))
    assert isinstance(past, PlanProblem) and past.code == "in_the_past"
    assert isinstance(monday, PlanProblem) and monday.code == "closed"
    assert monday.fact_ref == "custom-cakes#availability.weekly"
    assert [a.day for a in monday.alternatives] == [date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)]
    assert isinstance(thanksgiving, PlanProblem) and thanksgiving.code == "closed"
    assert thanksgiving.fact_ref == "custom-cakes#availability.closures"
    assert engine.day_candidates(bakery, "nope", date_from=SAT, date_to=SUN) == []
    bad = engine.check_day(bakery, "menu", SAT)
    assert isinstance(bad, PlanProblem) and bad.code == "unknown_module"


def test_location_closures_apply_even_without_module_closures(
    raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile], clock: FixedClock
) -> None:
    doc = raw("thimbleberry-bakehouse")
    doc["modules"]["custom-cakes"]["availability"].pop("closures")
    profile = build_profile(doc)
    clock.set(datetime(2026, 11, 1, 12, 0, tzinfo=UTC))
    result = AvailabilityEngine(clock).check_day(profile, "custom-cakes", date(2026, 11, 26))
    assert isinstance(result, PlanProblem) and result.code == "closed"
    assert result.fact_ref == "locations/main#hours.closures"


def test_location_hours_are_the_fallback_for_day_modules(engine: AvailabilityEngine, home: Profile) -> None:
    # Kettlewick's quote module has no availability block: the core service area's hours apply.
    days = [slot.day for slot, _ in engine.day_candidates(home, "quotes", date_from=date(2026, 10, 14), date_to=date(2026, 10, 19))]
    assert days == [date(2026, 10, 14), date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 19)]
    slot, holds = engine.check_day(home, "quotes", date(2026, 10, 17))  # type: ignore[misc]
    assert slot.fact_refs[0] == "locations/core-area#hours" and holds == ()


async def test_open_days_subtract_per_day_capacity(engine: AvailabilityEngine, bakery: Profile) -> None:
    capacity = CountingCapacity({day_key("thimbleberry-bakehouse", "custom-cakes", SAT): 2, day_key("thimbleberry-bakehouse", "custom-cakes", SUN): 1})
    days = await engine.open_days(bakery, "custom-cakes", capacity, limit=3)
    assert [(slot.day, slot.remaining) for slot, _ in days] == [(SUN, 1), (date(2026, 10, 20), 2), (date(2026, 10, 21), 2)]
    assert capacity.calls == 2  # one batch read per week searched (Oct 14-20, then Oct 21-27)
    _, holds = engine.check_day(bakery, "custom-cakes", SAT)  # type: ignore[misc]
    assert await engine.is_free(holds, capacity) == day_key("thimbleberry-bakehouse", "custom-cakes", SAT)
    assert await engine.is_free((), capacity) is None


# ---------------------------------------------------------------------------- DST (America/Chicago)


@pytest.fixture
def round_the_clock(raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]) -> Profile:
    """A 24-hour, 30-minute-grid barber module with no resources and no booking horizon."""
    doc = raw("quillfern-salon")
    module = doc["modules"]["appointments"]
    module.pop("resources")
    module["services"] = [{"item": "services/mens-cut"}]
    module["slotting"] = {"mode": "interval", "step_minutes": 30}
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    module["availability"] = {"weekly": [{"days": days, "open": "00:00", "close": "24:00"}], "provenance": {}}
    return build_profile(doc)


def test_fall_back_week(round_the_clock: Profile, clock: FixedClock) -> None:
    clock.set(datetime(2026, 10, 25, 12, 0, tzinfo=UTC))
    engine = AvailabilityEngine(clock)
    week = engine.booking_candidates(
        round_the_clock, "appointments", "services/mens-cut", date_from=date(2026, 10, 26), date_to=date(2026, 11, 1)
    )
    per_day: dict[date, int] = {}
    for slot, _ in week:
        per_day[slot.window.start.date()] = per_day.get(slot.window.start.date(), 0) + 1
    assert all(count == 48 for count in per_day.values()), per_day
    sunday = [slot.window.start for slot, _ in week if slot.window.start.date() == date(2026, 11, 1)]
    utc = [s.astimezone(UTC).strftime("%H:%M") for s in sunday]
    assert utc[:6] == ["05:00", "05:30", "06:00", "06:30", "08:00", "08:30"]  # 01:00/01:30 CDT, then 02:00 CST
    assert len(set(sunday)) == len(sunday)
    assert len({h.key for _, holds in week for h in holds}) == len(week)  # one cell each, all distinct
    # An ambiguous request resolves to the first occurrence (CDT).
    start, problem, _ = parse_start("2026-11-01T01:30", CHICAGO)
    assert problem is None and start is not None and start.astimezone(UTC) == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert not isinstance(engine.check_booking(round_the_clock, "appointments", "services/mens-cut", start), PlanProblem)


def test_spring_forward_week(round_the_clock: Profile, clock: FixedClock) -> None:
    clock.set(datetime(2027, 3, 7, 12, 0, tzinfo=UTC))
    engine = AvailabilityEngine(clock)
    week = engine.booking_candidates(
        round_the_clock, "appointments", "services/mens-cut", date_from=date(2027, 3, 8), date_to=date(2027, 3, 14)
    )
    sunday = [slot.window.start for slot, _ in week if slot.window.start.date() == date(2027, 3, 14)]
    assert len(week) == 6 * 48 + 46
    assert [s.strftime("%H:%M") for s in sunday[2:6]] == ["01:00", "01:30", "03:00", "03:30"]  # 02:00 and 02:30 never exist
    assert wall_time(date(2027, 3, 14), 150, CHICAGO) is None
    start, problem, _ = parse_start("2027-03-14T02:30", CHICAGO)
    assert problem is None and start is not None
    result = engine.check_booking(round_the_clock, "appointments", "services/mens-cut", start)
    assert isinstance(result, PlanProblem) and result.code == "not_on_schedule"
    assert result.alternatives[0].window.start.strftime("%H:%M") == "03:00"
    # The 01:30 CST cut ends at 03:00 CDT: thirty real minutes.
    last_before_gap = next(slot for slot, _ in week if slot.window.start == sunday[3])
    assert last_before_gap.window.end.astimezone(UTC) - last_before_gap.window.start.astimezone(UTC) == timedelta(minutes=30)
    assert last_before_gap.window.end.strftime("%H:%M") == "03:00"
