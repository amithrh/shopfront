"""Regression tests for confirmed review findings in the domain layer.

Each test names the finding it pins down (SEC-2/F7 unbounded search, G2/G3/G4 disputed facts in the
transactional paths, F4 cancellation notice, F8 overnight lines, F9 visit windows, F10 resource races,
F11 DST cells). Clock: Wed 2026-10-14 11:00 in Chicago unless a test moves it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any

import pytest
from support import add_dispute

from shopfront_server.clock import FixedClock
from shopfront_server.domain.availability import MAX_SEARCH_DAYS, AvailabilityEngine
from shopfront_server.domain.planning import PlanProblem
from shopfront_server.errors import DisputedFact
from shopfront_server.profile.model import Profile

pytestmark = pytest.mark.anyio

Domain = Any  # the `domain` fixture (tests/domain/conftest.py)

CUSTOMER = {"customer_name": "Sam Lee", "customer_phone": "(217) 555-0100", "customer_email": "sam@example.com"}
CAKE_CUSTOMER = {"customer_name": "Sam Lee", "customer_phone": "+12175550100"}
SMALL_CAKE = {"size": "6 inch", "flavor": "vanilla", "filling": "caramel", "frosting": "cream cheese"}
JOB = {"description": "Slow leak under the kitchen sink", "name": "Sam Lee", "contact_by": "text", "phone": "+13035550100"}


class CountingCapacity:
    """CapacityStore fake: nothing used; records how many keys each batch read asked for."""

    def __init__(self) -> None:
        self.reads: list[int] = []

    async def usage(self, keys: Sequence[str]) -> dict[str, int]:
        self.reads.append(len(keys))
        return {key: 0 for key in keys}


# ---------------------------------------------------------------------------- SEC-2 / F7: bounded search


async def test_far_date_to_is_clamped_and_cheap(engine: AvailabilityEngine, salon: Profile, bakery: Profile) -> None:
    capacity = CountingCapacity()
    started = time.perf_counter()
    slots = await engine.open_booking_slots(
        salon, "appointments", "services/womens-cut", capacity, date_from=date(2026, 10, 14), date_to=date(9999, 12, 31), limit=6
    )
    days = await engine.open_days(
        bakery, "custom-cakes", capacity, date_from=date(2026, 10, 14), date_to=date(9999, 12, 31), limit=6
    )
    assert time.perf_counter() - started < 1.0
    assert len(slots) == 6 and len(days) == 6
    assert len(capacity.reads) <= 3 and max(capacity.reads) < 2000  # a week at a time, stopping at `limit`


async def test_search_never_passes_the_range_cap_or_max_advance(engine: AvailabilityEngine, salon: Profile, bakery: Profile) -> None:
    capacity = CountingCapacity()
    # The bakery has no max advance: an unbounded range still covers at most MAX_SEARCH_DAYS days.
    days = await engine.open_days(bakery, "custom-cakes", capacity, date_to=date(2099, 12, 31), limit=12)
    assert days and max(slot.day for slot, _ in days if slot.day) < date(2026, 10, 14).fromordinal(
        date(2026, 10, 14).toordinal() + MAX_SEARCH_DAYS
    )
    # The salon books 8 weeks ahead: nothing is evaluated beyond that horizon.
    late = engine.booking_candidates(
        salon, "appointments", "services/womens-cut", date_from=date(2027, 1, 1), date_to=date(2099, 12, 31)
    )
    assert late == []
    # Dates at the edge of the calendar do not overflow (the bakery sets no max advance).
    edge = await engine.open_days(bakery, "custom-cakes", capacity, date_from=date(9999, 12, 30))
    assert [slot.day for slot, _ in edge] == [date(9999, 12, 30), date(9999, 12, 31)]


# ---------------------------------------------------------------------------- G2: disputed hours


async def test_disputed_hours_block_booking_and_listing(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    salon = build_profile(add_dispute(raw("quillfern-salon"), "locations/studio#hours.weekly"))
    plan = await domain.bookings.plan(
        salon, item="men's cut", start="2026-10-15T15:30", party_size=None, intake={"stylist": "any", **CUSTOMER}, principal="anonymous"
    )
    assert [p.code for p in plan.problems][:1] == ["disputed_fact"]
    assert plan.problems[0].fact_ref == "locations/studio#hours.weekly"
    with pytest.raises(DisputedFact) as raised:
        await domain.bookings.open_slots(salon, item="men's cut")
    assert raised.value.fact_ref == "locations/studio#hours.weekly"


async def test_disputed_closures_block_custom_orders(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    bakery = build_profile(add_dispute(raw("thimbleberry-bakehouse"), "custom-cakes#availability.closures"))
    plan = await domain.orders.plan(
        bakery, item="custom cake", choices=SMALL_CAKE, day="2026-10-18", intake={"servings": 10, **CAKE_CUSTOMER}, principal="anonymous"
    )
    assert plan.problems[0].code == "disputed_fact"
    with pytest.raises(DisputedFact):
        await domain.orders.open_days(bakery, item="custom cake")


# ---------------------------------------------------------------------------- G3: disputed prices


async def test_disputed_price_is_never_read_back_or_committed(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    bakery = build_profile(add_dispute(raw("thimbleberry-bakehouse"), "menu/custom-cake/size/6in-round#price"))
    plan = await domain.orders.plan(
        bakery, item="custom cake", choices=SMALL_CAKE, day="2026-10-18", intake={"servings": 10, **CAKE_CUSTOMER}, principal="anonymous"
    )
    assert not plan.ok
    assert (plan.problems[0].code, plan.problems[0].fact_ref) == ("disputed_fact", "menu/custom-cake/size/6in-round#price")


# ---------------------------------------------------------------------------- G4: disputed service area


@pytest.mark.parametrize("zip_code", ["80033", "80214"])
async def test_disputed_service_area_is_withheld_both_ways(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile], zip_code: str
) -> None:
    doc = raw("kettlewick-home")
    for question in doc["open_questions"]:
        if question["id"] == "q-wheat-ridge":
            question["status"] = "open"
            question.pop("answer", None)
            question["blocking"] = False
    home = build_profile(doc)
    plan = await domain.quotes.plan(
        home, item="leak repair", intake={**JOB, "zip": zip_code}, principal="anonymous", day="2026-10-16", visit_window="weekday morning"
    )
    codes = [p.code for p in plan.problems]
    assert codes[:1] == ["disputed_fact"] and "outside_service_area" not in codes
    assert plan.problems[0].fact_ref == "locations/extended-area#area.postal_codes"


# ---------------------------------------------------------------------------- F4: cancellation notice


async def test_pending_request_can_be_withdrawn_inside_the_notice_window(domain: Domain, salon: Profile) -> None:
    plan = await domain.bookings.plan(
        salon, item="root touch-up", start="2026-10-15T10:00", party_size=None, intake={"stylist": "any", **CUSTOMER}, principal="anonymous"
    )
    pending = (await domain.bookings.commit(salon, plan, task_id=None)).commitment
    assert pending is not None and pending.status == "pending_owner"
    withdraw = await domain.bookings.plan_cancel(salon, booking_id=pending.commitment_id, principal="anonymous")
    assert withdraw.ok and withdraw.say_before_confirm == ()  # no late fee on a request nobody accepted
    cancelled = await domain.bookings.cancel(salon, booking_id=pending.commitment_id, principal="anonymous")
    assert cancelled.status == "cancelled" and domain.capacity_used("CAP#quillfern-salon") == {}


# ---------------------------------------------------------------------------- F8: overnight lines


@pytest.fixture
def late_night(raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]) -> Callable[..., Profile]:
    def build(closures: list[dict[str, str]] | None = None, days: tuple[str, ...] = ("fri",)) -> Profile:
        doc = raw("quillfern-salon")
        module = doc["modules"]["appointments"]
        module.pop("resources")
        module["services"] = [{"item": "services/mens-cut"}]
        module["slotting"] = {"mode": "interval", "step_minutes": 30}
        module["availability"] = {"weekly": [{"days": list(days), "open": "20:00", "close": "02:00"}], "provenance": {}}
        if closures:
            module["availability"]["closures"] = closures
        return build_profile(doc)

    return build


def test_overnight_line_offers_and_accepts_starts_after_midnight(engine: AvailabilityEngine, late_night: Callable[..., Profile]) -> None:
    profile = late_night()
    tz = profile.timezone
    starts = [
        slot.window.start.strftime("%a %H:%M")
        for slot, _ in engine.booking_candidates(
            profile, "appointments", "services/mens-cut", date_from=date(2026, 10, 16), date_to=date(2026, 10, 17)
        )
    ]
    assert starts[-4:] == ["Sat 00:00", "Sat 00:30", "Sat 01:00", "Sat 01:30"]
    after_midnight = datetime(2026, 10, 17, 0, 30, tzinfo=tz)
    assert not isinstance(engine.check_booking(profile, "appointments", "services/mens-cut", after_midnight), PlanProblem)
    # A Saturday closure does not close Friday night; a Friday closure does.
    saturday_closed = late_night([{"from": "2026-10-17"}], days=("fri", "sat"))
    assert not isinstance(engine.check_booking(saturday_closed, "appointments", "services/mens-cut", after_midnight), PlanProblem)
    friday_closed = late_night([{"from": "2026-10-16"}])
    problem = engine.check_booking(friday_closed, "appointments", "services/mens-cut", after_midnight)
    assert isinstance(problem, PlanProblem) and problem.code == "closed"


# ---------------------------------------------------------------------------- F9: visit windows


async def test_visit_window_must_fit_the_days_hours(domain: Domain, home: Profile) -> None:
    saturday = await domain.quotes.plan(
        home, item="leak repair", intake={**JOB, "zip": "80202"}, principal="anonymous", day="2026-10-17", visit_window="weekday afternoon"
    )
    assert [p.code for p in saturday.problems] == ["outside_hours"]
    morning = await domain.quotes.plan(
        home, item="leak repair", intake={**JOB, "zip": "80202"}, principal="anonymous", day="2026-10-17", visit_window="saturday morning"
    )
    assert morning.ok


# ---------------------------------------------------------------------------- F10: resource races


async def test_lost_race_for_an_auto_assigned_stylist_takes_the_next_one(domain: Domain, salon: Profile) -> None:
    async def plan(stylist: str, name: str) -> Any:
        intake = {"stylist": stylist, **CUSTOMER, "customer_name": name}
        return await domain.bookings.plan(
            salon, item="women's cut", start="2026-10-15T10:00", party_size=None, intake=intake, principal="anonymous"
        )

    alice = await plan("any", "Alice")
    bob = await plan("wren", "Bob")
    assert alice.resource_id == bob.resource_id == "wren" and not alice.resource_requested and bob.resource_requested
    assert (await domain.bookings.commit(salon, bob, task_id=None)).commitment is not None
    won = await domain.bookings.commit(salon, alice, task_id=None)
    assert won.commitment is not None and won.commitment.resource_id == "june"
    # A customer who asked for Wren by name is told the time is taken, with June at 10:00 offered.
    carol = await plan("wren", "Carol")
    lost = await domain.bookings.commit(salon, carol.model_copy(update={"problems": ()}), task_id=None)
    assert lost.commitment is None and lost.problem is not None and lost.problem.code == "capacity_full"


# ---------------------------------------------------------------------------- F11: DST cells


async def test_overlapping_bookings_across_a_dst_jump_share_a_cell(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile], clock: FixedClock
) -> None:
    doc = raw("quillfern-salon")
    module = doc["modules"]["appointments"]
    module.pop("resources")
    module["services"] = [{"item": "services/womens-cut"}]
    module["slotting"] = {"mode": "interval", "step_minutes": 45}
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    module["availability"] = {"weekly": [{"days": days, "open": "00:00", "close": "24:00"}], "provenance": {}}
    profile = build_profile(doc)
    clock.set(datetime(2027, 3, 13, 12, 0, tzinfo=UTC))
    intake = {"stylist": "any", **CUSTOMER}
    first = await domain.bookings.plan(
        profile, item="women's cut", start="2027-03-14T01:30", party_size=None, intake=intake, principal="anonymous"
    )
    second = await domain.bookings.plan(
        profile, item="women's cut", start="2027-03-14T03:00", party_size=None, intake=intake, principal="someone-else"
    )
    assert first.ok and second.ok
    assert {h.key for h in first.holds} & {h.key for h in second.holds}  # 07:30Z-09:00Z and 08:00Z-09:30Z overlap
    assert (await domain.bookings.commit(profile, first, task_id=None)).commitment is not None
    clash = await domain.bookings.commit(profile, second, task_id=None)
    assert clash.commitment is None and clash.problem is not None and clash.problem.code == "capacity_full"
