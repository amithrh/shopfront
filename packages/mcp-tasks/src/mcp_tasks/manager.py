"""Task state machine. Every state change goes through `TaskManager`, one guarded CAS per call.

Allowed transitions:

    working --set_message--> working|input_required (unchanged status)
    working|input_required --set_ttl--> (unchanged status, new ttlMs)
    working|input_required --create(same id, refresh_ttl=True)--> (unchanged status, ttlMs only extended)
    working --require_input--> input_required --tasks/update (all keys answered)--> working
    working|input_required --complete--> completed      (result = CallToolResult dump)
    working|input_required --fail--> failed             (error = {code, message, data})
    working|input_required --cancel--> cancelled        (client tasks/cancel, server expiry)
    terminal --anything--> InvalidTransition

Time comes from an injectable clock (`TaskManager(now=...)`), used for `createdAt`,
`lastUpdatedAt` and TTL expiry, so expiry is deterministic under test. `lastUpdatedAt` never moves
backwards, even if the clock does, because SEP-2663 clients treat an older `lastUpdatedAt` as a stale
read.
"""

from __future__ import annotations

import json
import math
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import mcp_types as types
from mcp_types import INTERNAL_ERROR

from mcp_tasks.models import ACTIVE, Clock, TaskRecord, utc_now
from mcp_tasks.store import DuplicateTask, TaskStore
from mcp_tasks.validation import InvalidContent, check_form_content, requested_schema

_CAS_ATTEMPTS = 8

MAX_INPUT_RESPONSES_BYTES = 64_000
"""Upper bound on one `tasks/update` payload (serialised JSON), whatever the forms allow."""


class TaskNotFound(Exception):
    """Unknown id, expired task, or a task owned by another principal (deliberately indistinguishable)."""


class InvalidTransition(Exception):
    """The requested change is not allowed from the task's current status."""


class InvalidInput(ValueError):
    """A `tasks/update` answer does not match the question it answers (schema, size, type)."""


class TaskManager:
    """Creates tasks and applies guarded transitions with compare-and-swap retries."""

    def __init__(
        self,
        store: TaskStore,
        *,
        default_ttl_ms: int | None = 24 * 3600 * 1000,
        default_poll_interval_ms: int | None = 2000,
        now: Clock = utc_now,
    ) -> None:
        """`now` must return timezone-aware datetimes; it defaults to the wall clock (UTC)."""
        _check_non_negative("default_ttl_ms", default_ttl_ms)
        _check_non_negative("default_poll_interval_ms", default_poll_interval_ms)
        self.store = store
        self.default_ttl_ms = default_ttl_ms
        self.default_poll_interval_ms = default_poll_interval_ms
        self._clock = now
        self._changed: dict[str, anyio.Event] = {}

    def now(self) -> datetime:
        """The manager's current instant in UTC (from the injected clock)."""
        instant = self._clock()
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("the TaskManager clock must return timezone-aware datetimes")
        return instant.astimezone(UTC)

    async def create(
        self,
        *,
        principal: str,
        tool_name: str,
        arguments: dict[str, Any],
        status_message: str | None,
        ttl_ms: int | None = None,
        poll_interval_ms: int | None = None,
        task_id: str | None = None,
        labels: dict[str, str] | None = None,
        refresh_ttl: bool = False,
    ) -> TaskRecord:
        """Create a `working` task, or return the existing one when `task_id` is reused.

        A caller-chosen `task_id` makes creation idempotent: a retry with the same id, principal and
        tool returns the stored live record unchanged (whatever its status). A record whose TTL has
        passed is no longer addressable by anyone, so it is replaced by a fresh `working` record
        rather than handed back dead. A reused id with a different principal or tool raises
        `DuplicateTask`. The id is a bearer capability, so derive it with a keyed hash (HMAC), never
        from guessable input alone. Without `task_id` a 128-bit random id is minted.

        `refresh_ttl=True` is for a retry that starts its own deadline (e.g. a request that is only
        now being committed): a reused *active* record is made to stay addressable at least `ttl_ms`
        (or the default; None = unlimited) from now, in one compare-and-swap. A TTL is only ever
        extended, never shortened, and terminal records are still returned unchanged. Without it, a
        retry made long after the first attempt could be handed a task that expires before the work
        it now tracks.
        """
        _check_non_negative("ttl_ms", ttl_ms)
        _check_non_negative("poll_interval_ms", poll_interval_ms)
        if task_id is not None and not task_id:
            raise ValueError("task_id must be a non-empty string")
        now = self.now()
        record = TaskRecord(
            task_id=task_id or secrets.token_urlsafe(16),
            principal=principal,
            tool_name=tool_name,
            arguments=arguments,
            status="working",
            status_message=status_message,
            created_at=now,
            last_updated_at=now,
            ttl_ms=ttl_ms if ttl_ms is not None else self.default_ttl_ms,
            poll_interval_ms=poll_interval_ms if poll_interval_ms is not None else self.default_poll_interval_ms,
            labels=dict(labels or {}),
        )
        for _ in range(_CAS_ATTEMPTS):
            try:
                await self.store.put_new(record)
            except DuplicateTask:
                if task_id is None:
                    raise
                existing = await self.store.get(task_id)
                if existing is None:
                    continue  # deleted between the two calls (e.g. TTL garbage collection): try again
                if not existing.expired(now):
                    if existing.principal != principal or existing.tool_name != tool_name:
                        raise
                    refreshed = _with_ttl_from(existing, record.ttl_ms, now) if refresh_ttl else None
                    if refreshed is None:
                        return existing
                    if await self.store.replace(refreshed, expected_version=existing.version):
                        self._bump(task_id)
                        return refreshed
                    continue  # it changed under us (a transition, another refresh): look again
                fresh = replace(record, version=existing.version + 1)
                if await self.store.replace(fresh, expected_version=existing.version):
                    self._bump(task_id)
                    return fresh
                continue  # someone else replaced it first: look again
            return record
        raise RuntimeError(f"too much contention creating task {task_id}")

    async def get(self, task_id: str, *, principal: str | None = None) -> TaskRecord:
        """Load a live task. Raises `TaskNotFound` for missing, expired or foreign tasks."""
        record = await self.store.get(task_id)
        if record is None or record.expired(self.now()) or (principal is not None and record.principal != principal):
            raise TaskNotFound(task_id)
        return record

    async def list_for(self, principal: str, *, cursor: str | None, limit: int) -> tuple[list[TaskRecord], str | None]:
        """One page of `principal`'s live tasks, oldest first. A malformed cursor raises `ValueError`.

        Expired records are dropped after paging, so a page can hold fewer than `limit` rows while a
        next cursor is still returned.
        """
        rows, nxt = await self.store.list_for(principal, cursor=cursor, limit=limit)
        now = self.now()
        return [r for r in rows if not r.expired(now)], nxt

    async def _transition(self, task_id: str, allowed_from: frozenset[str], **changes: Any) -> TaskRecord:
        for _ in range(_CAS_ATTEMPTS):
            current = await self.get(task_id)
            if current.status not in allowed_from:
                raise InvalidTransition(f"task {task_id} is {current.status!r}")
            updated = replace(
                current,
                **changes,
                last_updated_at=max(self.now(), current.last_updated_at),
                version=current.version + 1,
            )
            if await self.store.replace(updated, expected_version=current.version):
                self._bump(task_id)
                return updated
        raise RuntimeError(f"too much contention updating task {task_id}")

    # -- business-side API (owner webhook, workers, schedulers) -----------------------------

    async def set_message(self, task_id: str, message: str) -> TaskRecord:
        """Update `statusMessage` of an active task (clients see it on their next tasks/get)."""
        return await self._transition(task_id, ACTIVE, status_message=message)

    async def set_ttl(self, task_id: str, ttl_ms: int | None) -> TaskRecord:
        """Change an active task's TTL (counted from `createdAt`; None = unlimited), e.g. when the
        business decides a request may keep waiting past its original deadline."""
        _check_non_negative("ttl_ms", ttl_ms)
        return await self._transition(task_id, ACTIVE, ttl_ms=ttl_ms)

    async def complete(self, task_id: str, result: types.CallToolResult, *, message: str | None = None) -> TaskRecord:
        """Finish the task with the tool's final result.

        The result must validate against the originating tool's `outputSchema`; `Client.call_tool`
        re-validates it after resolving the task.
        """
        return await self._transition(
            task_id,
            ACTIVE,
            status="completed",
            status_message=message,
            input_requests=None,
            result=result.model_dump(by_alias=True, mode="json", exclude_none=True),
        )

    async def fail(self, task_id: str, message: str, *, code: int = INTERNAL_ERROR, data: Any = None) -> TaskRecord:
        """Finish the task with a JSON-RPC style error."""
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return await self._transition(
            task_id, ACTIVE, status="failed", status_message=message, input_requests=None, error=error
        )

    async def cancel(self, task_id: str, message: str = "Cancelled") -> TaskRecord:
        """Cancel an active task (client request, expiry sweep, or business decision)."""
        return await self._transition(task_id, ACTIVE, status="cancelled", status_message=message, input_requests=None)

    async def require_input(self, task_id: str, requests: dict[str, types.InputRequest], message: str) -> TaskRecord:
        """Move a working task to `input_required` with embedded (wire-shaped) input requests.

        Use a fresh key for every question: clients (ours and strands') skip keys they have already
        answered while the task stays `input_required`.
        """
        if not requests:
            raise ValueError("require_input needs at least one input request")
        dumped = {k: r.model_dump(by_alias=True, mode="json", exclude_none=True) for k, r in requests.items()}
        return await self._transition(
            task_id, frozenset({"working"}), status="input_required", status_message=message, input_requests=dumped
        )

    # -- client-side API (driven by tasks/update) --------------------------------------------

    async def submit_input(self, task_id: str, principal: str, responses: dict[str, Any]) -> TaskRecord:
        """Record answers to outstanding input requests; back to `working` once all are answered.

        Every accepted answer to a form elicitation must match that form's `requestedSchema` (types,
        lengths, choices, required fields, no extra fields) and the whole payload must stay under
        `MAX_INPUT_RESPONSES_BYTES`; otherwise `InvalidInput` is raised and nothing is stored.

        What is stored of an elicitation answer is only what MCP gives meaning to (`_stored_answer`):
        its `action`, plus the validated `content` of an accepted form. A decline or cancel that
        carries a `content` payload (which MCP only defines on accept) is stored as `{"action": ...}`,
        so no unvalidated payload outlives the call in the task record.
        """
        current = await self.get(task_id, principal=principal)
        if current.status != "input_required" or not current.input_requests:
            raise InvalidTransition(f"task {task_id} is not waiting for input")
        unknown = set(responses) - set(current.input_requests)
        if unknown:
            raise InvalidTransition(f"unknown input keys {sorted(unknown)}")
        _check_responses(current.input_requests, responses)
        cleaned = {key: _stored_answer(current.input_requests[key], response) for key, response in responses.items()}
        outstanding = {k: v for k, v in current.input_requests.items() if k not in cleaned}
        return await self._transition(
            task_id,
            frozenset({"input_required"}),
            status="input_required" if outstanding else "working",
            input_requests=outstanding or None,
            input_responses={**current.input_responses, **cleaned},
            status_message=current.status_message if outstanding else "Input received; continuing",
        )

    # -- in-process waiting (single replica only; poll the store across replicas) ------------

    def _bump(self, task_id: str) -> None:
        event = self._changed.pop(task_id, None)
        if event is not None:
            event.set()

    async def wait_for_change(self, task_id: str, timeout: float) -> bool:
        """Wait until this process changes the task, or `timeout` seconds. Single-replica nicety only."""
        event = self._changed.setdefault(task_id, anyio.Event())
        with anyio.move_on_after(timeout):
            await event.wait()
            return True
        return False


def _check_responses(requests: dict[str, dict[str, Any]], responses: dict[str, Any]) -> None:
    """Raise `InvalidInput` unless every accepted form answer matches its question's schema."""
    if len(json.dumps(responses, default=str)) > MAX_INPUT_RESPONSES_BYTES:
        raise InvalidInput("the answer is too large")
    for key, response in responses.items():
        if not isinstance(response, dict) or response.get("action") != "accept":
            continue
        schema = requested_schema(requests[key])
        if schema is None:
            continue
        try:
            check_form_content(schema, response.get("content") or {})
        except InvalidContent as exc:
            raise InvalidInput(f"{key}: {exc}") from None


def _stored_answer(request: dict[str, Any], response: Any) -> Any:
    """What the task record keeps of one answer.

    An elicitation answer (a dict with an `action`) keeps its `action` and, only when it is an
    accepted form answer, its `content` (already checked against the form by `_check_responses`).
    MCP defines `content` on accept only, so a decline or cancel is reduced to `{"action": ...}`:
    a payload smuggled into a non-accept answer (or into `_meta`) is never validated and must not be
    stored for the task's whole TTL. Other answers (sampling, roots) are kept as given."""
    if not isinstance(response, dict) or "action" not in response:
        return response
    kept: dict[str, Any] = {"action": response["action"]}
    if response["action"] == "accept" and "content" in response and requested_schema(request) is not None:
        kept["content"] = response["content"]
    return kept


def _check_non_negative(name: str, value: int | None) -> None:
    if value is not None and value < 0:
        raise ValueError(f"{name} must be >= 0 or None, got {value}")


def _with_ttl_from(existing: TaskRecord, ttl_ms: int | None, now: datetime) -> TaskRecord | None:
    """`existing` with its TTL (counted from `createdAt`) extended so it stays addressable at least
    `ttl_ms` past `now` (None = unlimited), as the next version; None when it already does, or when it
    is terminal (its outcome is final; `set_ttl` refuses terminal records for the same reason)."""
    if existing.status not in ACTIVE or existing.ttl_ms is None:
        return None
    if ttl_ms is None:
        needed: int | None = None
    else:
        elapsed_ms = math.ceil((now - existing.created_at) / timedelta(milliseconds=1))
        needed = max(elapsed_ms, 0) + ttl_ms
        if needed <= existing.ttl_ms:
            return None
    return replace(
        existing, ttl_ms=needed, last_updated_at=max(now, existing.last_updated_at), version=existing.version + 1
    )
