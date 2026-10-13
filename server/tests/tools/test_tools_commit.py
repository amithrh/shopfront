"""Transactional tools over MCP: MRTR confirmation, idempotency, owner tasks and their fallbacks."""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from support import ElicitationScript
from tools_support import BAKERY, BIG_CAKE, HOME, SALON, SMALL_CAKE, Stack, assert_grounded, structured

from shopfront_server.domain.planning import CommitOutcome, PlanProblem
from shopfront_server.domain.services.owner import OwnerDecisionInput
from shopfront_server.ids import spoken_reference

pytestmark = pytest.mark.anyio

MENS_CUT = {"business_id": SALON, "item": "men's cut", "start": "2026-10-16T10:00"}
FULL_SALON_DETAILS = {"stylist": "wren", "customer_name": "Sam", "customer_phone": "+15125550100", "customer_email": "s@example.com"}
QUOTE = {"business_id": HOME, "item": "leak repair", "details": {"description": "The kitchen sink pipe drips.", "zip": "80211"}}


async def commitments(stack: Stack, business_id: str) -> list[Any]:
    items, _cursor = await stack.deps.commitments.list_for_business(business_id)
    return items


def confirm_messages(script: ElicitationScript) -> list[str]:
    return [message for message, props in script.seen if props == ["confirm"]]


# ---------------------------------------------------------------------------- MRTR rounds


async def test_two_rounds_when_nothing_is_missing(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        data = structured(await client.call_tool("place_custom_order", SMALL_CAKE))
    assert data["status"] == "confirmed"
    assert stack.counter.calls == {"place_custom_order": 2}  # call -> confirmation -> body
    assert [props for _m, props in elicit.seen] == [["confirm"]]
    assert confirm_messages(elicit)[0].endswith("Shall I place the order?")
    await assert_grounded(stack.deps, data)


async def test_three_rounds_when_intake_is_missing(stack: Stack, elicit: ElicitationScript) -> None:
    order = {**SMALL_CAKE, "details": {"servings": 10}}
    async with stack.client(elicit) as client:
        data = structured(await client.call_tool("place_custom_order", order))
    assert data["status"] == "confirmed"
    assert stack.counter.calls == {"place_custom_order": 3}  # call -> intake -> confirmation -> body
    assert [props for _m, props in elicit.seen] == [["customer_name", "customer_phone"], ["confirm"]]
    [stored] = await commitments(stack, BAKERY)
    assert stored.intake["customer_name"] == "Sam Lee" and stored.intake["customer_phone"] == "+12175550100"


async def test_missing_choices_return_needs_info_without_questions(stack: Stack, elicit: ElicitationScript) -> None:
    order = {**SMALL_CAKE, "choices": {"size": "six inch round"}}
    async with stack.client(elicit) as client:
        data = structured(await client.call_tool("place_custom_order", order))
    assert data["status"] == "needs_info" and data["missing_fields"] == ["flavor", "filling", "frosting"]
    assert elicit.seen == [] and stack.counter.calls == {"place_custom_order": 1}
    assert await commitments(stack, BAKERY) == []


# ---------------------------------------------------------------------------- no confirmation, no write


@pytest.mark.parametrize("answer", ["decline", "cancel", {"confirm": False}])
async def test_declined_confirmation_writes_nothing(stack: Stack, elicit: ElicitationScript, answer: Any) -> None:
    elicit.answers.append(answer)
    async with stack.client(elicit) as client:
        data = structured(await client.call_tool("place_custom_order", BIG_CAKE))
    assert data["status"] == "not_confirmed" and data["speak"] == "Okay, I haven't placed the order."
    assert data["reference"] is None and data["task_id"] is None
    assert await commitments(stack, BAKERY) == []


async def test_declined_intake_writes_nothing(stack: Stack, elicit: ElicitationScript) -> None:
    elicit.answers.append("decline")
    async with stack.client(elicit) as client:
        data = structured(await client.call_tool("place_custom_order", {**SMALL_CAKE, "details": {"servings": 10}}))
    assert data["status"] == "needs_info" and data["missing_fields"] == ["customer_name", "customer_phone"]
    assert data["speak"] == "I still need name for the order and phone number."
    assert await commitments(stack, BAKERY) == []


async def test_legacy_client_gets_the_same_questions(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit, mode="legacy") as client:
        assert client.protocol_version == "2025-11-25"
        data = structured(await client.call_tool("place_custom_order", {**SMALL_CAKE, "details": {"servings": 10}}))
        assert data["status"] == "confirmed"
        assert [props for _m, props in elicit.seen] == [["customer_name", "customer_phone"], ["confirm"]]
        elicit.answers.append("decline")
        declined = structured(await client.call_tool("book", {**MENS_CUT, "details": FULL_SALON_DETAILS}))
    assert declined["status"] == "not_confirmed"
    assert len(await commitments(stack, BAKERY)) == 1 and await commitments(stack, SALON) == []


# ---------------------------------------------------------------------------- idempotency


async def test_retries_replay_the_same_commitment(stack: Stack, elicit: ElicitationScript) -> None:
    booking = {**MENS_CUT, "details": FULL_SALON_DETAILS}
    async with stack.client(elicit) as client:
        first = structured(await client.call_tool("book", booking))
        second = structured(await client.call_tool("book", booking))
    assert first["status"] == "confirmed" and first["replayed"] is False
    assert second["status"] == "confirmed" and second["replayed"] is True
    # Without auth the replay carries no capabilities: anyone repeating these details would get them.
    assert first["reference"] and second["reference"] is None and second["commitment"] is None
    assert not second["card"]["actions"] and "Reference" not in {line["label"] for line in second["card"]["lines"]}
    assert len(confirm_messages(elicit)) == 1  # the replay comes from the full slot, without a second question
    assert len(await commitments(stack, SALON)) == 1
    await assert_grounded(stack.deps, second)


async def test_retry_of_a_request_completed_through_the_form_replays(stack: Stack, elicit: ElicitationScript) -> None:
    """The intake answers are part of the idempotency key, so a retry whose slot is now full (our own
    hold) must still collect them to be recognised; otherwise it would wrongly say 'already taken'."""
    async with stack.client(elicit) as client:
        first = structured(await client.call_tool("book", MENS_CUT))
        second = structured(await client.call_tool("book", MENS_CUT))
    assert first["status"] == "confirmed"
    assert second["status"] == "confirmed" and second["replayed"] is True
    assert first["reference"] and second["reference"] is None
    assert [props for _m, props in elicit.seen].count(["customer_email", "customer_name", "customer_phone", "stylist"]) == 2
    assert len(await commitments(stack, SALON)) == 1


async def test_someone_elses_full_slot_is_not_asked_for_details(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        await client.call_tool("book", {**MENS_CUT, "details": FULL_SALON_DETAILS})
        stack.counter.reset()
        elicit.seen.clear()
        other = {**FULL_SALON_DETAILS, "customer_name": "Alex", "customer_email": "alex@example.com"}
        data = structured(await client.call_tool("book", {**MENS_CUT, "details": other}))
    assert data["status"] == "unavailable" and data["speak"].startswith("That time is already taken.")
    assert elicit.seen == [] and stack.counter.calls == {"book": 1}


async def test_concurrent_identical_orders_commit_once(stack: Stack, elicit: ElicitationScript) -> None:
    results: list[dict[str, Any]] = []
    async with stack.client(elicit) as client:

        async def order() -> None:
            results.append(structured(await client.call_tool("place_custom_order", SMALL_CAKE)))

        async with anyio.create_task_group() as group:
            for _ in range(4):
                group.start_soon(order)
    assert sorted(r["replayed"] for r in results) == [False, True, True, True]
    assert [r["reference"] is None for r in results if r["replayed"]] == [True, True, True]
    assert [r for r in results if not r["replayed"]][0]["reference"]
    assert len(await commitments(stack, BAKERY)) == 1


# ---------------------------------------------------------------------------- owner approval as a task


async def _approve_when_pending(stack: Stack, business_id: str, decision: OwnerDecisionInput) -> None:
    for _ in range(400):
        items, _ = await stack.deps.commitments.list_for_business(business_id, statuses=["pending_owner"])
        if items:
            await stack.deps.owner.decide(business_id, items[0].commitment_id, decision)
            return
        await anyio.sleep(0.01)
    raise AssertionError("no pending commitment appeared")


async def test_opted_in_client_receives_the_owner_decision(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit, mode="tasks") as client:
        async with anyio.create_task_group() as group:
            group.start_soon(_approve_when_pending, stack, BAKERY, OwnerDecisionInput(decision="approve", note="See you Sunday"))
            result = await client.call_tool("place_custom_order", BIG_CAKE)
    data = structured(result)
    assert data["status"] == "approved"
    assert data["speak"] == (
        "Good news: Rosa confirmed your custom cake for Sunday, October 18. The total is $120. Rosa says: See you Sunday."
    )
    assert data["task_id"] and data["commitment"]["decision"] == "approve"
    [message] = confirm_messages(elicit)
    assert message.endswith("Shall I send it to Rosa?")
    record = await stack.deps.task_manager.get(data["task_id"])
    assert record.status == "completed" and record.labels["business_id"] == BAKERY
    await assert_grounded(stack.deps, data)


async def test_plain_client_polls_get_task(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        assert created["status"] == "pending_owner" and created["task_id"]
        assert created["speak"] == (
            "I've sent your request to Rosa, who usually replies within 24 hours. Nothing is confirmed until Rosa approves it."
        )
        assert created["card"]["actions"][0] == {"label": "Check status", "tool": "get_task", "arguments": {"task_id": created["task_id"]}}
        waiting = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
        assert waiting["task_status"] == "working" and waiting["result"] is None
        assert waiting["status_message"] == "Waiting for Rosa, who usually replies within 24 hours."
        assert waiting["commitment"]["reference"] == created["reference"]
        await stack.deps.owner.decide(BAKERY, created["reference"], OwnerDecisionInput(decision="decline", note="Fully booked"))
        done = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
    assert done["task_status"] == "completed" and done["business_id"] == BAKERY
    assert done["result"]["status"] == "declined"
    assert done["speak"] == done["result"]["speak"] == (
        "Sorry, Rosa can't take your custom cake for Sunday, October 18. Rosa says: Fully booked."
    )
    await assert_grounded(stack.deps, done)


async def test_get_task_shows_owner_questions(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        await stack.deps.owner.decide(
            BAKERY, created["reference"], OwnerDecisionInput(decision="request_info", question="Is Sunday a firm date?")
        )
        data = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
    assert data["task_status"] == "input_required"
    assert data["speak"] == (
        "Rosa needs a bit more information: Is Sunday a firm date? Tell me your answer and I'll pass it on."
    )
    assert data["card"]["actions"][0] == {
        "label": "Answer",
        "tool": "reply_to_owner",
        "arguments": {"task_id": created["task_id"]},
        "input": {"key": "answer", "label": "Your answer", "max_length": 1000},
    }
    assert data["card"]["actions"][1] == {"label": "Check again", "tool": "get_task", "arguments": {"task_id": created["task_id"]}}


@pytest.mark.parametrize("mode", ["plain", "legacy"])
async def test_customers_without_tasks_can_answer_the_owner(stack: Stack, elicit: ElicitationScript, mode: str) -> None:
    """request_info must not be a dead end for the sync fallback (the primary contract for voice hosts)."""
    async with stack.client(elicit, mode=mode) as client:
        created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        await stack.deps.owner.decide(
            BAKERY, created["reference"], OwnerDecisionInput(decision="request_info", question="Is Sunday a firm date?")
        )
        replied = structured(
            await client.call_tool("reply_to_owner", {"task_id": created["task_id"], "answer": "Yes, Sunday is firm."})
        )
        assert replied["task_status"] == "working"
        assert replied["speak"] == "Thanks, I've passed that on to Rosa. I'll let you know what they decide."
        again = await client.call_tool("reply_to_owner", {"task_id": created["task_id"], "answer": "Again"})
        assert again.is_error  # nothing is waiting for an answer any more
    [item] = (await stack.deps.owner.inbox(BAKERY))[0]
    assert item.commitment.status == "pending_owner"
    assert item.commitment.customer_input == {"owner_question_1": {"answer": "Yes, Sunday is firm."}}
    approved = await stack.deps.owner.decide(BAKERY, created["reference"], OwnerDecisionInput(decision="approve"))
    assert approved.status == "approved"


async def test_an_unanswered_owner_question_expires_with_its_own_wording(stack: Stack, elicit: ElicitationScript) -> None:
    from datetime import timedelta

    async with stack.client(elicit) as client:
        created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        await stack.deps.owner.decide(
            BAKERY, created["reference"], OwnerDecisionInput(decision="request_info", question="Is Sunday a firm date?")
        )
        stack.deps.clock.advance(timedelta(days=3))
        assert await stack.deps.owner.expire_due() == 1
        done = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
    assert done["speak"] == "Your request lapsed because Rosa's question wasn't answered in time."


# ---------------------------------------------------------------------------- round 2: request_info

ASK_FIRM_DATE = OwnerDecisionInput(decision="request_info", question="Is Sunday a firm date?")
PASSED_ON = "I've passed your answer on to Rosa, who usually replies within 24 hours. Nothing is confirmed until Rosa approves it."


async def first_commitment(stack: Stack, business_id: str) -> str:
    """The reference of the first commitment stored for `business_id` (waits for the tool to write it)."""
    with anyio.fail_after(5):
        while True:
            items, _ = await stack.deps.commitments.list_for_business(business_id)
            if items:
                return str(items[0].commitment_id)
            await anyio.sleep(0.01)


@pytest.mark.parametrize("mode", ["plain", "legacy"])
async def test_round2_request_info_get_task_after_reply_does_not_repeat_the_question(
    stack: Stack, elicit: ElicitationScript, mode: str
) -> None:
    """R1: after reply_to_owner, get_task kept reading Rosa's question again ('More info needed') until
    Rosa opened her inbox, and a second answer was refused."""
    async with stack.client(elicit, mode=mode) as client:
        created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        await stack.deps.owner.decide(BAKERY, created["reference"], ASK_FIRM_DATE)
        structured(await client.call_tool("reply_to_owner", {"task_id": created["task_id"], "answer": "Yes, Sunday is firm."}))
        again = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
    assert again["task_status"] == "working" and again["commitment"]["status"] == "pending_owner"
    assert again["speak"] == PASSED_ON
    lines = {line["label"]: line["value"] for line in again["card"]["lines"]}
    assert lines["Status"] == "Waiting for the owner"
    assert [action["label"] for action in again["card"]["actions"]] == ["Check again"]
    stored = await stack.deps.commitments.get(BAKERY, created["reference"])
    assert stored is not None and stored.status == "pending_owner"  # stored before Rosa opens the inbox
    assert stored.customer_input == {"owner_question_1": {"answer": "Yes, Sunday is firm."}}
    assert stored.decision is not None and stored.decision.answered_at == stack.deps.clock.now()
    await assert_grounded(stack.deps, again)


async def test_round2_request_info_get_task_shows_a_reply_not_stored_yet_without_writing(
    stack: Stack, elicit: ElicitationScript
) -> None:
    """A reply on the task that nothing folded yet (e.g. a hook that failed) is rendered as folded, but
    get_task stays read-only."""
    async with stack.client(elicit) as client:
        created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        await stack.deps.owner.decide(BAKERY, created["reference"], ASK_FIRM_DATE)
        await stack.deps.task_manager.submit_input(
            created["task_id"], "anonymous", {"owner_question_1": {"action": "accept", "content": {"answer": "Yes"}}}
        )
        before = await stack.deps.commitments.get(BAKERY, created["reference"])
        data = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
    assert before is not None and before.status == "info_requested"
    assert data["commitment"]["status"] == "pending_owner" and data["speak"] == PASSED_ON
    assert await stack.deps.commitments.get(BAKERY, created["reference"]) == before


async def test_round2_request_info_answered_question_then_expiry_keeps_the_answer(
    stack: Stack, elicit: ElicitationScript
) -> None:
    """R2: the customer answered, Rosa never looked, the request expired. The answer was dropped and the
    customer heard that Rosa's question went unanswered."""
    from datetime import timedelta

    async with stack.client(elicit) as client:
        created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        await stack.deps.owner.decide(BAKERY, created["reference"], ASK_FIRM_DATE)
        structured(await client.call_tool("reply_to_owner", {"task_id": created["task_id"], "answer": "Yes, Sunday is firm."}))
        stack.deps.clock.advance(timedelta(days=3))
        assert await stack.deps.owner.expire_due() == 1
        done = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
    stored = await stack.deps.commitments.get(BAKERY, created["reference"])
    assert stored is not None and stored.status == "expired"
    assert stored.customer_input == {"owner_question_1": {"answer": "Yes, Sunday is firm."}}
    assert done["task_status"] == "completed" and done["result"]["status"] == "expired"
    assert done["speak"] == "Rosa hasn't answered yet, so your request isn't confirmed."


async def test_round2_request_info_tasks_client_answer_is_stored_at_once_and_survives_expiry(
    stack: Stack, elicit: ElicitationScript
) -> None:
    """The same with a tasks-extension client answering through tasks/update: the input hook stores the
    answer at once, and the expired result the client finally receives does not blame the customer."""
    from datetime import timedelta

    import mcp_types as types
    from mcp import Client
    from mcp_tasks.client import TasksClientExtension
    from support import apps_advertisement

    async def callback(ctx: Any, params: types.ElicitRequestParams) -> Any:
        if "asks:" in params.message:  # Rosa's question, relayed through input_required
            return types.ElicitResult(action="accept", content={"answer": "Yes, Sunday is firm."})
        return await elicit(ctx, params)

    stored_before_expiry: list[Any] = []

    async def rosa_asks_then_the_request_expires() -> None:
        reference = await first_commitment(stack, BAKERY)
        await stack.deps.owner.decide(BAKERY, reference, ASK_FIRM_DATE)
        with anyio.fail_after(5):
            while (stored := await stack.deps.commitments.get(BAKERY, reference)) is None or stored.status != "pending_owner":
                await anyio.sleep(0.01)
        stored_before_expiry.append(stored)
        stack.deps.clock.advance(timedelta(hours=49))
        assert await stack.deps.owner.expire_due() == 1

    ext = TasksClientExtension(default_poll_interval_s=0.02, max_wait_s=10)
    async with Client(stack.server, elicitation_callback=callback, cache=None, extensions=[ext, apps_advertisement()]) as client:
        async with anyio.create_task_group() as tg:
            tg.start_soon(rosa_asks_then_the_request_expires)
            data = structured(await client.call_tool("place_custom_order", BIG_CAKE))
    [before] = stored_before_expiry
    assert before.customer_input == {"owner_question_1": {"answer": "Yes, Sunday is firm."}}
    assert before.decision is not None and before.decision.answered_at is not None
    assert data["status"] == "expired" and data["speak"] == "Rosa hasn't answered yet, so your request isn't confirmed."
    stored = await stack.deps.commitments.get(BAKERY, before.commitment_id)
    assert stored is not None and stored.status == "expired" and stored.customer_input == before.customer_input


async def test_round2_request_info_the_card_answer_action_collects_its_input(stack: Stack, elicit: ElicitationScript) -> None:
    """R3: the task card's Answer button called reply_to_owner without `answer`, so it always failed.
    The action now declares the text it needs; sending it as declared works."""
    async with stack.client(elicit) as client:
        created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        await stack.deps.owner.decide(BAKERY, created["reference"], ASK_FIRM_DATE)
        status = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
        [answer] = [action for action in status["card"]["actions"] if action.get("input")]
        assert answer["tool"] == "reply_to_owner" and answer["input"]["key"] == "answer"
        schema = {t.name: t for t in (await client.list_tools()).tools}["reply_to_owner"].input_schema
        assert answer["input"]["key"] in schema["required"]
        assert schema["properties"]["answer"]["maxLength"] == answer["input"]["max_length"]
        sent = {**answer["arguments"], answer["input"]["key"]: "Yes, Sunday is firm."}  # what the card sends
        replied = structured(await client.call_tool(answer["tool"], sent))
    assert replied["task_status"] == "working"
    assert not [action for action in replied["card"]["actions"] if action.get("input")]


async def test_round2_request_info_a_tasks_client_that_cannot_answer_leaves_the_task_for_reply_to_owner(
    stack: Stack, elicit: ElicitationScript
) -> None:
    """R4: a host that takes task handles but cannot show a free-text question used to cancel the order.
    With `cancel_on_unanswerable=False` the task stays waiting and the answer can come via reply_to_owner."""
    import mcp_types as types
    from mcp import Client
    from mcp_tasks.client import TaskInputUnanswerable, TasksClientExtension
    from support import apps_advertisement

    async def cannot_answer(ctx: Any, params: types.ElicitRequestParams) -> Any:
        if "asks:" in params.message:
            return types.ErrorData(code=types.INVALID_REQUEST, message="free-text questions are not supported here")
        return await elicit(ctx, params)

    async def rosa_asks() -> None:
        await stack.deps.owner.decide(BAKERY, await first_commitment(stack, BAKERY), ASK_FIRM_DATE)

    ext = TasksClientExtension(default_poll_interval_s=0.02, max_wait_s=10, cancel_on_unanswerable=False)
    async with Client(stack.server, elicitation_callback=cannot_answer, cache=None, extensions=[ext, apps_advertisement()]) as client:
        async with anyio.create_task_group() as tg:
            tg.start_soon(rosa_asks)
            with pytest.raises(TaskInputUnanswerable) as raised:
                await client.call_tool("place_custom_order", BIG_CAKE)
    task_id = raised.value.task_id
    assert list(raised.value.input_requests) == ["owner_question_1"]
    assert (await stack.deps.task_manager.get(task_id)).status == "input_required"
    reference = await first_commitment(stack, BAKERY)
    stored = await stack.deps.commitments.get(BAKERY, reference)
    assert stored is not None and stored.status == "info_requested" and stored.task_id == task_id

    async with stack.client(elicit) as plain:  # the app falls back to the sync tools on the same task id
        waiting = structured(await plain.call_tool("get_task", {"task_id": task_id}))
        assert waiting["task_status"] == "input_required"
        replied = structured(await plain.call_tool("reply_to_owner", {"task_id": task_id, "answer": "Yes, Sunday is firm."}))
        assert replied["task_status"] == "working"
        await stack.deps.owner.decide(BAKERY, reference, OwnerDecisionInput(decision="approve"))
        done = structured(await plain.call_tool("get_task", {"task_id": task_id}))
    assert done["task_status"] == "completed" and done["result"]["status"] == "approved"
    approved = await stack.deps.commitments.get(BAKERY, reference)
    assert approved is not None and approved.customer_input == {"owner_question_1": {"answer": "Yes, Sunday is firm."}}


async def test_unknown_task_ids_are_indistinguishable(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        result = await client.call_tool("get_task", {"task_id": "t_doesnotexist"})
    assert result.is_error and result.content[0].text.endswith("Unknown or expired task")


async def test_retried_approval_request_replays_and_keeps_its_task(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        first = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        second = structured(await client.call_tool("place_custom_order", BIG_CAKE))
    assert second["replayed"] is True and second["status"] == "pending_owner"
    assert second["task_id"] is None  # an anonymous replay never hands out the task capability
    assert (await stack.deps.task_manager.get(first["task_id"])).status == "working"
    assert len(await commitments(stack, BAKERY)) == 1


async def test_lost_capacity_race_leaves_the_fresh_task_for_a_retry(
    stack: Stack, elicit: ElicitationScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[str] = []
    real_ensure = stack.deps.tasks.ensure_task

    async def spy_ensure(**kwargs: Any) -> Any:
        record = await real_ensure(**kwargs)
        created.append(record.task_id)
        return record

    async def lost_race(profile: Any, plan: Any, *, task_id: str | None) -> CommitOutcome:
        # Another customer took the last cake of the day between our plan and our commit.
        return CommitOutcome(commitment=None, problem=PlanProblem(code="capacity_full", detail="taken"))

    monkeypatch.setattr(stack.deps.tasks, "ensure_task", spy_ensure)
    monkeypatch.setattr(stack.deps.orders, "commit", lost_race)
    async with stack.client(elicit) as client:
        data = structured(await client.call_tool("place_custom_order", BIG_CAKE))
    assert data["status"] == "unavailable" and data["speak"] == "That day is fully booked."
    assert data["task_id"] is None and len(created) == 1
    record = await stack.deps.task_manager.get(created[0])
    assert record.status == "working"  # never handed out; an identical retry reuses it, else it expires
    assert record.arguments == {"business_id": BAKERY, "module_id": "custom-cakes", "kind": "custom_order"}
    assert await commitments(stack, BAKERY) == []


async def test_round2_capacity_race_an_identical_anonymous_request_hitting_duplicate_gets_no_task_handle(
    stack: Stack, elicit: ElicitationScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranger's identical request whose replay check missed the victim's commit (the race window,
    simulated by a replay that finds nothing) commits into `Duplicate`. Without auth it must be
    answered like a sequential replay: redacted and never adopted, so an opted-in client cannot poll
    or tasks/cancel the victim's task."""
    from mcp import Client
    from mcp_tasks.client import TaskHandle, TasksClientExtension
    from support import apps_advertisement

    async with stack.client(elicit) as client:
        victim = structured(await client.call_tool("place_custom_order", BIG_CAKE))

    async def no_replay(plan: Any) -> None:
        return None

    adopted: list[str] = []
    real_adopt = stack.deps.tasks.adopt

    def spy_adopt(record: Any) -> bool:
        adopted.append(record.task_id)
        return real_adopt(record)

    handles: list[TaskHandle] = []

    async def capture(state: Any) -> None:
        if isinstance(state, TaskHandle):
            handles.append(state)

    monkeypatch.setattr(stack.deps.orders, "replay", no_replay)
    monkeypatch.setattr(stack.deps.tasks, "adopt", spy_adopt)
    extension = TasksClientExtension(on_status=capture, default_poll_interval_s=0.02, max_wait_s=0.5)
    with anyio.fail_after(10):
        async with Client(
            stack.server, elicitation_callback=elicit, cache=None, extensions=[extension, apps_advertisement()]
        ) as stranger:
            leaked = structured(await stranger.call_tool("place_custom_order", BIG_CAKE))
    assert leaked["replayed"] is True and leaked["status"] == "pending_owner"
    assert leaked["task_id"] is None and leaked["reference"] is None and leaked["commitment"] is None
    assert victim["task_id"] not in str(leaked) and victim["reference"] not in str(leaked)
    assert adopted == [] and handles == []
    assert (await stack.deps.task_manager.get(victim["task_id"])).status == "working"
    [stored] = await commitments(stack, BAKERY)
    assert stored.commitment_id == victim["reference"] and stored.status == "pending_owner"


# ---------------------------------------------------------------------------- bookings


async def test_book_then_cancel_then_cancel_again(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        booked = structured(await client.call_tool("book", MENS_CUT))
        assert booked["status"] == "confirmed"
        assert booked["speak"] == (
            "You're booked: men's cut on Friday, October 16 at 10 am. The price is $42. "
            "Your booking code is 2 0 9 5, 5 7 1 6."
        )
        assert spoken_reference(booked["reference"]) == "20955716"
        assert booked["card"]["actions"][0]["tool"] == "cancel_booking"
        stack.counter.reset()
        cancelled = structured(await client.call_tool("cancel_booking", {"business_id": SALON, "reference": booked["reference"]}))
        assert cancelled["status"] == "cancelled" and stack.counter.calls == {"cancel_booking": 2}
        assert confirm_messages(elicit)[-1].startswith("Cancel men's cut, Friday, October 16 at 10 am.")
        asked = len(elicit.seen)
        stack.counter.reset()
        again = structured(await client.call_tool("cancel_booking", {"business_id": SALON, "reference": booked["reference"]}))
    assert again["status"] == "cancelled" and again["replayed"] is True
    assert len(elicit.seen) == asked and stack.counter.calls == {"cancel_booking": 1}  # no second question
    [stored] = await commitments(stack, SALON)
    assert stored.status == "cancelled"


async def test_cancel_declined_keeps_the_booking(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        booked = structured(await client.call_tool("book", MENS_CUT))
        elicit.answers.append({"confirm": False})
        kept = structured(await client.call_tool("cancel_booking", {"business_id": SALON, "reference": booked["reference"]}))
    assert kept["status"] == "not_confirmed" and kept["speak"] == "Okay, your booking stays as it is."
    assert kept["reference"] == booked["reference"]
    [stored] = await commitments(stack, SALON)
    assert stored.status == "confirmed"


async def test_cancel_unknown_reference(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        data = structured(await client.call_tool("cancel_booking", {"business_id": SALON, "reference": "c_nope"}))
    assert data["status"] == "unavailable" and data["speak"] == "I couldn't find a booking with that reference."


async def test_closed_day_offers_alternatives(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        data = structured(await client.call_tool("book", {**MENS_CUT, "start": "2026-10-19T10:00"}))
    assert data["status"] == "unavailable" and data["alternatives"]
    assert data["speak"].startswith("Quillfern Hair Studio is closed then. The nearest open times are Tuesday at 10 am")
    assert elicit.seen == []


async def test_color_booking_needs_the_owner(stack: Stack, elicit: ElicitationScript) -> None:
    booking = {"business_id": SALON, "item": "root touch up", "start": "2026-10-16T10:00"}
    async with stack.client(elicit) as client:
        data = structured(await client.call_tool("book", booking))
    assert data["status"] == "pending_owner" and data["task_id"]
    assert confirm_messages(elicit)[-1].endswith("Shall I send it to Inés?")


# ---------------------------------------------------------------------------- quotes and questions


async def test_quote_request_is_always_a_task(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        created = structured(await client.call_tool("request_quote", QUOTE))
        assert created["status"] == "pending_owner" and created["task_id"]
        assert confirm_messages(elicit)[-1].endswith("Shall I send it to Jo?")
        await stack.deps.owner.decide(
            HOME, created["reference"], OwnerDecisionInput(decision="quote", values={"reply_type": "fixed_price", "quoted_price": 240})
        )
        done = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
    assert done["result"]["status"] == "quoted"
    assert done["speak"] == "Kettlewick Home Repair sent a quote: $240."
    assert done["result"]["commitment"]["quote"]["amount_cents"] == 24000


async def test_ask_owner_needs_no_confirmation(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        created = structured(
            await client.call_tool("ask_owner", {"business_id": BAKERY, "question": "Do you sell gift cards?", "fact_ref": "not a ref"})
        )
        assert created["status"] == "pending_owner" and elicit.seen == []
        assert stack.counter.calls == {"ask_owner": 1}
        await stack.deps.owner.decide(
            BAKERY, created["reference"], OwnerDecisionInput(decision="answer", values={"answer": "Yes, in any amount."})
        )
        done = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
    assert done["result"]["status"] == "answered"
    assert done["speak"] == "Rosa says: Yes, in any amount."
    [stored] = await commitments(stack, BAKERY)
    assert stored.question == "Do you sell gift cards?" and stored.fact_ref is None
    await assert_grounded(stack.deps, done)


async def test_anonymous_identical_questions_stay_separate(stack: Stack, elicit: ElicitationScript) -> None:
    """Without auth two strangers asking the same common question must not share one question: the
    second would get the first one's task id and, later, their personal answer."""
    args = {"business_id": BAKERY, "question": "Is there parking near the shop?"}
    async with stack.client(elicit) as alice, stack.client(elicit) as mallory:
        first = structured(await alice.call_tool("ask_owner", args))
        second = structured(await mallory.call_tool("ask_owner", {**args, "question": "  Is there parking   near the shop? "}))
    assert first["replayed"] is False and second["replayed"] is False
    assert first["task_id"] != second["task_id"] and first["reference"] != second["reference"]
    assert len(await commitments(stack, BAKERY)) == 2


# ---------------------------------------------------------------------------- VUX-11: the spoken booking code


def _spoken_code(speak: str) -> str:
    """The 8 digits after 'Your booking code is' ('0 4 9 7, 3 0 7 3' -> '04973073')."""
    import re

    found = re.search(r"Your booking code is ([0-9 ,]+)\.", speak)
    assert found, speak
    return re.sub(r"[^0-9]", "", found.group(1))


async def test_round2_misc_low_spoken_code_cancels_the_booking(stack: Stack, elicit: ElicitationScript) -> None:
    """A voice host only reads `speak`: the code it hears is enough to cancel later, in a new session."""
    async with stack.client(elicit, mode="legacy") as client:
        booked = structured(await client.call_tool("book", MENS_CUT))
    code = _spoken_code(booked["speak"])
    assert len(code) == 8 and code == spoken_reference(booked["reference"])
    assert booked["reference"] not in booked["speak"]
    lines = {line["label"]: line["value"] for line in booked["card"]["lines"]}
    assert lines["Booking code"] == f"{code[:4]} {code[4:]}" and lines["Reference"] == booked["reference"]
    await assert_grounded(stack.deps, booked)
    code_fact = next(f for f in booked["facts"] if f["fact_ref"].endswith("#booking_code"))
    assert code_fact["value"] == code and code_fact["evidence_url"].startswith("urn:shopfront:commitment:")

    async with stack.client(elicit, mode="legacy") as later:  # the customer calls back
        cancelled = structured(await later.call_tool("cancel_booking", {"business_id": SALON, "reference": f"{code[:4]} {code[4:]}"}))
        assert cancelled["status"] == "cancelled" and cancelled["reference"] == booked["reference"]
        assert confirm_messages(elicit)[-1].startswith("Cancel men's cut, Friday, October 16 at 10 am.")
        asked = len(elicit.seen)
        again = structured(await later.call_tool("cancel_booking", {"business_id": SALON, "reference": f"{code[:4]}-{code[4:]}"}))
    assert again["status"] == "cancelled" and again["replayed"] is True and len(elicit.seen) == asked
    assert "booking code" not in cancelled["speak"].lower()  # a cancelled booking has nothing left to cancel
    [stored] = await commitments(stack, SALON)
    assert stored.status == "cancelled"


async def test_round2_misc_low_unknown_or_foreign_code_is_not_found(stack: Stack, elicit: ElicitationScript) -> None:
    not_found = "I couldn't find a booking with that reference."
    async with stack.client(elicit) as client:
        booked = structured(await client.call_tool("book", MENS_CUT))
        order = structured(await client.call_tool("place_custom_order", SMALL_CAKE))
        booking_code = spoken_reference(booked["reference"])
        order_code = spoken_reference(order["reference"])
        assert booking_code and order_code and booking_code != order_code
        assert "booking code" not in order["speak"].lower()  # only bookings have a code
        unknown = f"{(int(booking_code) + 1) % 32**5:08d}"
        attempts = [
            (SALON, unknown),  # nobody's code
            (BAKERY, order_code),  # a custom order is not a booking
            (BAKERY, booking_code),  # the right code at another business
            (SALON, booking_code + "9"),  # nine digits
            (SALON, "99999999"),  # beyond 32**5: no id has it
        ]
        results = [
            structured(await client.call_tool("cancel_booking", {"business_id": business, "reference": said}))
            for business, said in attempts
        ]
    for (business, said), result in zip(attempts, results, strict=True):
        assert result["status"] == "unavailable" and result["speak"] == not_found, (business, said)
    [stored] = await commitments(stack, SALON)
    assert stored.status == "confirmed"


async def test_round2_misc_low_anonymous_replay_does_not_speak_the_code(stack: Stack, elicit: ElicitationScript) -> None:
    """Without auth, anyone repeating the same request details gets the replay: it must not carry the
    code (a capability that cancels the booking), in speech, on the card or among the facts."""
    async with stack.client(elicit) as client:
        first = structured(await client.call_tool("book", MENS_CUT))
        second = structured(await client.call_tool("book", MENS_CUT))
    code = _spoken_code(first["speak"])
    assert second["replayed"] is True and second["status"] == "confirmed"
    assert "booking code" not in second["speak"].lower() and code not in str(second)
    assert f"{code[:4]} {code[4:]}" not in str(second)
    assert "Booking code" not in {line["label"] for line in second["card"]["lines"]}
    assert not [f for f in second["facts"] if f["fact_ref"].endswith("#booking_code")]
    await assert_grounded(stack.deps, second)


async def test_round2_misc_low_concurrent_duplicate_does_not_speak_the_code(
    stack: Stack, elicit: ElicitationScript, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other anonymous replay path: a duplicate whose replay check ran before the first commit."""
    async with stack.client(elicit) as client:
        first = structured(await client.call_tool("book", MENS_CUT))

        async def no_replay(plan: Any) -> CommitOutcome | None:
            return None

        monkeypatch.setattr(stack.deps.bookings, "replay", no_replay)
        second = structured(await client.call_tool("book", MENS_CUT))
    code = _spoken_code(first["speak"])
    assert second["replayed"] is True and second["reference"] is None
    assert "booking code" not in second["speak"].lower() and code not in str(second)


async def test_round2_misc_low_owner_approved_booking_speaks_code(stack: Stack, elicit: ElicitationScript) -> None:
    """A booking the owner approves gets its code in the task result (only the task id holder sees it)."""
    booking = {"business_id": SALON, "item": "root touch up", "start": "2026-10-16T10:00"}
    async with stack.client(elicit) as client:
        created = structured(await client.call_tool("book", booking))
        assert created["status"] == "pending_owner"
        assert "booking code" not in created["speak"].lower()  # not booked yet
        assert "Booking code" not in {line["label"] for line in created["card"]["lines"]}
        await stack.deps.owner.decide(SALON, created["reference"], OwnerDecisionInput(decision="approve"))
        done = structured(await client.call_tool("get_task", {"task_id": created["task_id"]}))
        assert done["result"]["status"] == "approved"
        code = _spoken_code(done["result"]["speak"])
        assert code == spoken_reference(created["reference"]) and done["speak"] == done["result"]["speak"]
        assert done["speak"].startswith("Good news: Inés confirmed your root touch-up")
        cancelled = structured(await client.call_tool("cancel_booking", {"business_id": SALON, "reference": code}))
    assert cancelled["status"] == "cancelled" and cancelled["reference"] == created["reference"]
    await assert_grounded(stack.deps, done)


async def test_round2_misc_low_anonymous_replay_of_an_approved_booking_carries_no_capability(
    stack: Stack, elicit: ElicitationScript
) -> None:
    """Withholding the code is moot if the replay names the booking elsewhere: an owner's note is cited
    as `<module>/<id>#owner_note` with evidence `urn:shopfront:owner-answer:<id>`. In a replay the id is
    replaced by its digest, so the note stays grounded but nothing in the result can cancel the booking."""
    from shopfront_server.observability import id_digest

    booking = {"business_id": SALON, "item": "root touch up", "start": "2026-10-16T10:00"}
    async with stack.client(elicit) as alice:
        created = structured(await alice.call_tool("book", booking))
    await stack.deps.owner.decide(SALON, created["reference"], OwnerDecisionInput(decision="approve", note="See you Friday"))
    async with stack.client(elicit) as stranger:
        replay = structured(await stranger.call_tool("book", booking))
    assert replay["replayed"] is True and replay["status"] == "approved"
    assert replay["speak"].endswith("Inés says: See you Friday.")
    code = spoken_reference(created["reference"])
    assert code is not None
    for secret in (created["reference"], created["task_id"], code, f"{code[:4]} {code[4:]}"):
        assert secret not in str(replay), secret
    alias = id_digest(created["reference"])
    note = next(f for f in replay["facts"] if f["fact_ref"].endswith("#owner_note"))
    assert note["fact_ref"] == f"appointments/{alias}#owner_note" and note["evidence_url"] == f"urn:shopfront:owner-answer:{alias}"
    assert f"urn:shopfront:owner-answer:{alias}" in {c["url"] for c in replay["card"]["citations"]}
    await assert_grounded(stack.deps, replay)
