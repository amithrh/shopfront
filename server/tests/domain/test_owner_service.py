"""OwnerService: ask-the-owner questions, decisions driving real MCP tasks, the inbox, and expiry.

Uses a real `TaskManager(InMemoryTaskStore)` and the conftest's `FakeRenderer`, so each decision's
effect on the task (completed result, input_required request) is checked end to end.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from shopfront_server.domain.models import Commitment
from shopfront_server.domain.planning import CommitPlan
from shopfront_server.domain.services.owner import OwnerDecisionInput
from shopfront_server.errors import InvalidDecision, InvalidState, NotFound
from shopfront_server.profile.model import Profile

pytestmark = pytest.mark.anyio

Domain = Any  # the `domain` fixture (tests/domain/conftest.py)

BAKERY = "thimbleberry-bakehouse"
BIG_CAKE = {"size": "ten inch round", "flavor": "chocolate", "filling": "jam", "frosting": "vanilla frosting"}
CUSTOMER = {"customer_name": "Sam Lee", "customer_phone": "+12175550100"}
DAY_KEY = "CAP#thimbleberry-bakehouse#custom-cakes#DAY#2026-10-18"


async def submit(domain: Domain, profile: Profile, plan: CommitPlan, tool: str) -> Commitment:
    """What the tools layer does: ensure the task (id derived from the key), then commit."""
    assert plan.ok, (plan.problems, plan.missing)
    record = await domain.tasks.create(
        principal=plan.principal,
        tool_name=tool,
        arguments={},
        status_message="Waiting for the owner",
        task_id=domain.ctx.ids.task_id(plan.idempotency_key),
    )
    outcome = await domain.orders.commit(profile, plan, task_id=record.task_id)
    assert outcome.commitment is not None
    return outcome.commitment


async def big_cake(domain: Domain, bakery: Profile, principal: str = "anonymous") -> Commitment:
    plan = await domain.orders.plan(
        bakery,
        item="custom cake",
        choices=BIG_CAKE,
        day="2026-10-18",
        intake={"servings": 30, **CUSTOMER},
        principal=principal,
    )
    return await submit(domain, bakery, plan, "place_custom_order")


async def decide(domain: Domain, commitment: Commitment, decision: str, **kwargs: Any) -> Commitment:
    return await domain.owner.decide(
        commitment.business_id, commitment.commitment_id, OwnerDecisionInput(decision=decision, **kwargs)
    )


# ---------------------------------------------------------------------------- ask the owner


async def test_plan_question(domain: Domain, bakery: Profile) -> None:
    plan = await domain.owner.plan_question(
        bakery, question="  Do you make   nut-free cakes? ", principal="anonymous", fact_ref="menu/custom-cake#allergens"
    )
    same = await domain.owner.plan_question(
        bakery, question="Do you make nut-free cakes?", principal="anonymous", fact_ref="menu/custom-cake#allergens"
    )
    assert plan == same and plan.ok
    assert (plan.kind, plan.operation, plan.module_id) == ("owner_question", "ask_owner", "rosa")
    assert plan.question == "Do you make nut-free cakes?" and plan.fact_ref == "menu/custom-cake#allergens"
    assert plan.approval.required and plan.approval.owner_module_id == "rosa"
    sloppy = await domain.owner.plan_question(bakery, question="Gift cards?", principal="anonymous", fact_ref="NOT A REF")
    assert sloppy.ok and sloppy.fact_ref is None
    empty = await domain.owner.plan_question(bakery, question="   ", principal="anonymous")
    too_long = await domain.owner.plan_question(bakery, question="why " * 100, principal="anonymous")
    assert [p.code for p in empty.problems] == [p.code for p in too_long.problems] == ["invalid_intake"]


async def test_owner_who_takes_no_questions(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    doc = raw(BAKERY)
    doc["modules"]["rosa"]["answers_unknown_questions"] = False
    plan = await domain.owner.plan_question(build_profile(doc), question="Gift cards?", principal="anonymous")
    assert [(p.code, p.fact_ref) for p in plan.problems] == [("not_offered", "rosa#answers_unknown_questions")]


async def test_question_is_answered_through_its_task(domain: Domain, bakery: Profile) -> None:
    plan = await domain.owner.plan_question(bakery, question="Do you sell gift cards?", principal="anonymous")
    question = await submit(domain, bakery, plan, "ask_owner")
    assert (question.status, question.kind, question.module_id) == ("pending_owner", "owner_question", "rosa")
    assert question.expires_at == domain.clock.now() + timedelta(minutes=2880)
    items, _ = await domain.owner.inbox(BAKERY)
    assert items[0].allowed_decisions == ["answer"] and items[0].response_fields[0]["key"] == "answer"
    with pytest.raises(InvalidDecision):
        await decide(domain, question, "approve")
    with pytest.raises(InvalidDecision, match="required"):
        await decide(domain, question, "answer")
    answered = await decide(domain, question, "answer", note="Yes, in any amount from $25.")
    assert answered.status == "answered" and answered.decision is not None
    assert answered.decision.values == {"answer": "Yes, in any amount from $25."}
    task = await domain.tasks.get(question.task_id)
    assert task.status == "completed" and task.result["structuredContent"]["status"] == "answered"


# ---------------------------------------------------------------------------- decisions


async def test_approve_completes_the_task_and_keeps_the_slot(domain: Domain, bakery: Profile) -> None:
    cake = await big_cake(domain, bakery)
    approved = await decide(domain, cake, "approve", note="  See you   Sunday ")
    assert approved.status == "approved" and approved.version == 1 and not approved.holds_released
    assert approved.decision is not None and approved.decision.note == "See you Sunday"
    assert approved.expires_at is None
    task = await domain.tasks.get(cake.task_id)
    assert task.status == "completed" and task.result["structuredContent"] == {"status": "approved", "reference": cake.commitment_id}
    assert domain.renderer.rendered == [(cake.commitment_id, "approved")]
    assert domain.state.capacity[DAY_KEY] == 1
    with pytest.raises(InvalidState):
        await decide(domain, approved, "decline")


async def test_decline_frees_the_day(domain: Domain, bakery: Profile) -> None:
    cake = await big_cake(domain, bakery)
    declined = await decide(domain, cake, "decline")
    assert declined.status == "declined" and declined.holds_released
    assert domain.state.capacity[DAY_KEY] == 0
    assert (await domain.tasks.get(cake.task_id)).status == "completed"


async def test_request_info_round_trip(domain: Domain, bakery: Profile) -> None:
    cake = await big_cake(domain, bakery)
    with pytest.raises(InvalidDecision, match="question"):
        await decide(domain, cake, "request_info")
    asked = await decide(domain, cake, "request_info", question="Which colour should the piping be?")
    assert asked.status == "info_requested" and asked.expires_at == cake.expires_at
    task = await domain.tasks.get(cake.task_id)
    assert task.status == "input_required" and list(task.input_requests) == ["owner_question_1"]
    request = task.input_requests["owner_question_1"]
    assert request["method"] == "elicitation/create"
    assert request["params"]["message"] == "Rosa asks: Which colour should the piping be?"
    assert request["params"]["requestedSchema"]["required"] == ["answer"]

    # The customer answers through tasks/update; the inbox folds the answer back in.
    await domain.tasks.submit_input(cake.task_id, "anonymous", {"owner_question_1": {"action": "accept", "content": {"answer": "Pink"}}})
    items, _ = await domain.owner.inbox(BAKERY)
    folded = next(item.commitment for item in items if item.commitment.commitment_id == cake.commitment_id)
    assert folded.status == "pending_owner" and folded.customer_input == {"owner_question_1": {"answer": "Pink"}}

    again = await decide(domain, folded, "request_info", question="And the lettering?")
    task = await domain.tasks.get(cake.task_id)
    assert again.status == "info_requested" and list(task.input_requests) == ["owner_question_2"]
    await domain.tasks.submit_input(cake.task_id, "anonymous", {"owner_question_2": {"action": "decline"}})
    approved = await decide(domain, again, "approve")  # folds the decline, then approves
    assert approved.status == "approved"
    assert approved.customer_input == {"owner_question_1": {"answer": "Pink"}, "owner_question_2": {"action": "decline"}}
    assert (await domain.tasks.get(cake.task_id)).status == "completed"


async def test_response_fields_are_validated_and_money_becomes_cents(domain: Domain, home: Profile) -> None:
    plan = await domain.quotes.plan(
        home,
        item="water heater",
        intake={
            "description": "No hot water",
            "zip": "80210",
            "time_window": "first available",
            "name": "Sam",
            "contact_by": "email",
            "email": "sam@example.com",
        },
        principal="anonymous",
    )
    job = await submit(domain, home, plan, "request_quote")
    items, _ = await domain.owner.inbox("kettlewick-home")
    assert items[0].allowed_decisions == ["quote", "request_info", "decline"]
    assert [f["key"] for f in items[0].response_fields] == ["reply_type", "quoted_price", "note"]
    with pytest.raises(InvalidDecision, match="required"):
        await decide(domain, job, "quote", values={"quoted_price": 240})
    with pytest.raises(InvalidDecision, match="unknown fields"):
        await decide(domain, job, "quote", values={"reply_type": "fixed_price", "discount": 5})
    with pytest.raises(InvalidDecision, match="Quoted price"):
        await decide(domain, job, "quote", values={"reply_type": "fixed_price", "quoted_price": "lots"})
    with pytest.raises(InvalidDecision):
        await decide(domain, job, "approve")  # not in this module's decisions
    quoted = await decide(domain, job, "quote", values={"reply_type": "Fixed price for the job as described", "quoted_price": "1,240.50"})
    assert quoted.status == "quoted"
    assert quoted.decision is not None and quoted.decision.values == {"reply_type": "fixed_price", "quoted_price": 124050}


async def test_suggest_time_needs_content_and_frees_the_slot(domain: Domain, salon: Profile) -> None:
    plan = await domain.bookings.plan(
        salon,
        item="all over color",
        start="2026-10-16T10:00",
        party_size=None,
        intake={"stylist": "ines", "customer_name": "Sam", "customer_phone": "2175550100", "customer_email": "s@example.com"},
        principal="anonymous",
    )
    record = await domain.tasks.create(
        principal="anonymous", tool_name="book", arguments={}, status_message="Waiting", task_id=domain.ctx.ids.task_id(plan.idempotency_key)
    )
    booking = (await domain.bookings.commit(salon, plan, task_id=record.task_id)).commitment
    assert booking is not None and booking.status == "pending_owner"
    with pytest.raises(InvalidDecision, match="details or a note"):
        await decide(domain, booking, "suggest_time")
    suggested = await decide(domain, booking, "suggest_time", values={"suggested_date": "2026-10-17", "suggested_time": "9:30"})
    assert suggested.status == "suggested_time" and suggested.holds_released
    assert suggested.decision is not None and suggested.decision.values == {"suggested_date": "2026-10-17", "suggested_time": "09:30"}
    assert domain.capacity_used("CAP#quillfern-salon") == {}


async def test_customer_cancelled_task_rejects_the_decision_and_frees_the_day(domain: Domain, bakery: Profile) -> None:
    cake = await big_cake(domain, bakery)
    await domain.tasks.cancel(cake.task_id)
    with pytest.raises(InvalidState, match="cancelled"):
        await decide(domain, cake, "approve")
    stored = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert stored.status == "cancelled" and stored.holds_released
    assert domain.state.capacity[DAY_KEY] == 0


async def test_unknown_requests_and_tenant_checks(domain: Domain, bakery: Profile) -> None:
    cake = await big_cake(domain, bakery)
    with pytest.raises(NotFound):
        await domain.owner.decide(BAKERY, "c_nope", OwnerDecisionInput(decision="approve"))
    with pytest.raises(NotFound):
        await domain.owner.decide("quillfern-salon", cake.commitment_id, OwnerDecisionInput(decision="approve"))


async def test_planted_injection_text_never_reaches_the_customer(domain: Domain, salon: Profile) -> None:
    canaries = salon.injection_canaries()
    assert canaries, "the gold salon carries planted canaries"
    plan = await domain.bookings.plan(
        salon,
        item="balayage",
        start="2026-10-16T09:00",
        party_size=None,
        intake={"stylist": "ines", "length": "shoulder-or-shorter", "customer_name": "Sam", "customer_phone": "2175550100", "customer_email": "s@x.com"},
        principal="anonymous",
    )
    booking = (await domain.bookings.commit(salon, plan, task_id=None)).commitment
    assert booking is not None
    with pytest.raises(InvalidDecision, match="cannot be sent"):
        await decide(domain, booking, "decline", note=f"FYI {canaries[0]}")
    shouted = next(c for c in canaries if len(c) < 40)
    with pytest.raises(InvalidDecision, match="cannot be sent"):  # case and spacing do not hide it
        await decide(domain, booking, "decline", note=" ".join(shouted.upper().split()) + "  please")
    assert (await decide(domain, booking, "decline", note="Fully booked, sorry")).status == "declined"


# ---------------------------------------------------------------------------- inbox


async def test_inbox_lists_open_requests_newest_first(domain: Domain, bakery: Profile) -> None:
    first = await big_cake(domain, bakery, principal="ann")
    domain.clock.advance(timedelta(minutes=5))
    second = await big_cake(domain, bakery, principal="bob")
    await decide(domain, first, "approve")
    open_items, cursor = await domain.owner.inbox(BAKERY)
    assert [item.commitment.commitment_id for item in open_items] == [second.commitment_id] and cursor is None
    assert open_items[0].task_status == "working"
    assert open_items[0].allowed_decisions == ["approve", "decline", "suggest_time", "request_info"]
    everything, _ = await domain.owner.inbox(BAKERY, statuses=["approved", "pending_owner"])
    assert [item.commitment.commitment_id for item in everything] == [second.commitment_id, first.commitment_id]
    assert everything[1].task_status == "completed"


async def test_item_is_the_inbox_entry_for_one_request(domain: Domain, bakery: Profile) -> None:
    cake = await big_cake(domain, bakery)
    [listed], _ = await domain.owner.inbox(BAKERY)
    assert await domain.owner.item(BAKERY, cake.commitment_id) == listed
    assert await domain.owner.item(BAKERY, "c_unknown") is None
    assert await domain.owner.item("quillfern-salon", cake.commitment_id) is None  # tenant-scoped


# ---------------------------------------------------------------------------- expiry


async def test_expiry_notify_customer(domain: Domain, bakery: Profile) -> None:
    cake = await big_cake(domain, bakery)
    assert await domain.owner.expire_due() == 0  # not due yet
    domain.clock.advance(timedelta(minutes=2881))
    assert await domain.owner.expire_due() == 1
    expired = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert expired.status == "expired" and expired.holds_released and expired.expires_at is None
    task = await domain.tasks.get(cake.task_id)
    assert task.status == "completed" and task.result["structuredContent"]["status"] == "expired"
    assert await domain.owner.expire_due() == 0  # idempotent


async def with_policy(domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile], policy: str) -> Profile:
    doc = raw(BAKERY)
    doc["modules"]["rosa"]["on_expiry"] = policy
    profile = build_profile(doc)
    domain.profiles.replace(profile)
    return profile


async def test_expiry_auto_decline(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    profile = await with_policy(domain, raw, build_profile, "auto_decline")
    cake = await big_cake(domain, profile)
    domain.clock.advance(timedelta(days=3))
    assert await domain.owner.expire_due() == 1
    declined = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert declined.status == "declined" and declined.decision is not None
    assert declined.decision.decision == "decline" and declined.decision.values == {"automatic": True}
    assert (await domain.tasks.get(cake.task_id)).result["structuredContent"]["status"] == "declined"
    assert domain.state.capacity[DAY_KEY] == 0


async def test_expiry_keep_waiting(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    profile = await with_policy(domain, raw, build_profile, "keep_waiting")
    cake = await big_cake(domain, profile)
    domain.clock.advance(timedelta(days=3))
    assert await domain.owner.expire_due() == 0
    waiting = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    # It keeps waiting until its date is over (end of 18 Oct in Los Angeles), and is re-scanned then.
    assert waiting.status == "pending_owner" and waiting.expires_at == END_OF_CAKE_DAY
    assert await domain.ctx.commitments.list_expiring(before=domain.clock.now()) == []
    assert (await domain.tasks.get(cake.task_id)).status == "working"
    assert (await decide(domain, waiting, "approve")).status == "approved"


END_OF_CAKE_DAY = datetime(2026, 10, 19, 7, 0, tzinfo=UTC)
"""Midnight after 18 Oct 2026 in America/Los_Angeles (PDT): the keep_waiting horizon of the big cake."""


async def test_round2_expiry_keep_waiting_ends_once_the_date_has_passed(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    """keep_waiting used to have no upper bound: the cake for 18 Oct waited (and held its day) forever,
    and the owner could still approve it on the 26th."""
    profile = await with_policy(domain, raw, build_profile, "keep_waiting")
    cake = await big_cake(domain, profile)
    domain.clock.advance(timedelta(days=3))
    assert await domain.owner.expire_due() == 0
    domain.clock.set(END_OF_CAKE_DAY + timedelta(minutes=1))  # the next sweep after midnight
    assert await domain.owner.expire_due() == 1
    expired = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert expired.status == "expired" and expired.holds_released and expired.expires_at is None
    assert domain.state.capacity[DAY_KEY] == 0
    task = await domain.tasks.get(cake.task_id)
    assert task.status == "completed" and task.result["structuredContent"]["status"] == "expired"


async def test_round2_expiry_keep_waiting_undated_requests_wait_at_most_keep_waiting_max(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    """An owner question has no service date: it waits `keep_waiting_max` (30 days by default)."""
    profile = await with_policy(domain, raw, build_profile, "keep_waiting")
    plan = await domain.owner.plan_question(profile, question="Do you make gluten-free cupcakes?", principal="anonymous")
    question = await submit(domain, profile, plan, "ask_owner")
    domain.clock.advance(timedelta(days=3))
    assert await domain.owner.expire_due() == 0
    waiting = await domain.ctx.commitments.get(BAKERY, question.commitment_id)
    assert waiting.status == "pending_owner"
    assert waiting.expires_at == question.created_at + domain.ctx.keep_waiting_max == question.created_at + timedelta(days=30)
    domain.clock.set(waiting.expires_at + timedelta(minutes=1))
    assert await domain.owner.expire_due() == 1
    assert (await domain.ctx.commitments.get(BAKERY, question.commitment_id)).status == "expired"


async def test_round2_expiry_owner_cannot_approve_a_dated_request_after_its_date(
    domain: Domain, bakery: Profile
) -> None:
    """Whatever `on_expiry` says, a cake for a day that is over cannot be approved (nor counter-offered,
    nor asked about); it can still be declined or given another date, and the inbox says so."""
    cake = await big_cake(domain, bakery)
    domain.clock.set(END_OF_CAKE_DAY - timedelta(seconds=1))
    [item] = (await domain.owner.inbox(BAKERY))[0]
    assert "approve" in item.allowed_decisions
    domain.clock.set(END_OF_CAKE_DAY)
    [item] = (await domain.owner.inbox(BAKERY))[0]
    assert not {"approve", "counter_offer", "request_info"} & set(item.allowed_decisions)
    assert "decline" in item.allowed_decisions
    with pytest.raises(InvalidState, match="date has passed"):
        await decide(domain, cake, "approve")
    with pytest.raises(InvalidState, match="date has passed"):
        await decide(domain, cake, "request_info", question="Which colour?")
    assert (await domain.ctx.commitments.get(BAKERY, cake.commitment_id)).status == "pending_owner"
    declined = await decide(domain, cake, "decline")
    assert declined.status == "declined" and declined.holds_released


async def test_round2_expiry_keep_waiting_withdraws_a_request_whose_task_was_cancelled(
    domain: Domain, raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]
) -> None:
    """A cancel that bypassed the hook (another replica died in between) must not leave the day held
    until the horizon: the keep_waiting sweep withdraws the request."""
    profile = await with_policy(domain, raw, build_profile, "keep_waiting")
    cake = await big_cake(domain, profile)
    await domain.tasks.cancel(cake.task_id)  # the manager directly: no cancel hook
    domain.clock.advance(timedelta(hours=49))
    assert await domain.owner.expire_due() == 0
    withdrawn = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert withdrawn.status == "cancelled" and withdrawn.holds_released
    assert domain.state.capacity[DAY_KEY] == 0


async def test_expiry_survives_a_cancelled_task(domain: Domain, bakery: Profile) -> None:
    cake = await big_cake(domain, bakery)
    await domain.tasks.cancel(cake.task_id)
    domain.clock.advance(timedelta(days=3))
    assert await domain.owner.expire_due() == 1
    assert (await domain.ctx.commitments.get(BAKERY, cake.commitment_id)).status == "expired"


async def test_a_rendering_failure_fails_the_task_instead_of_leaving_it_waiting(
    domain: Domain, bakery: Profile, caplog: pytest.LogCaptureFixture
) -> None:
    first = await big_cake(domain, bakery, principal="ann")
    second = await big_cake(domain, bakery, principal="bob")
    domain.renderer.fail = True
    approved = await decide(domain, first, "approve")
    assert approved.status == "approved"
    task = await domain.tasks.get(first.task_id)
    assert task.status == "failed" and "rendering the decision failed" in caplog.text
    domain.clock.advance(timedelta(days=3))
    assert await domain.owner.expire_due() == 1  # the sweep goes on
    assert (await domain.tasks.get(second.task_id)).status == "failed"
    assert (await domain.ctx.commitments.get(BAKERY, second.commitment_id)).status == "expired"


# ---------------------------------------------------------------------------- round 2: idempotency replay


async def test_round2_idempotency_replay_an_answered_question_can_be_asked_again(domain: Domain, bakery: Profile) -> None:
    """F1 residue: `answered` has no outgoing transition but was not treated as settled, so an
    authenticated customer (no per-call nonce) asking the same question again got the old answer
    replayed forever, and the owner never saw the new question."""

    async def ask() -> CommitPlan:
        return await domain.owner.plan_question(bakery, question="Can you do a nut-free cake for my son?", principal="user-1")

    first = await submit(domain, bakery, await ask(), "ask_owner")
    live = await domain.owner.replay(await ask())
    assert live is not None and live.commitment is not None and live.commitment.commitment_id == first.commitment_id
    assert (await decide(domain, first, "answer", note="Yes, for Saturday only.")).status == "answered"

    again = await ask()
    assert await domain.owner.replay(again) is None
    fresh = await domain.owner.with_generation(again)
    assert fresh.ok and fresh.idempotency_key == again.idempotency_key + "~1"
    second = await submit(domain, bakery, fresh, "ask_owner")
    assert second.status == "pending_owner" and second.commitment_id != first.commitment_id
    retry = await domain.owner.replay(await ask())  # the new live attempt still dedupes
    assert retry is not None and retry.commitment is not None and retry.commitment.commitment_id == second.commitment_id


async def test_round2_idempotency_replay_used_up_attempts_are_refused_not_replayed(
    domain: Domain, bakery: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once every attempt at one request is settled, the next identical request used to replay the last
    settled (declined) commitment. It is now refused with `too_many_attempts` and nothing is written."""
    from shopfront_server.domain.services import base

    monkeypatch.setattr(base, "MAX_GENERATIONS", 2)

    async def plan() -> CommitPlan:
        return await domain.orders.plan(
            bakery, item="custom cake", choices=BIG_CAKE, day="2026-10-18", intake={"servings": 30, **CUSTOMER}, principal="anonymous"
        )

    request_key = (await plan()).idempotency_key
    for expected in (request_key, request_key + "~1"):
        attempt = await domain.orders.with_generation(await plan())
        assert attempt.ok and attempt.idempotency_key == expected
        await decide(domain, await submit(domain, bakery, attempt, "place_custom_order"), "decline")

    last = await plan()
    assert last.ok and await domain.orders.replay(last) is None
    refused = await domain.orders.with_generation(last)
    assert not refused.ok and [p.code for p in refused.problems] == ["too_many_attempts"]
    with pytest.raises(ValueError):
        await domain.orders.commit(bakery, refused, task_id=None)
    statuses = sorted(c.status for c in (await domain.ctx.commitments.list_for_business(BAKERY, statuses=["declined", "pending_owner"]))[0])
    assert statuses == ["declined", "declined"]


# ---------------------------------------------------------------------------- round 2: cancel releases


def cancel_lands_before_delivery(domain: Domain, monkeypatch: pytest.MonkeyPatch, *, hook: bool) -> None:
    """Make the customer's tasks/cancel land inside `decide`, after it stored the decision and before it
    drives the task (in production: between two DynamoDB round trips). `hook=False` models a cancel
    whose hook never ran (the replica died between `TaskManager.cancel` and the hook)."""
    owner = domain.owner
    real_drive = owner._drive_task

    async def drive(profile: Profile, updated: Commitment, task: Any, question: str | None) -> None:
        record = await domain.tasks.cancel(updated.task_id, "Cancelled by the client")
        if hook:
            await owner.on_task_cancelled(record)
        await real_drive(profile, updated, task, question)

    monkeypatch.setattr(owner, "_drive_task", drive)


@pytest.mark.parametrize("hook", [True, False], ids=["with-hook", "hook-never-ran"])
async def test_round2_cancel_releases_a_cancel_between_decision_and_delivery_withdraws_the_approval(
    domain: Domain, bakery: Profile, monkeypatch: pytest.MonkeyPatch, hook: bool
) -> None:
    """S4: `decide` stored `approved`, the customer's cancel landed, and completing the task failed. The
    approval used to stay stored (and keep the day) although the customer never received it."""
    cake = await big_cake(domain, bakery)
    cancel_lands_before_delivery(domain, monkeypatch, hook=hook)
    with pytest.raises(InvalidState, match="already cancelled"):
        await decide(domain, cake, "approve")
    monkeypatch.undo()
    stored = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert stored.status == "cancelled" and stored.holds_released and stored.expires_at is None
    assert domain.state.capacity[DAY_KEY] == 0
    assert (await domain.tasks.get(cake.task_id)).status == "cancelled"
    assert (await domain.owner.inbox(BAKERY))[0] == []
    with pytest.raises(InvalidState):  # a later approval cannot revive it
        await decide(domain, stored, "approve")
    assert (await domain.ctx.commitments.get(BAKERY, cake.commitment_id)).status == "cancelled"


@pytest.mark.parametrize(
    ("decision", "kwargs", "final"),
    [("request_info", {"question": "Any nut allergies?"}, "cancelled"), ("decline", {}, "declined")],
)
async def test_round2_cancel_releases_other_decisions_racing_a_cancel_still_free_the_day(
    domain: Domain, bakery: Profile, monkeypatch: pytest.MonkeyPatch, decision: str, kwargs: dict[str, Any], final: str
) -> None:
    """A question (info_requested) is withdrawn like an approval; a decline already released the holds."""
    cake = await big_cake(domain, bakery)
    cancel_lands_before_delivery(domain, monkeypatch, hook=False)
    with pytest.raises(InvalidState, match="already cancelled"):
        await decide(domain, cake, decision, **kwargs)
    stored = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert stored.status == final and stored.holds_released
    assert domain.state.capacity[DAY_KEY] == 0


async def test_round2_cancel_releases_the_hook_rereads_a_stale_index_lookup(
    domain: Domain, bakery: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S5: the task index (a DynamoDB GSI) can lag. The hook used to CAS against the stale copy it got
    from it, lose, and give up, leaving the holds in place until the inbox or expiry cleaned up."""
    cake = await big_cake(domain, bakery)  # tasks created without labels: the hook falls back to the index
    await decide(domain, cake, "request_info", question="Any nut allergies?")

    async def lagging_index(task_id: str) -> Commitment | None:
        return cake  # the pre-question copy (pending_owner, version 0)

    monkeypatch.setattr(domain.ctx.commitments, "get_by_task", lagging_index)
    await domain.owner.on_task_cancelled(await domain.tasks.cancel(cake.task_id, "Cancelled by the client"))
    stored = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert stored.status == "cancelled" and stored.holds_released
    assert domain.state.capacity[DAY_KEY] == 0


async def test_round2_cancel_releases_the_hook_retries_after_a_concurrent_change(
    domain: Domain, bakery: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another writer (here: folding the customer's answer) changes the commitment between the hook's
    read and its write; the hook re-reads and withdraws the new version instead of giving up."""
    cake = await big_cake(domain, bakery)
    await decide(domain, cake, "request_info", question="Any nut allergies?")
    store = domain.ctx.commitments
    real_update = store.update
    writes: list[str] = []

    async def racing_update(commitment: Commitment, *, expected_version: int) -> Commitment:
        if not writes:
            current = await store.get(BAKERY, cake.commitment_id)
            folded = current.model_copy(update={"status": "pending_owner", "customer_input": {"owner_question_1": "No nuts"}})
            await real_update(folded, expected_version=current.version)
        writes.append(commitment.status)
        return await real_update(commitment, expected_version=expected_version)

    monkeypatch.setattr(store, "update", racing_update)
    await domain.owner.on_task_cancelled(await domain.tasks.cancel(cake.task_id, "Cancelled by the client"))
    monkeypatch.undo()
    stored = await store.get(BAKERY, cake.commitment_id)
    assert writes == ["cancelled", "cancelled"]  # the first CAS lost, the retry won
    assert stored.status == "cancelled" and stored.holds_released and stored.customer_input == {"owner_question_1": "No nuts"}
    assert domain.state.capacity[DAY_KEY] == 0


async def test_round2_cancel_releases_a_hook_that_keeps_losing_logs_and_the_inbox_finishes_the_job(
    domain: Domain, bakery: Profile, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from shopfront_server.errors import ConcurrencyConflict

    cake = await big_cake(domain, bakery)
    store = domain.ctx.commitments

    async def always_conflicts(commitment: Commitment, *, expected_version: int) -> Commitment:
        raise ConcurrencyConflict("the commitment changed; reload and retry")

    monkeypatch.setattr(store, "update", always_conflicts)
    await domain.owner.on_task_cancelled(await domain.tasks.cancel(cake.task_id, "Cancelled by the client"))
    monkeypatch.undo()
    assert "could not withdraw the commitment of a cancelled task" in caplog.text
    assert cake.commitment_id not in caplog.text  # ids are logged as digests only
    assert (await store.get(BAKERY, cake.commitment_id)).status == "pending_owner"
    [item] = (await domain.owner.inbox(BAKERY))[0]  # the next inbox view closes it, offering no decision
    assert item.commitment.status == "cancelled" and item.allowed_decisions == []
    assert (await domain.owner.inbox(BAKERY))[0] == []
    assert (await store.get(BAKERY, cake.commitment_id)).status == "cancelled"
    assert domain.state.capacity[DAY_KEY] == 0


async def test_round2_cancel_releases_the_hook_uses_task_labels_when_the_index_misses(
    domain: Domain, bakery: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tools layer labels each owner task with its commitment id, so the hook needs no index."""
    plan = await domain.orders.plan(
        bakery, item="custom cake", choices=BIG_CAKE, day="2026-10-18", intake={"servings": 30, **CUSTOMER}, principal="anonymous"
    )
    record = await domain.tasks.create(
        principal="anonymous",
        tool_name="place_custom_order",
        arguments={},
        status_message="Waiting for the owner",
        task_id=domain.ctx.ids.task_id(plan.idempotency_key),
        labels={"business_id": BAKERY, "commitment_id": domain.ctx.ids.commitment_id(plan.idempotency_key)},
    )
    outcome = await domain.orders.commit(bakery, plan, task_id=record.task_id)
    assert outcome.commitment is not None and outcome.commitment.commitment_id == record.labels["commitment_id"]

    async def not_yet_indexed(task_id: str) -> Commitment | None:
        return None

    monkeypatch.setattr(domain.ctx.commitments, "get_by_task", not_yet_indexed)
    await domain.owner.on_task_cancelled(await domain.tasks.cancel(record.task_id, "Cancelled by the client"))
    stored = await domain.ctx.commitments.get(BAKERY, outcome.commitment.commitment_id)
    assert stored.status == "cancelled" and stored.holds_released
    assert domain.state.capacity[DAY_KEY] == 0


async def test_round2_cancel_releases_labels_never_reach_a_commitment_of_another_task(domain: Domain, bakery: Profile) -> None:
    mine = await big_cake(domain, bakery, principal="ann")
    other = await big_cake(domain, bakery, principal="bob")
    record = await domain.tasks.create(
        principal="anonymous",
        tool_name="place_custom_order",
        arguments={},
        status_message="Waiting for the owner",
        labels={"business_id": BAKERY, "commitment_id": other.commitment_id},
    )
    await domain.owner.on_task_cancelled(await domain.tasks.cancel(record.task_id, "Cancelled by the client"))
    assert (await domain.ctx.commitments.get(BAKERY, other.commitment_id)).status == "pending_owner"
    assert (await domain.ctx.commitments.get(BAKERY, mine.commitment_id)).status == "pending_owner"
    assert domain.state.capacity[DAY_KEY] == 2


async def test_round2_cancel_releases_a_delivered_decision_is_never_withdrawn(
    domain: Domain, bakery: Profile, home: Profile, salon: Profile
) -> None:
    """Once the decision reached the customer (task completed) tasks/cancel is refused, so the hook never
    runs; and the hook leaves settled commitments (quoted, declined) and task-less ones alone."""
    from mcp_tasks import InvalidTransition

    cake = await big_cake(domain, bakery)
    approved = await decide(domain, cake, "approve")
    with pytest.raises(InvalidTransition):
        await domain.tasks.cancel(cake.task_id, "Cancelled by the client")
    completed = await domain.tasks.get(cake.task_id)
    await domain.owner.on_task_cancelled(completed)  # not a cancelled task: ignored
    assert (await domain.ctx.commitments.get(BAKERY, cake.commitment_id)) == approved
    assert domain.state.capacity[DAY_KEY] == 1

    plan = await domain.quotes.plan(
        home,
        item="water heater",
        intake={"description": "No hot water", "zip": "80210", "time_window": "first available", "name": "Sam", "contact_by": "email", "email": "sam@example.com"},
        principal="anonymous",
    )
    job = await submit(domain, home, plan, "request_quote")
    quoted = await decide(domain, job, "quote", values={"reply_type": "fixed_price", "quoted_price": "240"})
    declined = await decide(domain, await big_cake(domain, bakery, principal="bob"), "decline")
    for settled in (quoted, declined):
        # As if the task had been cancelled: the commitment is settled already, nothing changes.
        fake = dataclasses.replace(await domain.tasks.get(settled.task_id), status="cancelled")
        await domain.owner.on_task_cancelled(fake)
        assert (await domain.ctx.commitments.get(settled.business_id, settled.commitment_id)) == settled

    cut = await domain.bookings.plan(
        salon,
        item="women's cut",
        start="2026-10-15T10:00",
        party_size=None,
        intake={"stylist": "any", **CUSTOMER, "customer_email": "sam@example.com"},
        principal="anonymous",
    )
    booked = (await domain.bookings.commit(salon, cut, task_id=None)).commitment
    assert booked is not None and booked.status == "confirmed" and booked.task_id is None
    stray = await domain.tasks.create(
        principal="anonymous",
        tool_name="book_appointment",
        arguments={},
        status_message="Waiting",
        labels={"business_id": booked.business_id, "commitment_id": booked.commitment_id},
    )
    await domain.owner.on_task_cancelled(await domain.tasks.cancel(stray.task_id, "Cancelled by the client"))
    assert (await domain.ctx.commitments.get(booked.business_id, booked.commitment_id)) == booked


async def test_round2_cancel_releases_the_inbox_closes_an_undelivered_approval_behind_a_cancelled_task(
    domain: Domain, bakery: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces: if neither the hook nor `decide` could withdraw it (the replica died after storing
    the approval), the owner page closes it the next time it shows the request."""
    cake = await big_cake(domain, bakery)

    async def crashed(*args: Any) -> None:  # the decision was stored, the task never driven
        return None

    monkeypatch.setattr(domain.owner, "_drive_task", crashed)
    assert (await decide(domain, cake, "approve")).status == "approved"
    monkeypatch.undo()
    await domain.tasks.cancel(cake.task_id, "Cancelled by the client")  # the hook did not run
    item = await domain.owner.item(BAKERY, cake.commitment_id)
    assert item is not None and item.commitment.status == "cancelled" and item.allowed_decisions == []
    assert item.commitment.holds_released and domain.state.capacity[DAY_KEY] == 0


# ---------------------------------------------------------------------------- round 2: request_info


async def test_round2_request_info_absorbing_a_reply_stores_it_at_once(domain: Domain, bakery: Profile) -> None:
    """R1: a reply delivered on the task is stored on the request right away (no inbox visit needed)."""
    cake = await big_cake(domain, bakery)
    await decide(domain, cake, "request_info", question="Any nut allergies?")
    task = await domain.tasks.submit_input(
        cake.task_id, "anonymous", {"owner_question_1": {"action": "accept", "content": {"answer": "None"}}}
    )
    absorbed = await domain.owner.absorb_customer_answers(task)
    assert absorbed is not None and absorbed.status == "pending_owner"
    assert absorbed.customer_input == {"owner_question_1": {"answer": "None"}}
    assert absorbed.decision is not None and absorbed.decision.decision == "request_info"
    assert absorbed.decision.answered_at == domain.clock.now()
    assert await domain.owner.absorb_customer_answers(task) == absorbed  # idempotent: nothing new to fold
    stored = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert stored == absorbed


async def test_round2_request_info_expiry_keeps_a_reply_nobody_folded(domain: Domain, bakery: Profile) -> None:
    """R2: the customer answered (here straight on the task, so nothing folded it) and Rosa never opened
    the inbox. Expiry used to throw the reply away; it now folds it first, then expires the request."""
    cake = await big_cake(domain, bakery)
    await decide(domain, cake, "request_info", question="Any nut allergies?")
    await domain.tasks.submit_input(
        cake.task_id, "anonymous", {"owner_question_1": {"action": "accept", "content": {"answer": "None"}}}
    )
    domain.clock.advance(timedelta(days=3))
    assert await domain.owner.expire_due() == 1
    stored = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert stored.status == "expired" and stored.holds_released and stored.expires_at is None
    assert stored.customer_input == {"owner_question_1": {"answer": "None"}}
    assert stored.decision is not None and stored.decision.answered_at is not None
    assert domain.renderer.rendered[-1] == (cake.commitment_id, "expired")
    assert (await domain.tasks.get(cake.task_id)).status == "completed"


async def test_round2_request_info_an_unanswered_question_still_expires_unanswered(domain: Domain, bakery: Profile) -> None:
    cake = await big_cake(domain, bakery)
    await decide(domain, cake, "request_info", question="Any nut allergies?")
    domain.clock.advance(timedelta(days=3))
    assert await domain.owner.expire_due() == 1
    stored = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert stored.status == "expired" and stored.customer_input is None
    assert stored.decision is not None and stored.decision.answered_at is None


async def test_round2_request_info_an_earlier_reply_never_answers_the_next_question(
    domain: Domain, bakery: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rosa asks a second question. Between storing it and putting it on the task, the task is still
    `working` with the first reply on it; a fold landing in that window (the customer's get_task, the
    input hook, expiry) used to read the old reply as the answer to the new question, so the second
    question was skipped and its real answer later never reached Rosa."""
    cake = await big_cake(domain, bakery)
    await decide(domain, cake, "request_info", question="Any nut allergies?")
    await domain.tasks.submit_input(
        cake.task_id, "anonymous", {"owner_question_1": {"action": "accept", "content": {"answer": "None"}}}
    )
    real_require = domain.tasks.require_input

    async def fold_first(task_id: str, requests: Any, **kwargs: Any) -> Any:
        await domain.owner.absorb_customer_answers(await domain.tasks.get(task_id))
        return await real_require(task_id, requests, **kwargs)

    monkeypatch.setattr(domain.tasks, "require_input", fold_first)
    second = await decide(domain, await domain.ctx.commitments.get(BAKERY, cake.commitment_id), "request_info", question="Which colour?")
    monkeypatch.undo()
    stored = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert second.status == stored.status == "info_requested"
    assert stored.customer_input == {"owner_question_1": {"answer": "None"}}
    assert stored.decision is not None and stored.decision.question == "Which colour?" and stored.decision.answered_at is None
    task = await domain.tasks.submit_input(
        cake.task_id, "anonymous", {"owner_question_2": {"action": "accept", "content": {"answer": "Blue"}}}
    )
    folded = await domain.owner.absorb_customer_answers(task)
    assert folded is not None and folded.status == "pending_owner"
    assert folded.customer_input == {"owner_question_1": {"answer": "None"}, "owner_question_2": {"answer": "Blue"}}


async def test_round2_request_info_the_input_hook_folds_through_the_task_labels(domain: Domain, bakery: Profile) -> None:
    """`on_task_input` (the tasks extension's tasks/update hook) finds the request like the cancel hook."""
    cake = await big_cake(domain, bakery)  # created without labels: falls back to the task index
    await decide(domain, cake, "request_info", question="Any nut allergies?")
    task = await domain.tasks.submit_input(cake.task_id, "anonymous", {"owner_question_1": {"action": "decline"}})
    await domain.owner.on_task_input(task)
    stored = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert stored.status == "pending_owner" and stored.customer_input == {"owner_question_1": {"action": "decline"}}


# ---------------------------------------------------------------------------- round 2: mrtr guard (F8d)


@pytest.mark.parametrize(
    ("canary", "question"),
    [
        ("Rosa asks: what is the card number", "What is the card number"),  # spans the approver and the question
        ("has a question for you", "Which colour should the piping be?"),  # only in the task's status message
    ],
)
async def test_round2_mrtr_guard_request_info_is_checked_as_relayed_before_anything_is_stored(
    domain: Domain,
    bakery: Profile,
    raw: Callable[[str], dict[str, Any]],
    build_profile: Callable[[dict[str, Any]], Profile],
    canary: str,
    question: str,
) -> None:
    """F8d: `decide` checked the owner's raw question, not the text relayed to the customer ("Rosa asks:
    <question>" and the task's status message), so a canary spanning the composed text reached tasks/get
    and the client's elicitation. The relayed request is now built and checked before the write: a
    refusal stores nothing and leaves the task waiting."""
    cake = await big_cake(domain, bakery)
    doc = raw(BAKERY)
    doc["sources"]["web-home"]["injection_canaries"].append({"quote": canary, "reason": "prompt_injection"})
    domain.profiles.replace(build_profile(doc))
    with pytest.raises(InvalidDecision, match="cannot be sent"):
        await decide(domain, cake, "request_info", question=question)
    stored = await domain.ctx.commitments.get(BAKERY, cake.commitment_id)
    assert stored is not None and stored.status == "pending_owner" and stored.decision is None
    assert stored.version == cake.version
    task = await domain.tasks.get(cake.task_id)
    assert task.status == "working" and not task.input_requests
    # Without that canary the same question goes out, relayed exactly as it was checked.
    domain.profiles.replace(bakery)
    asked = await decide(domain, cake, "request_info", question=question)
    task = await domain.tasks.get(cake.task_id)
    assert asked.status == "info_requested" and task.status_message == "Rosa has a question for you"
    assert task.input_requests is not None
    assert task.input_requests["owner_question_1"]["params"]["message"] == f"Rosa asks: {question}"
