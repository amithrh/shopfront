"""Server half of the extension, end to end over the in-process transport (protocol 2026-07-28).

Ported from the verified prototype scenarios and the verification log in docs/research/mcp-tasks.md
(sections 2.5, 2.10 and V1-V7).
"""

from __future__ import annotations

from typing import Any

import anyio
import mcp_types as types
import pytest
from mcp import Client
from mcp.client.session import ClientRequestContext
from mcp.shared.exceptions import MCPError
from mcp_types import INTERNAL_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND

from mcp_tasks import InMemoryTaskStore, InvalidTransition, TaskManager, TasksExtension
from mcp_tasks.client import (
    TaskHandle,
    TasksClientExtension,
    TaskState,
    UpdateTaskRequest,
    _UpdateParams,
    answer_input_requests,
    cancel_task,
    get_task,
    list_tasks,
    update_task,
)
from support import Harness, ManualClock, check_get_body, check_handle_body

pytestmark = pytest.mark.anyio


def tasks_client(harness: Harness, **kwargs: Any) -> Client:
    """A 2026-07-28 in-process client that advertises the tasks extension."""
    ext = kwargs.pop("extension", None) or TasksClientExtension()
    return Client(harness.server, extensions=[ext], **kwargs)


async def answer_flavour(ctx: ClientRequestContext, params: types.ElicitRequestParams) -> types.ElicitResult:
    return types.ElicitResult(action="accept", content={"answer": "Chocolate"})


async def open_handle(client: Client, summary: str = "cake") -> TaskHandle:
    handle = await client.session.call_tool("request_owner_approval", {"summary": summary}, allow_claimed=True)
    assert isinstance(handle, TaskHandle)
    return handle


# --------------------------------------------------------------------------- happy paths


async def test_transparent_call_tool_waits_for_owner_and_reports_progress(harness: Harness) -> None:
    progress: list[tuple[float, float | None, str | None]] = []
    seen: list[TaskHandle | TaskState] = []

    async def on_progress(value: float, total: float | None, message: str | None) -> None:
        progress.append((value, total, message))

    async def on_status(state: TaskHandle | TaskState) -> None:
        seen.append(state)
        if len(seen) == 3:  # the handle plus two `working` polls, then the owner approves
            await harness.approve(state.task_id, note="See you Saturday")

    ext = TasksClientExtension(on_status=on_status, report_every_poll=True)
    async with tasks_client(harness, extension=ext) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_capabilities.extensions == {"io.modelcontextprotocol/tasks": {}}
        result = await client.call_tool("request_owner_approval", {"summary": "cake"}, progress_callback=on_progress)
    assert result.is_error is False
    assert result.structured_content == {"status": "approved", "note": "See you Saturday", "reference": seen[0].task_id}
    assert progress == [(0.0, 1.0, "Contacting the owner")]  # progress only reaches the create round
    assert isinstance(seen[0], TaskHandle) and seen[0].status == "working"
    assert [s.status for s in seen[1:]] == ["working", "working", "completed"]
    assert seen[0].status_message == "Waiting for the owner: cake"


async def test_manual_handle_input_required_update_working_completed(harness: Harness) -> None:
    async with tasks_client(harness, elicitation_callback=answer_flavour) as client:
        session = client.session
        handle = await open_handle(client)
        check_handle_body(handle.model_dump(by_alias=True, mode="json", exclude_none=False))
        tid = handle.task_id
        assert (await get_task(session, tid)).status == "working"

        await harness.ask(tid, "Which flavour?")
        state = await get_task(session, tid)
        assert state.status == "input_required" and state.status_message == "Which flavour?"
        assert state.input_requests is not None and list(state.input_requests) == ["owner_question_1"]
        await update_task(session, tid, await answer_input_requests(session, state))

        state = await get_task(session, tid)
        assert state.status == "working" and state.status_message == "Input received; continuing"
        assert state.input_requests is None
        stored = await harness.manager.get(tid)
        assert stored.input_responses == {"owner_question_1": {"action": "accept", "content": {"answer": "Chocolate"}}}

        await harness.approve(tid, note="Chocolate it is")
        state = await get_task(session, tid)
        assert state.status == "completed" and state.result is not None
        assert state.result.structured_content == {"status": "approved", "note": "Chocolate it is", "reference": tid}
        check_get_body(state.model_dump(by_alias=True, mode="json", exclude_none=True) | {"ttlMs": state.ttl_ms})


async def test_tasks_list_paginates_oldest_first() -> None:
    harness = Harness(list_page_size=2)
    async with tasks_client(harness) as client:
        created = [(await open_handle(client, f"r{i}")).task_id for i in range(5)]
        pages: list[list[str]] = []
        cursor: str | None = None
        while True:
            listed = await list_tasks(client.session, cursor)
            pages.append([t.task_id for t in listed.tasks])
            cursor = listed.next_cursor
            if cursor is None:
                break
        with pytest.raises(MCPError) as bad:
            await list_tasks(client.session, "not-a-cursor")
    assert [len(p) for p in pages] == [2, 2, 1]
    assert [tid for page in pages for tid in page] == created
    assert bad.value.code == INVALID_PARAMS


def test_list_page_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        TasksExtension(TaskManager(InMemoryTaskStore()), list_page_size=0)


async def test_tasks_list_can_be_disabled() -> None:
    harness = Harness()
    harness.tasks = TasksExtension(harness.manager, serve_list=False)
    assert "tasks/list" not in {b.method for b in harness.tasks.methods()}
    assert {b.method for b in Harness().tasks.methods()} == {"tasks/get", "tasks/update", "tasks/cancel", "tasks/list"}


# --------------------------------------------------------------------------- cancellation and errors


async def test_cancel_then_owner_decision_double_cancel_and_unknown_ids(harness: Harness) -> None:
    async with tasks_client(harness) as client:
        session = client.session
        handle = await open_handle(client)
        await cancel_task(session, handle.task_id)
        state = await get_task(session, handle.task_id)
        assert state.status == "cancelled" and state.status_message == "Cancelled by the client"
        with pytest.raises(InvalidTransition):
            await harness.approve(handle.task_id)
        with pytest.raises(MCPError) as second:
            await cancel_task(session, handle.task_id)
        assert second.value.code == INVALID_PARAMS and "cancelled" in second.value.message
        for call in (
            lambda: get_task(session, "does-not-exist"),
            lambda: cancel_task(session, "does-not-exist"),
            lambda: update_task(session, "does-not-exist", {"k": types.ElicitResult(action="decline")}),
        ):
            with pytest.raises(MCPError) as unknown:
                await call()
            assert unknown.value.code == INVALID_PARAMS
            assert unknown.value.message == "Unknown or expired taskId"


async def test_ttl_expiry_answers_invalid_params(clock: ManualClock) -> None:
    harness = Harness(clock=clock, ttl_ms=60_000)
    async with tasks_client(harness) as client:
        session = client.session
        handle = await open_handle(client)
        assert handle.ttl_ms == 60_000
        clock.advance(seconds=60)
        assert (await get_task(session, handle.task_id)).status == "working"  # still live at createdAt + ttl
        clock.advance(milliseconds=1)
        for call in (lambda: get_task(session, handle.task_id), lambda: cancel_task(session, handle.task_id)):
            with pytest.raises(MCPError) as expired:
                await call()
            assert expired.value.code == INVALID_PARAMS and expired.value.message == "Unknown or expired taskId"
        assert (await list_tasks(session)).tasks == []
    assert harness.records()[0].status == "working"  # still stored; only addressing stops


async def test_tasks_update_rejects_unknown_keys_malformed_answers_and_idle_tasks(harness: Harness) -> None:
    async with tasks_client(harness) as client:
        session = client.session
        handle = await open_handle(client)
        decline = {"owner_question_1": types.ElicitResult(action="decline")}
        with pytest.raises(MCPError) as idle:
            await update_task(session, handle.task_id, decline)
        assert idle.value.code == INVALID_PARAMS and "not waiting" in idle.value.message

        await harness.ask(handle.task_id, "Size?")
        with pytest.raises(MCPError) as unknown_key:
            await update_task(session, handle.task_id, {"zzz": types.ElicitResult(action="accept", content={})})
        assert unknown_key.value.code == INVALID_PARAMS and "unknown input keys" in unknown_key.value.message

        bogus = UpdateTaskRequest(
            params=_UpdateParams(task_id=handle.task_id, input_responses={"owner_question_1": {"bogus": 1}})
        )
        with pytest.raises(MCPError) as malformed:
            await session.send_request(bogus, types.EmptyResult)
        assert malformed.value.code == INVALID_PARAMS and malformed.value.message == "Malformed inputResponses"
        assert (await get_task(session, handle.task_id)).status == "input_required"  # nothing was recorded


# --------------------------------------------------------------------------- graceful degradation


@pytest.mark.parametrize("mode", ["no-extension", "legacy"])
async def test_graceful_degradation_gives_a_sync_result_and_no_task(harness: Harness, mode: str) -> None:
    if mode == "legacy":
        client = Client(harness.server, mode="legacy", extensions=[TasksClientExtension()])
    else:
        client = Client(harness.server)
    async with client:
        result = await client.call_tool("request_owner_approval", {"summary": "x"})
    assert result.structured_content == {"status": "pending", "note": "sync fallback", "reference": "n/a"}
    assert harness.records() == []


async def test_legacy_wire_has_no_tasks_methods(harness: Harness) -> None:
    async with Client(harness.server, mode="legacy", extensions=[TasksClientExtension()]) as client:
        assert client.protocol_version == "2025-11-25"
        for call in (
            lambda: get_task(client.session, "any"),
            lambda: cancel_task(client.session, "any"),
            lambda: list_tasks(client.session),
        ):
            with pytest.raises(MCPError) as missing:
                await call()
            assert missing.value.code == METHOD_NOT_FOUND


async def test_sync_fallback_still_creates_a_pollable_task_record(harness: Harness) -> None:
    async with Client(harness.server) as client:
        result = await client.call_tool("idempotent_approval", {"key": "k2"})
    assert result.structured_content == {"status": "pending", "note": "sync", "reference": "task-k2"}
    assert [r.task_id for r in harness.records()] == ["task-k2"]


async def test_caller_chosen_task_id_is_idempotent(harness: Harness) -> None:
    async with tasks_client(harness) as client:
        first = await client.session.call_tool("idempotent_approval", {"key": "k1"}, allow_claimed=True)
        second = await client.session.call_tool("idempotent_approval", {"key": "k1"}, allow_claimed=True)
        assert isinstance(first, TaskHandle) and isinstance(second, TaskHandle)
        assert first.task_id == second.task_id == "task-k1"
        await harness.approve("task-k1", note="done")
        third = await client.session.call_tool("idempotent_approval", {"key": "k1"}, allow_claimed=True)
        assert isinstance(third, TaskHandle) and third.status == "completed"  # a retry sees the outcome
        state = await get_task(client.session, "task-k1")
        assert state.result is not None and state.result.structured_content is not None
        assert state.result.structured_content["note"] == "done"
    assert len(harness.records()) == 1


async def test_reusing_an_expired_task_id_starts_a_fresh_task(clock: ManualClock) -> None:
    """An expired record is unaddressable, so a retry with its id gets a new live task, not a dead one."""
    harness = Harness(clock=clock, ttl_ms=1000)
    async with tasks_client(harness) as client:
        first = await client.session.call_tool("idempotent_approval", {"key": "old"}, allow_claimed=True)
        assert isinstance(first, TaskHandle)
        await harness.manager.cancel(first.task_id, "gone")
        clock.advance(seconds=5)
        retry = await client.session.call_tool("idempotent_approval", {"key": "old"}, allow_claimed=True)
        assert isinstance(retry, TaskHandle) and retry.task_id == "task-old" and retry.status == "working"
        assert (await get_task(client.session, "task-old")).status == "working"
    (record,) = harness.records()
    assert record.status == "working" and record.version == 2 and record.status_message == "Waiting"


# --------------------------------------------------------------------------- tool-side API


@pytest.mark.parametrize(
    ("kind", "client_text", "stored_message"),
    [
        ("value", "Error executing tool boom", "Error executing tool boom"),
        ("tool", "Error executing tool boom: SMS gateway down", "Error executing tool boom: SMS gateway down"),
    ],
)
async def test_tool_that_raises_after_defer_fails_its_task(
    harness: Harness, kind: str, client_text: str, stored_message: str
) -> None:
    async with tasks_client(harness) as client:
        result = await client.call_tool("boom", {"kind": kind})
    assert result.is_error is True
    assert isinstance(result.content[0], types.TextContent) and result.content[0].text == client_text
    (record,) = harness.records()
    assert record.status == "failed"
    assert record.error == {"code": INTERNAL_ERROR, "message": stored_message}
    assert "secret internal detail" not in str(record.error)  # a crash's text stays on the server


async def test_mcp_error_after_defer_fails_the_task_and_reaches_the_client(harness: Harness) -> None:
    async with tasks_client(harness) as client:
        with pytest.raises(MCPError) as raised:
            await client.call_tool("boom", {"kind": "mcp"})
    assert raised.value.code == INVALID_PARAMS and raised.value.message == "bad business id"
    (record,) = harness.records()
    assert (record.status, record.error) == ("failed", {"code": INTERNAL_ERROR, "message": "bad business id"})


async def test_reused_task_id_from_another_tool_is_a_duplicate(harness: Harness) -> None:
    async with Client(harness.server) as client:
        a = await client.call_tool("claim_shared_a", {})
        a_again = await client.call_tool("claim_shared_a", {})
        b = await client.call_tool("claim_shared_b", {})
    texts = [r.content[0].text for r in (a, a_again, b) if isinstance(r.content[0], types.TextContent)]
    assert texts == ["a: owns shared-id", "a: owns shared-id", "b: duplicate"]


async def test_current_call_records_every_call(harness: Harness) -> None:
    async with tasks_client(harness) as modern, Client(harness.server, mode="legacy") as legacy:
        opted = await modern.call_tool("whoami", {"tag": "m"})
        plain = await legacy.call_tool("whoami", {"tag": "l"})
    assert opted.structured_content is not None and plain.structured_content is not None
    assert opted.structured_content == {
        "tool_name": "whoami",
        "arguments": {"tag": "m"},
        "principal": "anonymous",
        "opted_in": True,
    }
    assert plain.structured_content["opted_in"] is False


async def test_tool_side_api_outside_a_tool_call(harness: Harness) -> None:
    record = await harness.manager.create(principal="anonymous", tool_name="t", arguments={}, status_message="x")
    assert harness.tasks.current_call() is None
    assert harness.tasks.adopt(record) is False
    assert await harness.tasks.defer(status_message="x") is None
    with pytest.raises(RuntimeError, match="ensure_task"):
        await harness.tasks.ensure_task(status_message="x")


async def test_concurrent_calls_get_independent_tasks(harness: Harness) -> None:
    handles: list[TaskHandle] = []
    async with tasks_client(harness) as client:

        async def one(i: int) -> None:
            handles.append(await open_handle(client, f"c{i}"))

        async with anyio.create_task_group() as tg:
            for i in range(8):
                tg.start_soon(one, i)
    assert len({h.task_id for h in handles}) == 8
    assert sorted(r.arguments["summary"] for r in harness.records()) == sorted(f"c{i}" for i in range(8))


# --------------------------------------------------------------------------- security regressions


async def test_tasks_list_refuses_anonymous_callers_by_default() -> None:
    """Without auth every caller is 'anonymous': listing would leak every caller's task ids."""
    harness = Harness(list_anonymous=False)
    async with tasks_client(harness) as victim:
        await open_handle(victim, "victim's cake")
    async with tasks_client(harness) as stranger:
        with pytest.raises(MCPError) as refused:
            await list_tasks(stranger.session)
    assert refused.value.code == INVALID_PARAMS and "authenticated" in refused.value.message


async def test_cancel_hook_sees_the_cancelled_record_and_its_failure_is_contained() -> None:
    seen: list[str] = []

    async def hook(record: Any) -> None:
        seen.append(f"{record.task_id}:{record.status}")
        raise RuntimeError("downstream outage")

    harness = Harness(on_cancel=hook)
    async with tasks_client(harness) as client:
        handle = await open_handle(client)
        await cancel_task(client.session, handle.task_id)
        assert (await get_task(client.session, handle.task_id)).status == "cancelled"
    assert seen == [f"{handle.task_id}:cancelled"]


async def test_round2_request_info_input_hook_sees_the_answered_record_and_its_failure_is_contained() -> None:
    """`on_input` runs after an accepted tasks/update (e.g. to store the answer with the work the task
    tracks); a rejected update never reaches it, and a failing hook does not undo the answer."""
    seen: list[tuple[str, str, dict[str, Any]]] = []

    async def hook(record: Any) -> None:
        seen.append((record.task_id, record.status, dict(record.input_responses)))
        raise RuntimeError("downstream outage")

    harness = Harness(on_input=hook)
    async with tasks_client(harness) as client:
        handle = await open_handle(client)
        await harness.ask(handle.task_id, "Which flavour?")
        with pytest.raises(MCPError):
            await update_task(client.session, handle.task_id, {"zzz": types.ElicitResult(action="accept", content={})})
        assert seen == []
        await update_task(
            client.session, handle.task_id, {"owner_question_1": types.ElicitResult(action="accept", content={"answer": "Lemon"})}
        )
        assert (await get_task(client.session, handle.task_id)).status == "working"
    assert seen == [(handle.task_id, "working", {"owner_question_1": {"action": "accept", "content": {"answer": "Lemon"}}})]


def _schema_question(schema: dict[str, Any]) -> types.ElicitRequest:
    return types.ElicitRequest(params=types.ElicitRequestFormParams(message="Q?", requested_schema=schema))


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ({"answer": "x" * 1001}, "longer than 1000"),
        ({"answer": "ok", "unrequested": "extra"}, "unexpected fields"),
        ({}, "answer is required"),
        ({"answer": 12345}, "must be a string"),
        ({"answer": "ok", "size": "huge"}, "not one of the offered choices"),
        ({"answer": "ok", "servings": True}, "must be an integer"),
        ({"answer": "ok", "servings": 500}, "at most 100"),
    ],
)
async def test_tasks_update_checks_answers_against_the_form(
    harness: Harness, content: dict[str, Any], error: str
) -> None:
    schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string", "maxLength": 1000},
            "size": {"type": "string", "enum": ["small", "large"]},
            "servings": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["answer"],
    }
    async with tasks_client(harness) as client:
        handle = await open_handle(client)
        await harness.manager.require_input(handle.task_id, {"q": _schema_question(schema)}, "Q?")
        with pytest.raises(MCPError) as rejected:
            await update_task(client.session, handle.task_id, {"q": types.ElicitResult(action="accept", content=content)})
        assert rejected.value.code == INVALID_PARAMS and error in rejected.value.message
        await update_task(
            client.session, handle.task_id, {"q": types.ElicitResult(action="accept", content={"answer": "fine"})}
        )
    stored = await harness.manager.get(handle.task_id)
    assert stored.status == "working" and stored.input_responses == {
        "q": {"action": "accept", "content": {"answer": "fine"}}
    }


async def test_unbounded_strings_are_capped_even_without_max_length(harness: Harness) -> None:
    async with tasks_client(harness) as client:
        handle = await open_handle(client)
        await harness.ask(handle.task_id, "Free text?")  # schema without maxLength
        huge = types.ElicitResult(action="accept", content={"answer": "x" * 200_000})
        with pytest.raises(MCPError) as rejected:
            await update_task(client.session, handle.task_id, {"owner_question_1": huge})
    assert rejected.value.code == INVALID_PARAMS


async def test_ensure_task_can_store_minimal_arguments_instead_of_the_raw_call() -> None:
    harness = Harness()

    @harness.server.tool()
    async def minimal(secret: str) -> str:
        """Stores only what it chooses to."""
        record = await harness.tasks.ensure_task(status_message="Waiting", arguments={"kind": "demo"})
        return record.task_id

    async with Client(harness.server) as client:
        await client.call_tool("minimal", {"secret": "card 4111111111111111"})
    (record,) = harness.records()
    assert record.arguments == {"kind": "demo"}


async def test_round2_misc_low_defer_can_store_minimal_arguments_too() -> None:
    harness = Harness()

    @harness.server.tool()
    async def deferred(secret: str) -> str:
        """Defers with only what it chooses to store."""
        record = await harness.tasks.defer(status_message="Waiting", arguments={"kind": "demo"})
        return record.task_id if record is not None else "sync"

    async with tasks_client(harness) as client:
        handle = await client.session.call_tool("deferred", {"secret": "card 4111111111111111"}, allow_claimed=True)
        assert isinstance(handle, TaskHandle)
    (record,) = harness.records()
    assert record.arguments == {"kind": "demo"}


async def test_round2_misc_low_non_accept_answers_drop_content(harness: Harness) -> None:
    """SEC-7 residual: MCP only puts `content` on accept. A decline (or cancel) that carries a payload is
    not checked against the form, so it must not be stored either: the record keeps `{"action": ...}`."""
    async with tasks_client(harness) as client:
        handle = await open_handle(client)
        await harness.ask(handle.task_id, "Firm date?")
        sneaky = types.ElicitResult(action="decline", content={"answer": "x" * 30_000, "extra": 1})
        await update_task(client.session, handle.task_id, {"owner_question_1": sneaky})
    stored = await harness.manager.get(handle.task_id)
    assert stored.status == "working"
    assert stored.input_responses == {"owner_question_1": {"action": "decline"}}


# --------------------------------------------------------------------------- round 2: present hook (mrtr guard)


def _redact(record: Any) -> Any:
    """A `present` hook: the view a client may see (replaces the question text, keeps the form)."""
    import dataclasses

    requests = {
        key: {**request, "params": {**request["params"], "message": "A question about your request."}}
        for key, request in (record.input_requests or {}).items()
    }
    return dataclasses.replace(
        record, status_message=f"[shown] {record.status_message}", input_requests=requests or record.input_requests
    )


async def test_round2_mrtr_guard_present_shapes_every_task_object_a_client_is_sent() -> None:
    """The handle, tasks/get and tasks/list rows go through `present`; what is stored, and the answer
    validation of tasks/update, do not."""
    seen: list[str] = []

    async def present(record: Any) -> Any:
        seen.append(record.status)
        return _redact(record)

    harness = Harness(present=present)
    async with tasks_client(harness) as client:
        handle = await open_handle(client)
        assert handle.status_message == "[shown] Waiting for the owner: cake"
        await harness.ask(handle.task_id, "What is the secret code?")
        state = await get_task(client.session, handle.task_id)
        assert state.status_message == "[shown] What is the secret code?"
        assert state.input_requests is not None
        shown = state.input_requests["owner_question_1"].model_dump(by_alias=True, mode="json", exclude_none=True)
        assert shown["params"]["message"] == "A question about your request."
        assert shown["params"]["requestedSchema"]["required"] == ["answer"]
        listed = await list_tasks(client.session)
        assert [t.status_message for t in listed.tasks] == ["[shown] What is the secret code?"]
        stored = await harness.manager.get(handle.task_id)
        assert stored.status_message == "What is the secret code?"  # never stored
        assert stored.input_requests is not None
        assert stored.input_requests["owner_question_1"]["params"]["message"] == "What is the secret code?"
        answer = types.ElicitResult(action="accept", content={"answer": "42"})
        await update_task(client.session, handle.task_id, {"owner_question_1": answer})
        assert (await harness.manager.get(handle.task_id)).input_responses == {
            "owner_question_1": {"action": "accept", "content": {"answer": "42"}}
        }
    assert seen == ["working", "input_required", "input_required"]


async def test_round2_mrtr_guard_without_present_the_wire_is_the_stored_record() -> None:
    """No hook (or an identity hook): every task object is exactly `record.wire(...)`."""
    bodies: list[dict[str, Any]] = []
    for hook in (None, "identity"):

        async def identity(record: Any) -> Any:
            return record

        harness = Harness(present=identity if hook else None)
        async with tasks_client(harness) as client:
            handle = await open_handle(client)
            await harness.ask(handle.task_id, "Which flavour?")
            state = await get_task(client.session, handle.task_id)
            stored = await harness.manager.get(handle.task_id)
        dumped = state.model_dump(by_alias=True, mode="json", exclude_none=True)
        body = {k: v for k, v in dumped.items() if k != "_meta"}
        assert body == {k: v for k, v in stored.wire("complete").items() if v is not None}
        assert handle.status_message == "Waiting for the owner: cake"
        bodies.append({k: v for k, v in body.items() if k not in ("taskId", "createdAt", "lastUpdatedAt")})
    assert bodies[0] == bodies[1]
