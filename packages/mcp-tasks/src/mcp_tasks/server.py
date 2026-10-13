"""Server half of the SEP-2663 tasks extension for `mcp` 2.2 servers.

mcp 2.2.0 ships the 2025-11-25 task models as types only and never dispatches `tasks/*`. On protocol
2026-07-28 tasks continue as the `io.modelcontextprotocol/tasks` extension (SEP-2663). This module
implements the server side on the SDK's extension seams:

  * `Extension.intercept_tool_call` turns a `tools/call` into a task handle (`resultType: "task"`);
  * `MethodBinding` serves `tasks/get`, `tasks/update`, `tasks/cancel` (and `tasks/list`);
  * the runner leaves non-core `resultType` values unsieved on 2026-07-28.

A tool decides per call whether to answer with a task. A handle is only ever returned to a
2026-07-28 client that advertised the extension; every other caller gets the tool's ordinary
synchronous result (graceful degradation).

Typical tool body::

    record = await tasks.ensure_task(status_message="Waiting for the owner", task_id=derived_id)
    tasks.adopt(record)        # False -> caller cannot take tasks; answer synchronously
    return SyncFallbackResult(...)  # replaced by the task handle when adopted
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, cast

import anyio
import mcp_types as types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.server.request_state import authenticated_principal
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS
from pydantic import TypeAdapter, ValidationError

from mcp_tasks.manager import InvalidInput, InvalidTransition, TaskManager, TaskNotFound
from mcp_tasks.models import TASKS_EXTENSION_ID, TASKS_PROTOCOL_VERSIONS, TaskRecord

logger = logging.getLogger("mcp_tasks")

ANONYMOUS_PRINCIPAL = "anonymous"

CancelHook = Callable[[TaskRecord], Awaitable[None]]
"""Called with the cancelled record after a client's `tasks/cancel` succeeds."""

InputHook = Callable[[TaskRecord], Awaitable[None]]
"""Called with the updated record after a client's `tasks/update` was accepted."""

PresentHook = Callable[[TaskRecord], Awaitable[TaskRecord]]
"""Returns the record as a client may see it, just before it is sent (the `tools/call` handle,
`tasks/get`, `tasks/list` rows): e.g. text the application no longer wants shown, replaced. The
returned view is only rendered, never stored."""


# --------------------------------------------------------------------------- wire params


class GetTaskParams(types.RequestParams):
    """`tasks/get {taskId}`."""

    task_id: str


class UpdateTaskParams(types.RequestParams):
    """`tasks/update {taskId, inputResponses}`."""

    task_id: str
    input_responses: dict[str, Any]


class CancelTaskParams(types.RequestParams):
    """`tasks/cancel {taskId}`."""

    task_id: str


_INPUT_RESPONSES: TypeAdapter[types.InputResponses] = TypeAdapter(types.InputResponses)


# --------------------------------------------------------------------------- request helpers


def _request_context(ctx: Any) -> ServerRequestContext[Any, Any]:
    """Accept either a low-level `ServerRequestContext` or an MCPServer tool `Context`."""
    if isinstance(ctx, ServerRequestContext):
        return cast(ServerRequestContext[Any, Any], ctx)
    inner = getattr(ctx, "request_context", None)
    if isinstance(inner, ServerRequestContext):
        return cast(ServerRequestContext[Any, Any], inner)
    raise TypeError(f"expected a ServerRequestContext or MCPServer Context, got {type(ctx).__name__}")


def principal_of(ctx: Any) -> str:
    """Who owns a task: the SDK's authenticated (client, issuer, subject) binding, else 'anonymous'.

    Without auth every caller shares the anonymous principal, so the unguessable task id is the only
    capability guarding a task. Enable auth in production to bind tasks to their caller.
    """
    return authenticated_principal(_request_context(ctx)) or ANONYMOUS_PRINCIPAL


def client_opted_in(ctx: Any) -> bool:
    """True if this request is 2026-07-28 and its client advertised the tasks extension."""
    rctx = _request_context(ctx)
    caps = rctx.session.client_capabilities
    return (
        rctx.protocol_version in TASKS_PROTOCOL_VERSIONS
        and caps is not None
        and caps.extensions is not None
        and TASKS_EXTENSION_ID in caps.extensions
    )


@dataclass
class CallInfo:
    """The `tools/call` currently running inside the interceptor."""

    tool_name: str
    arguments: dict[str, Any]
    principal: str
    opted_in: bool


@dataclass
class _CallSlot:
    info: CallInfo
    record: TaskRecord | None = None  # set once the call is adopted as a task


_CURRENT: contextvars.ContextVar[_CallSlot | None] = contextvars.ContextVar("mcp_tasks_current_call", default=None)


# --------------------------------------------------------------------------- the extension


class TasksExtension(Extension):
    """Server half of the tasks extension. Pass the instance to `MCPServer(extensions=[...])`.

    Error mapping on `tasks/*`: an unknown, expired or foreign task id, an invalid transition, a
    malformed or schema-violating `inputResponses` map and a malformed `tasks/list` cursor all answer
    `-32602` (INVALID_PARAMS). On a pre-2026 wire every `tasks/*` method answers `-32601`
    (METHOD_NOT_FOUND).

    `tasks/list` requires an authenticated principal. Without auth every caller shares the
    'anonymous' principal, so listing would hand every caller every other caller's task ids (the
    only capability guarding a task). It therefore refuses anonymous callers with `-32602` unless
    `list_anonymous=True` (single-user development only).
    """

    identifier = TASKS_EXTENSION_ID

    def __init__(
        self,
        manager: TaskManager,
        *,
        serve_list: bool = True,
        list_page_size: int = 50,
        list_anonymous: bool = False,
        on_cancel: CancelHook | None = None,
        on_input: InputHook | None = None,
        present: PresentHook | None = None,
    ) -> None:
        """`serve_list` toggles the `tasks/list` convenience; `list_page_size` is its page size;
        `list_anonymous` lets unauthenticated callers list (development only); `on_cancel` lets the
        application react to a client's `tasks/cancel` (e.g. release what the task was holding);
        `on_input` lets it react to answers accepted through `tasks/update` (e.g. store them with the
        work the task tracks). Hook failures are logged and never undo the cancellation or the answers.

        `present` maps every record to the view a client is sent (the `tools/call` handle, `tasks/get`,
        `tasks/list` rows), e.g. an output check against rules that changed after the record was
        written. It never changes what is stored (`tasks/update` still validates answers against the
        stored input requests). It runs on the request path: an exception it raises fails that request."""
        if list_page_size < 1:
            raise ValueError("list_page_size must be at least 1")
        self.manager = manager
        self._serve_list = serve_list
        self._list_page_size = list_page_size
        self._list_anonymous = list_anonymous
        self._on_cancel = on_cancel
        self._on_input = on_input
        self._present = present

    # ---- contributions ------------------------------------------------------------------

    def methods(self) -> Sequence[MethodBinding]:
        versions = TASKS_PROTOCOL_VERSIONS
        bindings = [
            MethodBinding("tasks/get", GetTaskParams, self._handle_get, protocol_versions=versions),
            MethodBinding("tasks/update", UpdateTaskParams, self._handle_update, protocol_versions=versions),
            MethodBinding("tasks/cancel", CancelTaskParams, self._handle_cancel, protocol_versions=versions),
        ]
        if self._serve_list:  # not part of the strands SEP-2663 client; a convenience
            bindings.append(
                MethodBinding("tasks/list", types.PaginatedRequestParams, self._handle_list, protocol_versions=versions)
            )
        return bindings

    async def intercept_tool_call(
        self, params: types.CallToolRequestParams, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        """Run the tool with its `CallInfo` in context; swap its result for the adopted task's handle.

        A tool that adopted a task and then raised (an `MCPError`, which MCPServer re-raises, or a
        cancellation) or returned an `is_error` result leaves its task `failed`, never `working`.
        """
        slot = _CallSlot(
            info=CallInfo(
                tool_name=params.name,
                arguments=dict(params.arguments or {}),
                principal=principal_of(ctx),
                opted_in=client_opted_in(ctx),
            )
        )
        token = _CURRENT.set(slot)
        try:
            result = await call_next(ctx)
        except BaseException as exc:
            if slot.record is not None and not slot.record.terminal:
                # MCPError (and cancellation) propagate instead of becoming an is_error result, so fail
                # the adopted task here or it would stay `working` until its TTL.
                message = exc.error.message if isinstance(exc, MCPError) else "Tool call aborted"
                with anyio.CancelScope(shield=True):
                    try:
                        await self.manager.fail(slot.record.task_id, message)
                    except Exception:  # noqa: BLE001 - best effort; the original error wins
                        pass
            raise
        finally:
            _CURRENT.reset(token)
        if slot.record is None:
            return result  # the tool answered synchronously: an ordinary CallToolResult
        if isinstance(result, types.CallToolResult) and result.is_error:
            # The tool adopted a task and then failed: record the failure and surface it directly.
            text = " ".join(c.text for c in result.content if isinstance(c, types.TextContent))
            try:
                await self.manager.fail(slot.record.task_id, text or "Tool failed")
            except (InvalidTransition, TaskNotFound):
                pass
            return result
        try:
            current = await self.manager.get(slot.record.task_id)
        except TaskNotFound:
            # The adopted record vanished or had already expired (e.g. an idempotent retry long after
            # the first call): a handle would be unpollable, so answer with the tool's own result.
            return result
        return (await self._view(current)).wire("task")

    # ---- tool-side API --------------------------------------------------------------------

    def current_call(self) -> CallInfo | None:
        """The tools/call being handled in this context, or None outside the interceptor."""
        slot = _CURRENT.get()
        return slot.info if slot is not None else None

    async def ensure_task(
        self,
        *,
        status_message: str,
        task_id: str | None = None,
        ttl_ms: int | None = None,
        poll_interval_ms: int | None = None,
        labels: dict[str, str] | None = None,
        arguments: dict[str, Any] | None = None,
        refresh_ttl: bool = False,
    ) -> TaskRecord:
        """Create (or, for a reused `task_id`, fetch) the task for the current tool call.

        Works for every caller, including ones that cannot receive a task handle, so a synchronous
        fallback can still hand out the task id (e.g. for a `get_task` polling tool). Must be called
        inside a tool call routed through this extension.

        `arguments` is what the record stores as the call's arguments; it defaults to the raw
        `tools/call` arguments. Pass a minimal, validated dict when those may hold personal data or
        unvalidated keys (the record lives for the task's whole TTL).

        `refresh_ttl=True` extends a reused active task so it lives at least `ttl_ms` from now (see
        `TaskManager.create`): use it when this call starts the work the task tracks, so a retry long
        after an earlier attempt does not inherit that attempt's deadline.
        """
        slot = _CURRENT.get()
        if slot is None:
            raise RuntimeError("ensure_task() must be called from a tool running under TasksExtension")
        return await self.manager.create(
            principal=slot.info.principal,
            tool_name=slot.info.tool_name,
            arguments=dict(arguments) if arguments is not None else slot.info.arguments,
            status_message=status_message,
            ttl_ms=ttl_ms,
            poll_interval_ms=poll_interval_ms,
            task_id=task_id,
            labels=labels,
            refresh_ttl=refresh_ttl,
        )

    def adopt(self, record: TaskRecord) -> bool:
        """Answer the current tools/call with `record`'s task handle instead of the tool's result.

        Returns False (and changes nothing) when the caller cannot take a task: no tasks extension
        in its capabilities, or a pre-2026 wire, or no tool call is running in this context. The
        tool's own return value is then sent as is.
        """
        slot = _CURRENT.get()
        if slot is None or not slot.info.opted_in:
            return False
        slot.record = record
        return True

    async def defer(
        self,
        *,
        status_message: str,
        ttl_ms: int | None = None,
        poll_interval_ms: int | None = None,
        task_id: str | None = None,
        labels: dict[str, str] | None = None,
        arguments: dict[str, Any] | None = None,
    ) -> TaskRecord | None:
        """`ensure_task` + `adopt` for callers that can take a task; None (and no task) otherwise.

        `arguments` is passed through to `ensure_task` (what the record stores of the call; the raw
        `tools/call` arguments when None)."""
        slot = _CURRENT.get()
        if slot is None or not slot.info.opted_in:
            return None
        if slot.record is None:
            record = await self.ensure_task(
                status_message=status_message,
                task_id=task_id,
                ttl_ms=ttl_ms,
                poll_interval_ms=poll_interval_ms,
                labels=labels,
                arguments=arguments,
            )
            self.adopt(record)
        return slot.record

    # ---- method handlers --------------------------------------------------------------------

    async def _view(self, record: TaskRecord) -> TaskRecord:
        """`record` as a client may see it (the `present` hook; the record itself without one)."""
        return await self._present(record) if self._present is not None else record

    async def _load(self, ctx: ServerRequestContext[Any, Any], task_id: str) -> TaskRecord:
        try:
            return await self.manager.get(task_id, principal=principal_of(ctx))
        except TaskNotFound:
            raise MCPError(code=INVALID_PARAMS, message="Unknown or expired taskId", data={"taskId": task_id}) from None

    async def _handle_get(self, ctx: ServerRequestContext[Any, Any], params: GetTaskParams) -> HandlerResult:
        return (await self._view(await self._load(ctx, params.task_id))).wire("complete")

    async def _handle_update(self, ctx: ServerRequestContext[Any, Any], params: UpdateTaskParams) -> HandlerResult:
        await self._load(ctx, params.task_id)
        try:
            responses = _INPUT_RESPONSES.validate_python(params.input_responses)
        except ValidationError:
            raise MCPError(code=INVALID_PARAMS, message="Malformed inputResponses") from None
        dumped = {k: v.model_dump(by_alias=True, mode="json", exclude_none=True) for k, v in responses.items()}
        try:
            updated = await self.manager.submit_input(params.task_id, principal_of(ctx), dumped)
        except (InvalidTransition, InvalidInput) as e:
            raise MCPError(code=INVALID_PARAMS, message=str(e)) from None
        if self._on_input is not None:
            with anyio.CancelScope(shield=True):
                try:
                    await self._on_input(updated)
                except Exception:  # noqa: BLE001 - the answers themselves are already stored
                    logger.exception("tasks/update hook failed", extra={"task_tool": updated.tool_name})
        return {"resultType": "complete"}  # empty acknowledgement

    async def _handle_cancel(self, ctx: ServerRequestContext[Any, Any], params: CancelTaskParams) -> HandlerResult:
        await self._load(ctx, params.task_id)
        try:
            cancelled = await self.manager.cancel(params.task_id, "Cancelled by the client")
        except InvalidTransition as e:
            raise MCPError(code=INVALID_PARAMS, message=str(e)) from None
        if self._on_cancel is not None:
            with anyio.CancelScope(shield=True):
                try:
                    await self._on_cancel(cancelled)
                except Exception:  # noqa: BLE001 - the cancellation itself already happened
                    logger.exception("tasks/cancel hook failed", extra={"task_tool": cancelled.tool_name})
        return {"resultType": "complete"}  # empty acknowledgement

    async def _handle_list(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams
    ) -> HandlerResult:
        principal = principal_of(ctx)
        if principal == ANONYMOUS_PRINCIPAL and not self._list_anonymous:
            raise MCPError(code=INVALID_PARAMS, message="tasks/list requires an authenticated caller")
        try:
            rows, nxt = await self.manager.list_for(principal, cursor=params.cursor, limit=self._list_page_size)
        except ValueError:
            raise MCPError(code=INVALID_PARAMS, message="Invalid cursor") from None
        body: dict[str, Any] = {"resultType": "complete", "tasks": [(await self._view(r)).summary_wire() for r in rows]}
        if nxt:
            body["nextCursor"] = nxt
        return body
