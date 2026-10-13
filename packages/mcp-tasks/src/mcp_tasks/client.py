"""Client half of the SEP-2663 tasks extension for `mcp.client.Client` / `ClientSession` (mcp 2.2).

* `TasksClientExtension`: pass to `Client(extensions=[...])`. It advertises
  `io.modelcontextprotocol/tasks` and claims `resultType: "task"` on `tools/call`, so
  `client.call_tool(...)` polls `tasks/get` transparently and returns the final `CallToolResult`,
  answering `input_required` rounds through the client's elicitation/sampling callbacks.
* `get_task` / `update_task` / `cancel_task` / `list_tasks`: manual control through
  `client.session` (use with `client.session.call_tool(..., allow_claimed=True)`).

Server errors on `tasks/*` surface as `mcp.shared.exceptions.MCPError`: `-32602` for an unknown,
expired or foreign task or an invalid transition, `-32601` on a pre-2026 connection. An input request
the client has no answer for (no callback, or the callback returned an error) raises
`TaskInputUnanswerable`, which carries the task id.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

import anyio
import mcp_types as types
from mcp.client.extension import ClaimContext, ClientExtension, ResultClaim
from mcp.client.session import ClientRequestContext, ClientSession
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, Result
from pydantic import BaseModel, ConfigDict

from mcp_tasks.models import TASKS_EXTENSION_ID, TASKS_PROTOCOL_VERSIONS

# --------------------------------------------------------------------------- wire models


class _TaskFields(Result):
    task_id: str
    status: Literal["working", "input_required", "completed", "failed", "cancelled"]
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None  # required, nullable
    poll_interval_ms: int | None = None


class TaskHandle(_TaskFields):
    """`tools/call` answered with a task instead of a `CallToolResult`."""

    result_type: Literal["task"]


class TaskError(BaseModel):
    """`error` payload of a failed task (JSON-RPC error shape)."""

    model_config = ConfigDict(extra="allow")

    code: int
    message: str
    data: Any = None


class TaskState(_TaskFields):
    """`tasks/get` response: status plus the status-specific payload."""

    result_type: Literal["complete"] = "complete"
    input_requests: dict[str, types.InputRequest] | None = None
    result: types.CallToolResult | None = None
    error: TaskError | None = None


class TaskSummary(_TaskFields):
    """One row of `tasks/list` (no status payload)."""


class ListTasksResult(types.PaginatedResult):
    tasks: list[TaskSummary]


class _TaskIdParams(types.RequestParams):
    task_id: str


class _UpdateParams(types.RequestParams):
    task_id: str
    input_responses: dict[str, Any]


class GetTaskRequest(types.Request[_TaskIdParams, Literal["tasks/get"]]):
    method: Literal["tasks/get"] = "tasks/get"
    params: _TaskIdParams


class UpdateTaskRequest(types.Request[_UpdateParams, Literal["tasks/update"]]):
    method: Literal["tasks/update"] = "tasks/update"
    params: _UpdateParams


class CancelTaskRequest(types.Request[_TaskIdParams, Literal["tasks/cancel"]]):
    method: Literal["tasks/cancel"] = "tasks/cancel"
    params: _TaskIdParams


class ListTasksRequest(types.PaginatedRequest[Literal["tasks/list"]]):
    method: Literal["tasks/list"] = "tasks/list"


# SEP-2663: tasks/* mirror taskId into the Mcp-Name header (ClientSession.send_request reads name_param).
GetTaskRequest.name_param = "taskId"
UpdateTaskRequest.name_param = "taskId"
CancelTaskRequest.name_param = "taskId"


class TaskInputUnanswerable(RuntimeError):
    """The client cannot answer an input request of a task: no callback handles it, or the callback
    returned an error (e.g. a host that cannot render a free-text question).

    `task_id` is the task, `key` the request that could not be answered and `input_requests` every
    request that was pending in that round (wire models). With `TasksClientExtension`'s default
    (`cancel_on_unanswerable=True`) the task has been cancelled by the time this is raised; with
    `cancel_on_unanswerable=False` it is left `input_required`, so the application can answer it
    another way (a server-offered tool, a different device) or cancel it itself.
    """

    def __init__(
        self, task_id: str, key: str, reason: str, input_requests: dict[str, types.InputRequest] | None = None
    ) -> None:
        super().__init__(f"input request {key!r} declined: {reason}")
        self.task_id = task_id
        self.key = key
        self.reason = reason
        self.input_requests: dict[str, types.InputRequest] = dict(input_requests or {})


# --------------------------------------------------------------------------- manual API


async def get_task(session: ClientSession, task_id: str) -> TaskState:
    """`tasks/get`."""
    return await session.send_request(GetTaskRequest(params=_TaskIdParams(task_id=task_id)), TaskState)


async def update_task(session: ClientSession, task_id: str, responses: dict[str, types.InputResponse]) -> None:
    """`tasks/update` with answers keyed like the task's `inputRequests`."""
    dumped = {k: v.model_dump(by_alias=True, mode="json", exclude_none=True) for k, v in responses.items()}
    await session.send_request(
        UpdateTaskRequest(params=_UpdateParams(task_id=task_id, input_responses=dumped)), types.EmptyResult
    )


async def cancel_task(session: ClientSession, task_id: str) -> None:
    """`tasks/cancel`."""
    await session.send_request(CancelTaskRequest(params=_TaskIdParams(task_id=task_id)), types.EmptyResult)


async def list_tasks(session: ClientSession, cursor: str | None = None) -> ListTasksResult:
    """`tasks/list` (a convenience method served by `mcp_tasks.server`; not part of strands' client)."""
    return await session.send_request(
        ListTasksRequest(params=types.PaginatedRequestParams(cursor=cursor)), ListTasksResult
    )


async def answer_input_requests(session: ClientSession, state: TaskState) -> dict[str, types.InputResponse]:
    """Route each embedded input request through the session's own callbacks (elicitation/sampling/roots).

    Raises `TaskInputUnanswerable` when a callback declines with an error (or none is installed)."""
    responses: dict[str, types.InputResponse] = {}
    for key, req in (state.input_requests or {}).items():
        ctx = ClientRequestContext(session=session, request_id=key, meta=req.params.meta if req.params else None)
        answer = await session.dispatch_input_request(ctx, req)
        if isinstance(answer, types.ErrorData):
            raise TaskInputUnanswerable(state.task_id, key, answer.message, state.input_requests)
        responses[key] = answer
    return responses


# --------------------------------------------------------------------------- transparent resolver

StatusCallback = Callable[[TaskHandle | TaskState], Awaitable[None]]

_MIN_POLL_INTERVAL_S = 0.01
"""Floor for a server-supplied `pollIntervalMs` (a `0` hint must not turn into a busy loop)."""


def _instant(timestamp: str) -> datetime | None:
    """Parse an RFC 3339 timestamp (naive means UTC); None if it cannot be parsed."""
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _cancel_quietly(session: ClientSession, task_id: str) -> None:
    """Best-effort, shielded `tasks/cancel` (cleanup must not mask the original error)."""
    with anyio.CancelScope(shield=True), anyio.move_on_after(5):
        try:
            await cancel_task(session, task_id)
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass


async def _still_waiting(session: ClientSession, handle: TaskHandle, pending: dict[str, Any]) -> bool:
    """True if the task still waits for the very questions whose answers were just rejected."""
    try:
        state = await get_task(session, handle.task_id)
    except MCPError:
        return False  # gone (e.g. expired): let the next poll report it
    return state.status == "input_required" and bool(set(pending) & set(state.input_requests or {}))


class TasksClientExtension(ClientExtension):
    """Advertises the tasks extension and resolves task handles by polling until terminal.

    * Polls `tasks/get` every `pollIntervalMs` (else `default_poll_interval_s`).
    * Answers `input_required` rounds through the client's elicitation/sampling/roots callbacks and
      `tasks/update`. A key answered once is not answered again while the task stays
      `input_required`; the set resets as soon as a poll shows any other status, so a server that
      re-asks with a reused key after returning to `working` is answered. Servers should still use a
      fresh key per question: a re-ask that lands between two polls (never observed as `working`) is
      indistinguishable from the old question.
    * Ignores stale reads: a polled state whose `lastUpdatedAt` is older than one already seen (a
      lagging replica) is skipped and polled again.
    * A `tasks/update` rejected with `-32602` because the task moved on meanwhile (e.g. the owner
      decided while the user was still answering) is not an error: polling continues and the
      task's final result is returned. If the task is still waiting for that same input, the
      server rejected the answer itself and the `MCPError` is raised.
    * When the caller abandons the call (cancellation) or cannot answer an input request (no
      callback, or the callback fails) the task is cancelled server-side (best effort, shielded),
      so it does not linger. A callback that declines with an error (or a missing callback) raises
      `TaskInputUnanswerable`; with `cancel_on_unanswerable=False` the task is then left waiting
      for its input instead, for the application to answer another way (`exc.task_id`). A callback
      that raises still cancels the task. Reaching `max_wait_s` raises `TimeoutError` but
      leaves the task running by default, because a server-side task may legitimately take hours
      (an owner's approval) and cancelling it would discard that work; pass
      `cancel_on_timeout=True` to cancel it instead. A failed or cancelled task resolves to
      `CallToolResult(is_error=True)`.
    """

    identifier = TASKS_EXTENSION_ID

    def __init__(
        self,
        *,
        on_status: StatusCallback | None = None,
        default_poll_interval_s: float = 1.0,
        max_wait_s: float = 15 * 60,
        report_every_poll: bool = False,
        cancel_on_timeout: bool = False,
        cancel_on_unanswerable: bool = True,
    ) -> None:
        """`on_status` sees the handle and then each polled state (only changes unless `report_every_poll`).

        `cancel_on_timeout` cancels the server task when `max_wait_s` passes (default: leave it running).
        `cancel_on_unanswerable=False` leaves a task whose input request this client cannot answer
        waiting (`TaskInputUnanswerable` is raised either way; default: cancel it).
        """
        self._on_status = on_status
        self._every_poll = report_every_poll
        self._default_poll = default_poll_interval_s
        self._max_wait = max_wait_s
        self._cancel_on_timeout = cancel_on_timeout
        self._cancel_on_unanswerable = cancel_on_unanswerable

    def claims(self) -> Sequence[ResultClaim[Any]]:
        return (
            ResultClaim(
                result_type="task",
                model=TaskHandle,
                resolve=self._resolve,
                protocol_versions=TASKS_PROTOCOL_VERSIONS,
            ),
        )

    def _interval(self, current: TaskHandle | TaskState) -> float:
        if current.poll_interval_ms is None:
            return self._default_poll
        return max(_MIN_POLL_INTERVAL_S, current.poll_interval_ms / 1000)

    async def _resolve(self, handle: TaskHandle, cctx: ClaimContext) -> types.CallToolResult:
        session = cctx.session
        if self._on_status:
            await self._on_status(handle)
        current: TaskHandle | TaskState = handle
        answered: set[str] = set()
        last_status = (handle.status, handle.status_message)
        newest = _instant(handle.last_updated_at)
        try:
            with anyio.fail_after(self._max_wait):
                while True:
                    await anyio.sleep(self._interval(current))
                    state = await get_task(session, handle.task_id)
                    stamp = _instant(state.last_updated_at)
                    if newest is not None and stamp is not None and stamp < newest:
                        continue  # stale read: keep the newer state we already saw and poll again
                    newest = stamp or newest
                    if self._on_status and (self._every_poll or (state.status, state.status_message) != last_status):
                        await self._on_status(state)
                    last_status = (state.status, state.status_message)
                    current = state
                    if state.status != "input_required":
                        answered.clear()  # the round is over; a later re-ask may reuse a key
                    if state.status == "completed":
                        if state.result is None:
                            raise RuntimeError(f"task {handle.task_id} completed without a result")
                        return state.result
                    if state.status == "failed":
                        msg = state.error.message if state.error else (state.status_message or "Task failed")
                        return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], is_error=True)
                    if state.status == "cancelled":
                        msg = state.status_message or "Task was cancelled"
                        return types.CallToolResult(content=[types.TextContent(type="text", text=msg)], is_error=True)
                    if state.status == "input_required":
                        pending = {k: v for k, v in (state.input_requests or {}).items() if k not in answered}
                        if pending:
                            try:
                                responses = await answer_input_requests(
                                    session, state.model_copy(update={"input_requests": pending})
                                )
                            except TaskInputUnanswerable:
                                if self._cancel_on_unanswerable:
                                    await _cancel_quietly(session, handle.task_id)  # we cannot answer: give it up
                                raise
                            except Exception:
                                await _cancel_quietly(session, handle.task_id)  # the callback failed: give it up
                                raise
                            try:
                                await update_task(session, handle.task_id, responses)
                            except MCPError as exc:
                                if exc.error.code != INVALID_PARAMS or await _still_waiting(session, handle, pending):
                                    raise
                                continue  # lost a race with the task moving on: keep polling for its outcome
                            answered.update(responses)
        except TimeoutError:
            if self._cancel_on_timeout:
                await _cancel_quietly(session, handle.task_id)
            raise
        except anyio.get_cancelled_exc_class():
            await _cancel_quietly(session, handle.task_id)
            raise
