"""BookingService against the gold salon: plans, the atomic commit, replays, races and cancellation.

Clock: Wed 2026-10-14 11:00 in Chicago. Thursday: Wren and June work; Friday: Inés and Wren.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import anyio
import pytest

from askonce_server.domain.planning import CommitOutcome, CommitPlan
from askonce_server.errors import InvalidState, NotFound, UnknownItem
from askonce_server.profile.factref import resolve
from askonce_server.profile.model import Profile

pytestmark = pytest.mark.anyio

Domain = Any  # the `domain` fixture (tests/domain/conftest.py); conftest modules are not importable

CHICAGO = ZoneInfo("America/Chicago")
CUSTOMER = {"customer_name": "Sam Lee", "customer_phone": "(217) 555-0100", "customer_email": "Sam@Example.com"}


def details(stylist: str = "any", **extra: Any) -> dict[str, Any]:
    return {"stylist": stylist, **CUSTOMER, **extra}


def assert_refs_resolve(profile: Profile, refs: Iterable[str]) -> None:
    for ref in refs:
        assert resolve(profile, ref).present, ref


async def plan_cut(domain: Domain, salon: Profile, start: str | None = "2026-10-15T10:00", **kwargs: Any) -> CommitPlan:
    args: dict[str, Any] = {"item": "women's cut", "start": start, "party_size": None, "intake": details(), "principal": "anonymous"}
    args.update(kwargs)
    return await domain.bookings.plan(salon, **args)


async def test_plan_is_complete_grounded_and_deterministic(domain: Domain, salon: Profile) -> None:
    plan = await plan_cut(domain, salon)
    again = await plan_cut(domain, salon)
    assert plan == again
    assert plan.ok and plan.problems == () and plan.missing == ()
    assert (plan.kind, plan.operation, plan.module_id, plan.item_ref) == ("booking", "book", "appointments", "services/womens-cut")
    assert plan.resource_id == "wren"  # Inés is off on Thursdays; Wren is first in profile order
    assert plan.window is not None and plan.window.start == datetime(2026, 10, 15, 10, 0, tzinfo=CHICAGO)
    assert plan.intake == {
        "stylist": "any",
        "customer_name": "Sam Lee",
        "customer_phone": "+12175550100",
        "customer_email": "sam@example.com",
    }
    assert [item.key for item in plan.read_back] == ["item", "stylist", "when", "price"]
    assert plan.say_before_confirm == ("info/cancellation", "info/email-confirmation")
    assert plan.approval.required is False and len(plan.holds) == 4
    assert plan.price is not None and plan.price.amount_cents == 7800
    assert_refs_resolve(salon, plan.fact_refs)
    assert_refs_resolve(salon, (ref for item in plan.read_back for ref in item.fact_refs))


async def test_missing_intake_is_listed_and_a_named_stylist_fills_the_selector(domain: Domain, salon: Profile) -> None:
    bare = await plan_cut(domain, salon, intake={})
    assert [spec.key for spec in bare.missing] == ["stylist", "customer_name", "customer_phone", "customer_email"]
    assert not bare.ok and bare.problems == ()
    named = await plan_cut(domain, salon, intake={}, resource_id="June")
    assert [spec.key for spec in named.missing] == ["customer_name", "customer_phone", "customer_email"]
    assert named.intake["stylist"] == "june" and named.resource_id == "june"
    stylist = next(item for item in named.read_back if item.key == "stylist")
    assert stylist.fact_refs == ("appointments/june#spoken_name",)
    via_intake = await plan_cut(domain, salon, intake=details("wren"))
    assert via_intake.resource_id == "wren"


async def test_start_problems_offer_alternatives(domain: Domain, salon: Profile) -> None:
    missing = await plan_cut(domain, salon, start=None)
    assert [p.code for p in missing.problems] == ["not_on_schedule"]
    assert [a.window.start.strftime("%a %H:%M") for a in missing.problems[0].alternatives] == ["Wed 11:00", "Wed 11:15", "Wed 11:30"]
    day_only = await plan_cut(domain, salon, start="2026-10-16")
    assert day_only.problems[0].alternatives[0].window.start == datetime(2026, 10, 16, 9, 0, tzinfo=CHICAGO)
    garbled = await plan_cut(domain, salon, start="next thursday at ten")
    assert [(p.code, p.field) for p in garbled.problems] == [("invalid_intake", "start")]
    closed = await plan_cut(domain, salon, start="2026-10-19T10:00")
    assert closed.problems[0].code == "closed" and len(closed.problems[0].alternatives) == 3


async def test_unknown_and_unbookable_items(domain: Domain, salon: Profile, bakery: Profile) -> None:
    unknown = await plan_cut(domain, salon, item="moon landing")
    assert [p.code for p in unknown.problems] == ["unknown_item"] and unknown.module_id == "appointments"
    wrong_module = await plan_cut(domain, salon, module_id="info")
    assert [p.code for p in wrong_module.problems] == ["unknown_module"]
    cake = await domain.bookings.plan(
        bakery, item="custom cake", start="2026-10-17T10:00", party_size=None, intake={}, principal="anonymous"
    )
    assert [p.code for p in cake.problems] == ["not_offered"]
    nobody = await plan_cut(domain, salon, resource_id="Zelda")
    assert ("not_offered", "resource") in [(p.code, p.field) for p in nobody.problems]
    with pytest.raises(UnknownItem):
        await domain.bookings.open_slots(salon, item="moon landing")
    with pytest.raises(NotFound):
        await domain.bookings.open_slots(salon, item="women's cut", resource_id="Zelda")


async def test_option_groups_come_from_details_when_there_is_no_choices_argument(domain: Domain, salon: Profile) -> None:
    plan = await domain.bookings.plan(
        salon,
        item="balayage",
        start="2026-10-16T09:00",
        party_size=None,
        intake=details("ines", length="past the shoulder blades"),
        principal="anonymous",
    )
    assert plan.ok, plan.problems
    assert plan.choices == {"length": "past-shoulder-blades"}
    assert plan.price is not None and plan.price.amount_cents == 28500
    assert plan.window is not None and plan.window.end - plan.window.start == timedelta(minutes=240)
    assert plan.approval.required and plan.approval.owner_module_id == "owner"
    unanswered = await domain.bookings.plan(
        salon, item="balayage", start="2026-10-16T09:00", party_size=None, intake=details("ines"), principal="anonymous"
    )
    assert [(p.code, p.field) for p in unanswered.problems] == [("missing_choice", "length")]


async def test_commit_confirms_and_replays(domain: Domain, salon: Profile) -> None:
    plan = await plan_cut(domain, salon)
    first = await domain.bookings.commit(salon, plan, task_id=None)
    assert first.commitment is not None and not first.replayed
    stored = first.commitment
    assert stored.status == "confirmed" and stored.resource_id == "wren" and stored.expires_at is None
    assert stored.commitment_id == domain.ctx.ids.commitment_id(plan.idempotency_key)
    assert len(domain.capacity_used("CAP#quillfern-salon#appointments#CELL")) == 4
    assert domain.notifier.calls == []  # nothing waits for the owner

    retry_plan = await plan_cut(domain, salon)  # the slot is now Wren's: still ok because it is ours
    assert retry_plan.ok and retry_plan.idempotency_key == plan.idempotency_key
    retry = await domain.bookings.commit(salon, retry_plan, task_id=None)
    assert retry == CommitOutcome(commitment=stored, replayed=True)
    assert await domain.bookings.replay(plan) == CommitOutcome(commitment=stored, replayed=True)
    with pytest.raises(ValueError, match="not ok"):
        await domain.bookings.commit(salon, await plan_cut(domain, salon, start=None), task_id=None)


async def test_the_next_customer_gets_the_next_stylist_then_alternatives(domain: Domain, salon: Profile) -> None:
    outcomes = []
    for principal in ("ann", "bob"):
        plan = await plan_cut(domain, salon, principal=principal)
        outcomes.append(await domain.bookings.commit(salon, plan, task_id=None))
    assert [o.commitment.resource_id for o in outcomes if o.commitment] == ["wren", "june"]
    third = await plan_cut(domain, salon, principal="cy")
    assert [p.code for p in third.problems] == ["capacity_full"]
    assert [a.window.start.strftime("%H:%M") for a in third.problems[0].alternatives] == ["11:00", "11:15", "11:30"]


async def test_ten_customers_racing_for_one_slot_book_it_once(domain: Domain, salon: Profile) -> None:
    plans = [await plan_cut(domain, salon, principal=f"customer-{n}", resource_id="wren") for n in range(10)]
    assert all(plan.ok for plan in plans)
    outcomes: list[CommitOutcome] = []

    async def commit(plan: CommitPlan) -> None:
        outcomes.append(await domain.bookings.commit(salon, plan, task_id=None))

    async with anyio.create_task_group() as tg:
        for plan in plans:
            tg.start_soon(commit, plan)
    assert sum(o.commitment is not None for o in outcomes) == 1
    losers = [o for o in outcomes if o.commitment is None]
    assert len(losers) == 9 and {o.problem.code for o in losers if o.problem} == {"capacity_full"}
    assert all(o.problem and o.problem.alternatives for o in losers)
    assert len(domain.state.commitments) == 1
    assert set(domain.capacity_used("CAP#quillfern-salon").values()) == {1}


async def test_ten_identical_requests_make_one_booking(domain: Domain, salon: Profile) -> None:
    outcomes: list[CommitOutcome] = []

    async def book() -> None:
        plan = await plan_cut(domain, salon)
        outcomes.append(await domain.bookings.commit(salon, plan, task_id=None))

    async with anyio.create_task_group() as tg:
        for _ in range(10):
            tg.start_soon(book)
    ids = {o.commitment.commitment_id for o in outcomes if o.commitment}
    assert len(ids) == 1 and len(domain.state.commitments) == 1
    assert sum(not o.replayed for o in outcomes) == 1 and sum(o.replayed for o in outcomes) == 9
    assert len(domain.capacity_used("CAP#quillfern-salon")) == 4


async def test_colour_bookings_wait_for_the_owner(domain: Domain, salon: Profile, caplog: pytest.LogCaptureFixture) -> None:
    plan = await domain.bookings.plan(
        salon, item="all over color", start="2026-10-16T10:00", party_size=None, intake=details("ines"), principal="anonymous"
    )
    assert plan.ok and plan.approval.required
    outcome = await domain.bookings.commit(salon, plan, task_id="t_colour")
    assert outcome.commitment is not None
    assert (outcome.commitment.status, outcome.commitment.task_id) == ("pending_owner", "t_colour")
    assert outcome.commitment.expires_at == domain.clock.now() + timedelta(minutes=1440)
    assert domain.notifier.calls == [("quillfern-salon", outcome.commitment.commitment_id)]
    await domain.bookings.commit(salon, plan, task_id="t_colour")
    assert len(domain.notifier.calls) == 1  # a replay does not notify again

    domain.notifier.fail = True
    other = await domain.bookings.plan(
        salon, item="all over color", start="2026-10-16T13:00", party_size=None, intake=details("ines"), principal="anonymous"
    )
    with caplog.at_level(logging.ERROR, logger="askonce.domain"):
        failed_notice = await domain.bookings.commit(salon, other, task_id="t_other")
    assert failed_notice.commitment is not None and failed_notice.commitment.status == "pending_owner"
    assert "owner notification failed" in caplog.text


async def book_friday(domain: Domain, salon: Profile, principal: str = "anonymous") -> Any:
    plan = await plan_cut(domain, salon, start="2026-10-16T10:00", principal=principal)
    outcome = await domain.bookings.commit(salon, plan, task_id=None)
    assert outcome.commitment is not None
    return outcome.commitment


async def test_cancel_releases_holds_exactly_once(domain: Domain, salon: Profile) -> None:
    booking = await book_friday(domain, salon)
    neighbour = await book_friday(domain, salon, principal="neighbour")  # Inés and Wren both booked at 10:00
    assert len(domain.capacity_used("CAP#quillfern-salon")) == 8

    plan = await domain.bookings.plan_cancel(salon, booking_id=booking.commitment_id, principal="anonymous")
    assert plan.ok and plan.operation == "cancel_booking"
    assert plan.say_before_confirm == ("info/cancellation",)
    assert [item.key for item in plan.read_back] == ["item", "when"]
    assert_refs_resolve(salon, plan.fact_refs)

    cancelled = await domain.bookings.cancel(salon, booking_id=booking.commitment_id, principal="anonymous")
    assert cancelled.status == "cancelled" and cancelled.holds_released and cancelled.version == 1
    used = domain.capacity_used("CAP#quillfern-salon")
    assert len(used) == 4 and all(key.endswith(f"#{neighbour.resource_id}") for key in used)
    again = await domain.bookings.cancel(salon, booking_id=booking.commitment_id, principal="anonymous")
    assert again == cancelled  # idempotent: no second release, no version bump
    assert domain.capacity_used("CAP#quillfern-salon") == used
    replan = await domain.bookings.plan_cancel(salon, booking_id=booking.commitment_id, principal="anonymous")
    assert replan.ok  # re-cancelling replays
    reopened = await plan_cut(domain, salon, start="2026-10-16T10:00", principal="someone-else")
    assert reopened.ok and reopened.resource_id == booking.resource_id


async def test_cancellation_problems(domain: Domain, salon: Profile, clock: Any) -> None:
    thursday = await domain.bookings.commit(salon, await plan_cut(domain, salon), task_id=None)
    assert thursday.commitment is not None
    late = await domain.bookings.plan_cancel(salon, booking_id=thursday.commitment.commitment_id, principal="anonymous")
    # Inside the 24-hour notice window a cancel is allowed; the policy (with its late fee) is read back.
    assert late.ok and late.say_before_confirm == ("info/cancellation",)
    foreign = await domain.bookings.plan_cancel(salon, booking_id=thursday.commitment.commitment_id, principal="mallory")
    unknown = await domain.bookings.plan_cancel(salon, booking_id="c_nope", principal="anonymous")
    assert [p.code for p in foreign.problems] == [p.code for p in unknown.problems] == ["unknown_item"]
    with pytest.raises(NotFound):
        await domain.bookings.cancel(salon, booking_id=thursday.commitment.commitment_id, principal="mallory")
    clock.set(datetime(2026, 10, 15, 15, 30, tzinfo=UTC))  # 10:30 Thursday, after the start
    started = await domain.bookings.plan_cancel(salon, booking_id=thursday.commitment.commitment_id, principal="anonymous")
    assert [p.code for p in started.problems] == ["in_the_past"]


async def test_terminal_bookings_cannot_be_cancelled(domain: Domain, salon: Profile) -> None:
    # A colour booking waits for Inés, who declines it: there is nothing left to cancel.
    plan = await domain.bookings.plan(
        salon, item="all over color", start="2026-10-16T13:00", party_size=None, intake=details("ines"), principal="anonymous"
    )
    pending = (await domain.bookings.commit(salon, plan, task_id=None)).commitment
    assert pending is not None and pending.status == "pending_owner"
    declined = await domain.ctx.commitments.update(pending.model_copy(update={"status": "declined"}), expected_version=0)
    problem = await domain.bookings.plan_cancel(salon, booking_id=declined.commitment_id, principal="anonymous")
    assert [p.code for p in problem.problems] == ["not_offered"]
    with pytest.raises(InvalidState):
        await domain.bookings.cancel(salon, booking_id=declined.commitment_id, principal="anonymous")


async def test_open_slots_service_api(domain: Domain, salon: Profile) -> None:
    slots = await domain.bookings.open_slots(salon, item="womens cut", date_from=date(2026, 10, 15), limit=2, resource_id="June")
    assert [(s.window.start.strftime("%a %H:%M"), s.resource_id) for s in slots] == [("Thu 10:00", "june"), ("Thu 10:15", "june")]
    anyone = await domain.bookings.open_slots(salon, item="womens cut", date_from=date(2026, 10, 15), limit=1, resource_id="any")
    assert anyone[0].resource_id == "wren" and anyone[0].remaining == 2
