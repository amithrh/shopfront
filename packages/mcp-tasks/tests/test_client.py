"""Client half: `TasksClientExtension`'s transparent resolver (polling, input rounds, timeouts, guards)."""

from __future__ import annotations

from datetime import timedelta

import anyio
import mcp_types as types
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS
from mcp.client.session import ClientRequestContext

from mcp_tasks import InMemoryTaskStore
from mcp_tasks.client import TaskHandle, TaskInputUnanswerable, TasksClientExtension, TaskState
from support import Harness, ManualClock, StaleReadStore

pytestmark = pytest.mark.anyio

States = list[TaskHandle | TaskState]


async def test_final_result_is_revalidated_against_the_output_schema(harness: Harness) -> None:
    """Gotcha 2: `Client.call_tool` re-validates the resolved result, so a bad shape raises."""

    async def complete_with_wrong_shape(state: TaskHandle | TaskState) -> None:
        if isinstance(state, TaskHandle):
            bad = types.CallToolResult(
                content=[types.TextContent(type="text", text="ok")], structured_content={"status": "yes"}
            )
            await harness.manager.complete(state.task_id, bad)

    ext = TasksClientExtension(on_status=complete_with_wrong_shape)
    async with Client(harness.server, extensions=[ext]) as client:
        with pytest.raises(RuntimeError, match="Invalid structured content returned by tool request_owner_approval"):
            await client.call_tool("request_owner_approval", {"summary": "s"})


async def test_max_wait_leaves_the_server_task_running_by_default(harness: Harness) -> None:
    """An owner may answer hours later: a local timeout must not throw that work away."""
    async with Client(harness.server, extensions=[TasksClientExtension(max_wait_s=0.15)]) as client:
        with pytest.raises(TimeoutError):
            await client.call_tool("request_owner_approval", {"summary": "never answered"})
    (record,) = harness.records()
    assert record.status == "working"


async def test_max_wait_cancels_the_server_task_when_asked(harness: Harness) -> None:
    ext = TasksClientExtension(max_wait_s=0.15, cancel_on_timeout=True)
    async with Client(harness.server, extensions=[ext]) as client:
        with pytest.raises(TimeoutError):
            await client.call_tool("request_owner_approval", {"summary": "never answered"})
    (record,) = harness.records()
    assert (record.status, record.status_message) == ("cancelled", "Cancelled by the client")


async def test_caller_cancellation_cancels_the_server_task(harness: Harness) -> None:
    async with Client(harness.server, extensions=[TasksClientExtension()]) as client:
        with anyio.move_on_after(0.15):
            await client.call_tool("request_owner_approval", {"summary": "abandoned"})
    (record,) = harness.records()
    assert (record.status, record.status_message) == ("cancelled", "Cancelled by the client")


async def test_update_that_loses_a_race_with_completion_still_returns_the_result(harness: Harness) -> None:
    """The owner decides while the user is still answering: the decision is the outcome, not an error."""

    async def owner(state: TaskHandle | TaskState) -> None:
        if isinstance(state, TaskHandle):
            await harness.ask(state.task_id, "Which flavour?")

    async def answer_late(ctx: ClientRequestContext, params: types.ElicitRequestParams) -> types.ElicitResult:
        await harness.approve(note="Approved anyway")  # completes the task before the answer lands
        return types.ElicitResult(action="accept", content={"answer": "Lemon"})

    ext = TasksClientExtension(on_status=owner)
    async with Client(harness.server, extensions=[ext], elicitation_callback=answer_late) as client:
        result = await client.call_tool("request_owner_approval", {"summary": "s"})
    assert result.is_error is False
    assert result.structured_content is not None and result.structured_content["note"] == "Approved anyway"
    (record,) = harness.records()
    assert record.status == "completed"


async def test_an_answer_the_server_rejects_is_raised_not_retried_forever(harness: Harness) -> None:
    async def owner(state: TaskHandle | TaskState) -> None:
        if isinstance(state, TaskHandle):
            await harness.ask(state.task_id, "Which flavour?")

    async def bad_answer(ctx: ClientRequestContext, params: types.ElicitRequestParams) -> types.ElicitResult:
        return types.ElicitResult(action="accept", content={"answer": "Lemon", "unrequested": "x"})

    ext = TasksClientExtension(on_status=owner, max_wait_s=5)
    async with Client(harness.server, extensions=[ext], elicitation_callback=bad_answer) as client:
        with pytest.raises(MCPError) as rejected:
            await client.call_tool("request_owner_approval", {"summary": "s"})
    assert rejected.value.error.code == INVALID_PARAMS and "unexpected fields" in rejected.value.error.message
    (record,) = harness.records()
    assert record.status == "input_required"  # an error is not a reason to cancel the server's task


async def test_failed_and_cancelled_tasks_resolve_to_error_results(harness: Harness) -> None:
    async def owner(state: TaskHandle | TaskState) -> None:
        if isinstance(state, TaskHandle):
            summary = (await harness.manager.get(state.task_id)).arguments["summary"]
            if summary == "fail":
                await harness.manager.fail(state.task_id, "Kitchen closed")
            else:
                await harness.manager.cancel(state.task_id, "Owner did not respond in time")

    async with Client(harness.server, extensions=[TasksClientExtension(on_status=owner)]) as client:
        failed = await client.call_tool("request_owner_approval", {"summary": "fail"})
        cancelled = await client.call_tool("request_owner_approval", {"summary": "cancel"})
    for result, text in ((failed, "Kitchen closed"), (cancelled, "Owner did not respond in time")):
        assert result.is_error is True
        assert isinstance(result.content[0], types.TextContent) and result.content[0].text == text


async def test_on_status_reports_changes_only_by_default(harness: Harness) -> None:
    seen: States = []

    async def owner(state: TaskHandle | TaskState) -> None:
        seen.append(state)
        if isinstance(state, TaskHandle):
            await harness.manager.set_message(state.task_id, "Owner opened the link")
        elif state.status_message == "Owner opened the link":
            await harness.approve(state.task_id)

    async with Client(harness.server, extensions=[TasksClientExtension(on_status=owner)]) as client:
        result = await client.call_tool("request_owner_approval", {"summary": "s"})
    assert result.is_error is False
    assert [(s.status, s.status_message) for s in seen] == [
        ("working", "Waiting for the owner: s"),
        ("working", "Owner opened the link"),
        ("completed", "Owner approved"),
    ]


async def test_a_second_question_with_a_reused_key_is_answered(harness: Harness) -> None:
    """Regression for the `answered` set: it resets once the task leaves `input_required`."""
    asked: list[str] = []
    steps = iter(["flavour", "size", "approve"])

    async def elicit(ctx: ClientRequestContext, params: types.ElicitRequestParams) -> types.ElicitResult:
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"answer": f"answer to {params.message}"})

    async def owner(state: TaskHandle | TaskState) -> None:
        if state.status != "working":
            return
        step = next(steps)
        if step == "flavour":
            await harness.ask(state.task_id, "Which flavour?", key="owner_question")
        elif step == "size":
            await harness.ask(state.task_id, "Which size?", key="owner_question")  # same key again
        else:
            await harness.approve(state.task_id, note="Both answered")

    ext = TasksClientExtension(on_status=owner, report_every_poll=True, max_wait_s=5)
    async with Client(harness.server, extensions=[ext], elicitation_callback=elicit) as client:
        result = await client.call_tool("request_owner_approval", {"summary": "cake"})
    assert result.structured_content is not None and result.structured_content["note"] == "Both answered"
    assert asked == ["Which flavour?", "Which size?"]
    (record,) = harness.records()
    assert record.input_responses["owner_question"]["content"] == {"answer": "answer to Which size?"}


async def test_unanswerable_input_request_cancels_the_task(harness: Harness) -> None:
    async def ask(state: TaskHandle | TaskState) -> None:
        if isinstance(state, TaskHandle):
            await harness.ask(state.task_id, "Which flavour?")

    async with Client(harness.server, extensions=[TasksClientExtension(on_status=ask)]) as client:  # no callback
        with pytest.raises(RuntimeError, match="declined"):
            await client.call_tool("request_owner_approval", {"summary": "cake"})
    (record,) = harness.records()
    assert record.status == "cancelled"


async def test_round2_request_info_unanswerable_input_can_leave_the_task_waiting(harness: Harness) -> None:
    """A host that takes task handles but cannot show a free-text question: with
    `cancel_on_unanswerable=False` the task is not thrown away, and the error names it so the
    application can answer another way (or cancel it itself)."""

    async def ask(state: TaskHandle | TaskState) -> None:
        if isinstance(state, TaskHandle):
            await harness.ask(state.task_id, "Which flavour?")

    async def no_free_text(ctx: ClientRequestContext, params: types.ElicitRequestParams) -> types.ErrorData:
        return types.ErrorData(code=types.INVALID_REQUEST, message="free text not supported")

    ext = TasksClientExtension(on_status=ask, cancel_on_unanswerable=False)
    async with Client(harness.server, extensions=[ext], elicitation_callback=no_free_text) as client:
        with pytest.raises(TaskInputUnanswerable, match="free text not supported") as raised:
            await client.call_tool("request_owner_approval", {"summary": "cake"})
    (record,) = harness.records()
    assert record.status == "input_required" and raised.value.task_id == record.task_id
    assert raised.value.key == "owner_question_1" and list(raised.value.input_requests) == ["owner_question_1"]


async def test_round2_request_info_unanswerable_input_still_cancels_by_default(harness: Harness) -> None:
    async def ask(state: TaskHandle | TaskState) -> None:
        if isinstance(state, TaskHandle):
            await harness.ask(state.task_id, "Which flavour?")

    async with Client(harness.server, extensions=[TasksClientExtension(on_status=ask)]) as client:  # no callback
        with pytest.raises(TaskInputUnanswerable) as raised:
            await client.call_tool("request_owner_approval", {"summary": "cake"})
    (record,) = harness.records()
    assert record.status == "cancelled" and raised.value.task_id == record.task_id


async def test_round2_request_info_a_failing_callback_cancels_even_when_unanswerable_tasks_are_kept(harness: Harness) -> None:
    """Only a declined request (an error reply) keeps the task; a callback that crashes still gives it up."""

    async def ask(state: TaskHandle | TaskState) -> None:
        if isinstance(state, TaskHandle):
            await harness.ask(state.task_id, "Which flavour?")

    async def crashes(ctx: ClientRequestContext, params: types.ElicitRequestParams) -> types.ElicitResult:
        raise ValueError("callback bug")

    ext = TasksClientExtension(on_status=ask, cancel_on_unanswerable=False)
    async with Client(harness.server, extensions=[ext], elicitation_callback=crashes) as client:
        with pytest.raises(ValueError, match="callback bug"):
            await client.call_tool("request_owner_approval", {"summary": "cake"})
    (record,) = harness.records()
    assert record.status == "cancelled"


async def test_stale_reads_are_skipped() -> None:
    clock = ManualClock(tick=timedelta(seconds=1))  # every read advances: strictly increasing timestamps
    store = StaleReadStore(InMemoryTaskStore())
    harness = Harness(clock=clock, store=store)
    seen: States = []

    async def owner(state: TaskHandle | TaskState) -> None:
        seen.append(state)
        if isinstance(state, TaskHandle):
            await harness.manager.set_message(state.task_id, "Owner opened the link")  # version 1
        elif state.status_message == "Owner opened the link":
            await harness.approve(state.task_id)  # version 2
            store.serve_stale(state.task_id, version=0)  # the next tasks/get hits a lagging replica

    ext = TasksClientExtension(on_status=owner, report_every_poll=True)
    async with Client(harness.server, extensions=[ext]) as client:
        result = await client.call_tool("request_owner_approval", {"summary": "s"})
    assert result.is_error is False
    assert [s.status_message for s in seen] == [
        "Waiting for the owner: s",
        "Owner opened the link",
        "Owner approved",
    ]  # the stale version-0 read never reached on_status


async def test_zero_poll_interval_still_resolves() -> None:
    harness = Harness(poll_interval_ms=0)
    polls: States = []

    async def owner(state: TaskHandle | TaskState) -> None:
        polls.append(state)
        if len(polls) == 4:
            await harness.approve(state.task_id)

    ext = TasksClientExtension(on_status=owner, report_every_poll=True)
    async with Client(harness.server, extensions=[ext]) as client:
        result = await client.call_tool("request_owner_approval", {"summary": "s"})
    assert result.is_error is False and len(polls) == 5
