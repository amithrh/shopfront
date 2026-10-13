"""Shared test helpers for mcp-tasks: a fake clock, a demo approval server and SEP-2663 wire checks.

Everything here is generic (no product concepts): the "owner" is whoever completes a task out of band.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import mcp_types as types
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS
from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_tasks import (
    DuplicateTask,
    InMemoryTaskStore,
    InvalidTransition,
    TaskManager,
    TaskNotFound,
    TaskRecord,
    TasksExtension,
    TaskStore,
)

EPOCH = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)


class ManualClock:
    """Deterministic clock: returns `current`, optionally advancing by `tick` after every read."""

    def __init__(self, start: datetime = EPOCH, *, tick: timedelta = timedelta(0)) -> None:
        self.current = start
        self.tick = tick

    def __call__(self) -> datetime:
        now = self.current
        self.current += self.tick
        return now

    def advance(self, **delta: float) -> None:
        """Move the clock forward (`advance(hours=1)`)."""
        self.current += timedelta(**delta)


class ApprovalOutcome(BaseModel):
    """One output schema for every path (sync fallback and task completion), as gotcha 2 requires."""

    status: Literal["approved", "declined", "pending"]
    note: str
    reference: str


def outcome_result(outcome: ApprovalOutcome) -> types.CallToolResult:
    """A `CallToolResult` serialised exactly like MCPServer serialises an `ApprovalOutcome` return."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=outcome.model_dump_json())],
        structured_content=outcome.model_dump(mode="json"),
    )


def form_question(question: str) -> types.ElicitRequest:
    """An embedded form elicitation asking for one free-text `answer`."""
    return types.ElicitRequest(
        params=types.ElicitRequestFormParams(
            message=question,
            requested_schema={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        )
    )


class Harness:
    """An MCPServer with the tasks extension, a few task-shaped tools and an owner side.

    Tools:
      * `request_owner_approval(summary)`: reports progress, then `defer()`s (sync fallback otherwise).
      * `idempotent_approval(key)`: `ensure_task(task_id="task-<key>")` + `adopt()`.
      * `boom(kind)`: defers, then fails with a ValueError, a ToolError or an MCPError.
      * `whoami(tag)`: returns the `CallInfo` the interceptor recorded.
      * `claim_shared_a` / `claim_shared_b`: two tools racing for the same caller-chosen task id.
    Owner side: `approve`, `ask`, plus the HTTP webhook `POST /owner/tasks/{task_id}/decision`.
    """

    def __init__(
        self,
        *,
        clock: ManualClock | None = None,
        store: TaskStore | None = None,
        poll_interval_ms: int = 20,
        ttl_ms: int | None = 24 * 3600 * 1000,
        list_page_size: int = 50,
        list_anonymous: bool = True,
        on_cancel: Any = None,
        on_input: Any = None,
        present: Any = None,
    ) -> None:
        """`list_anonymous` defaults to True here: the harness is a single-user test server."""
        self.store: TaskStore = store if store is not None else InMemoryTaskStore()
        kwargs: dict[str, Any] = {"default_poll_interval_ms": poll_interval_ms, "default_ttl_ms": ttl_ms}
        if clock is not None:
            kwargs["now"] = clock
        self.manager = TaskManager(self.store, **kwargs)
        self.tasks = TasksExtension(
            self.manager,
            list_page_size=list_page_size,
            list_anonymous=list_anonymous,
            on_cancel=on_cancel,
            on_input=on_input,
            present=present,
        )
        self.outbox: list[str] = []
        self.server = MCPServer("mcp-tasks-test", extensions=[self.tasks])
        self._register_tools()
        self._register_owner_routes()

    def records(self) -> list[TaskRecord]:
        """Every stored record (requires the default in-memory store)."""
        assert isinstance(self.store, InMemoryTaskStore)
        return self.store.all()

    # -- owner side --------------------------------------------------------------------------

    async def approve(self, task_id: str | None = None, note: str = "ok") -> TaskRecord:
        """Complete a task (default: the latest deferred one) with an `approved` outcome."""
        tid = task_id or self.outbox[-1]
        outcome = ApprovalOutcome(status="approved", note=note, reference=tid)
        return await self.manager.complete(tid, outcome_result(outcome), message="Owner approved")

    async def ask(self, task_id: str, question: str, *, key: str = "owner_question_1") -> TaskRecord:
        """Move a task to `input_required` with one embedded form question."""
        return await self.manager.require_input(task_id, {key: form_question(question)}, message=question)

    # -- registration --------------------------------------------------------------------------

    def _register_tools(self) -> None:
        tasks, outbox, server = self.tasks, self.outbox, self.server

        @server.tool()
        async def request_owner_approval(summary: str, ctx: Context) -> ApprovalOutcome:
            """Ask the owner to approve a request."""
            await ctx.report_progress(0.0, 1.0, "Contacting the owner")
            task = await tasks.defer(status_message=f"Waiting for the owner: {summary}")
            if task is None:
                return ApprovalOutcome(status="pending", note="sync fallback", reference="n/a")
            outbox.append(task.task_id)
            return ApprovalOutcome(status="pending", note="deferred", reference=task.task_id)

        @server.tool()
        async def idempotent_approval(key: str) -> ApprovalOutcome:
            """Create-or-reuse a task with a caller-chosen id, then adopt it if the client can."""
            record = await tasks.ensure_task(status_message="Waiting", task_id=f"task-{key}")
            adopted = tasks.adopt(record)
            outbox.append(record.task_id)
            return ApprovalOutcome(status="pending", note="adopted" if adopted else "sync", reference=record.task_id)

        @server.tool()
        async def boom(kind: Literal["value", "tool", "mcp"]) -> ApprovalOutcome:
            """Defer, then fail in one of three ways."""
            task = await tasks.defer(status_message="About to fail")
            assert task is not None
            outbox.append(task.task_id)
            if kind == "value":
                raise ValueError("secret internal detail")
            if kind == "tool":
                raise ToolError("SMS gateway down")
            raise MCPError(code=INVALID_PARAMS, message="bad business id")

        @server.tool()
        async def whoami(tag: str) -> dict[str, Any]:
            """Report what the interceptor recorded for this call."""
            info = tasks.current_call()
            assert info is not None
            return {
                "tool_name": info.tool_name,
                "arguments": info.arguments,
                "principal": info.principal,
                "opted_in": info.opted_in,
            }

        async def claim_shared(tool: str) -> str:
            try:
                record = await tasks.ensure_task(status_message="Shared", task_id="shared-id")
            except DuplicateTask:
                return f"{tool}: duplicate"
            return f"{tool}: owns {record.task_id}"

        @server.tool()
        async def claim_shared_a() -> str:
            """Claim the task id `shared-id`."""
            return await claim_shared("a")

        @server.tool()
        async def claim_shared_b() -> str:
            """Claim the task id `shared-id` from a different tool."""
            return await claim_shared("b")

    def _register_owner_routes(self) -> None:
        @self.server.custom_route("/owner/tasks/{task_id}/decision", methods=["POST"])
        async def owner_decision(request: Request) -> JSONResponse:
            """Test-only owner webhook (unsigned; a real one must verify an HMAC-signed link)."""
            task_id = request.path_params["task_id"]
            body = await request.json()
            try:
                if body.get("action") == "ask":
                    record = await self.ask(task_id, body["question"])
                else:
                    record = await self.approve(task_id, body.get("note", "ok"))
            except TaskNotFound:
                return JSONResponse({"error": "unknown task"}, status_code=404)
            except InvalidTransition as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
            return JSONResponse({"taskId": task_id, "status": record.status})


class StaleReadStore:
    """Wraps a store and can serve one historic version of a record on the next `get` (a lagging replica)."""

    def __init__(self, inner: InMemoryTaskStore) -> None:
        self.inner = inner
        self.history: dict[str, list[TaskRecord]] = {}
        self._stale: dict[str, TaskRecord] = {}

    def serve_stale(self, task_id: str, version: int) -> None:
        """Make the next `get(task_id)` return the record as it was at `version`."""
        self._stale[task_id] = self.history[task_id][version]

    async def put_new(self, record: TaskRecord) -> None:
        await self.inner.put_new(record)
        self.history[record.task_id] = [record]

    async def get(self, task_id: str) -> TaskRecord | None:
        stale = self._stale.pop(task_id, None)
        return stale if stale is not None else await self.inner.get(task_id)

    async def replace(self, record: TaskRecord, *, expected_version: int) -> bool:
        ok = await self.inner.replace(record, expected_version=expected_version)
        if ok:
            self.history[record.task_id].append(record)
        return ok

    async def list_for(self, principal: str, *, cursor: str | None, limit: int) -> tuple[list[TaskRecord], str | None]:
        return await self.inner.list_for(principal, cursor=cursor, limit=limit)


# --------------------------------------------------------------------------- SEP-2663 wire checks

_TASK_FIELDS = {"taskId", "status", "statusMessage", "createdAt", "lastUpdatedAt", "ttlMs", "pollIntervalMs"}
_PAYLOAD_FOR_STATUS = {"input_required": "inputRequests", "completed": "result", "failed": "error"}
_PAYLOADS = set(_PAYLOAD_FOR_STATUS.values())


def _utc_instant(value: Any) -> datetime:
    assert isinstance(value, str) and value.endswith("Z"), f"timestamp must be UTC with a Z suffix: {value!r}"
    parsed = datetime.fromisoformat(value)
    assert parsed.utcoffset() == timedelta(0)
    return parsed


def check_task_fields(body: dict[str, Any]) -> None:
    """The field rules strands-agents 1.57.1 enforces on every SEP-2663 task object."""
    assert isinstance(body["taskId"], str) and body["taskId"], "taskId: non-empty string"
    assert body["status"] in {"working", "input_required", "completed", "failed", "cancelled"}
    assert "ttlMs" in body, "ttlMs is required (nullable)"
    assert body["ttlMs"] is None or (isinstance(body["ttlMs"], int) and body["ttlMs"] >= 0)
    if "pollIntervalMs" in body:
        assert isinstance(body["pollIntervalMs"], int) and body["pollIntervalMs"] >= 0
    if "statusMessage" in body:
        assert isinstance(body["statusMessage"], str)
    assert _utc_instant(body["lastUpdatedAt"]) >= _utc_instant(body["createdAt"]), "lastUpdatedAt >= createdAt"


def check_handle_body(body: dict[str, Any]) -> None:
    """A `tools/call` task handle: task fields, `resultType: "task"`, no status payload."""
    assert body["resultType"] == "task"
    check_task_fields(body)
    assert not (_PAYLOADS & body.keys()), f"a handle carries no payload: {sorted(_PAYLOADS & body.keys())}"
    assert set(body) - {"_meta"} <= _TASK_FIELDS | {"resultType"}


def check_get_body(body: dict[str, Any]) -> None:
    """A `tasks/get` body: task fields, `resultType: "complete"`, exactly the payload its status needs."""
    assert body["resultType"] == "complete"
    check_task_fields(body)
    expected = _PAYLOAD_FOR_STATUS.get(body["status"])
    present = _PAYLOADS & body.keys()
    assert present == ({expected} if expected else set()), f"{body['status']}: payloads {sorted(present)}"
    if expected is not None:
        assert body[expected] is not None, f"{expected} must not be null"
    if body["status"] == "failed":
        assert isinstance(body["error"]["code"], int) and isinstance(body["error"]["message"], str)
    if body["status"] == "input_required":
        assert isinstance(body["inputRequests"], dict) and body["inputRequests"]
    assert set(body) - {"_meta"} <= _TASK_FIELDS | {"resultType"} | _PAYLOADS
