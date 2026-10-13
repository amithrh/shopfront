"""Task record and status vocabulary for the SEP-2663 tasks extension.

The record is the single persisted shape: every store serialises exactly these fields, and
`TaskRecord.wire()` renders the SEP-2663 task object that `tools/call` (as `resultType: "task"`)
and `tasks/get` (as `resultType: "complete"`) put on the wire.

Wire shapes follow the SEP-2663 models in strands-agents 1.57.1 (`strands/tools/mcp/mcp_tasks.py`),
the only installed independent implementation; our server passes its strict validators:

  * `ttlMs` is always present (`null` = unlimited) and never negative;
  * timestamps are RFC 3339 UTC with a `Z` suffix and `lastUpdatedAt >= createdAt`;
  * a `tasks/get` body carries exactly one status payload (`inputRequests` | `result` | `error`) for
    `input_required` | `completed` | `failed`, and none for `working` / `cancelled`;
  * the `tools/call` handle never carries a payload.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, TypeAlias

TASKS_EXTENSION_ID = "io.modelcontextprotocol/tasks"
"""Extension identifier advertised under `capabilities.extensions` by both sides."""

TASKS_PROTOCOL_VERSIONS: frozenset[str] = frozenset({"2026-07-28"})
"""Wire versions on which a task handle may be returned. Legacy wires sieve unknown result types."""

TaskStatus = Literal["working", "input_required", "completed", "failed", "cancelled"]
TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
ACTIVE: frozenset[str] = frozenset({"working", "input_required"})

Clock: TypeAlias = Callable[[], datetime]
"""A source of timezone-aware "now" instants (inject a fake one in tests)."""


def utc_now() -> datetime:
    """Current UTC instant (timezone-aware)."""
    return datetime.now(UTC)


def iso_utc(dt: datetime) -> str:
    """RFC 3339 UTC timestamp with millisecond precision and a `Z` suffix."""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class TaskRecord:
    """One task as persisted. JSON-friendly so it maps 1:1 onto a document store item.

    `version` is an optimistic-concurrency counter: every state change is a compare-and-swap on it,
    so the owner webhook, `tasks/update` and `tasks/cancel` can race safely across replicas.
    `labels` are opaque caller tags (e.g. a tenant id) that the extension never interprets.
    """

    task_id: str
    principal: str
    tool_name: str
    arguments: dict[str, Any]
    status: TaskStatus
    created_at: datetime
    last_updated_at: datetime
    ttl_ms: int | None
    poll_interval_ms: int | None
    status_message: str | None = None
    input_requests: dict[str, dict[str, Any]] | None = None  # wire-shaped InputRequest dumps
    input_responses: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])  # all answers so far
    result: dict[str, Any] | None = None  # wire-shaped CallToolResult
    error: dict[str, Any] | None = None  # {"code", "message", "data"?}
    labels: dict[str, str] = field(default_factory=dict[str, str])
    version: int = 0

    @property
    def terminal(self) -> bool:
        """True once the task is completed, failed or cancelled."""
        return self.status in TERMINAL

    def expires_at(self) -> datetime | None:
        """When the task stops being addressable, or None for an unlimited TTL."""
        if self.ttl_ms is None:
            return None
        return self.created_at + timedelta(milliseconds=self.ttl_ms)

    def expired(self, now: datetime | None = None) -> bool:
        """True once `createdAt + ttlMs` has passed at `now` (default: the wall clock)."""
        expires = self.expires_at()
        return expires is not None and (now if now is not None else utc_now()) > expires

    def wire(self, result_type: Literal["task", "complete"]) -> dict[str, Any]:
        """SEP-2663 task object. `"task"` for the tools/call handle, `"complete"` for tasks/get.

        The status-specific payload (`inputRequests`, `result` or `error`) rides tasks/get only, and
        exactly one is present for the matching status.
        """
        body: dict[str, Any] = {
            "resultType": result_type,
            "taskId": self.task_id,
            "status": self.status,
            "createdAt": iso_utc(self.created_at),
            "lastUpdatedAt": iso_utc(self.last_updated_at),
            "ttlMs": self.ttl_ms,  # required on the wire; null = unlimited
        }
        if self.status_message is not None:
            body["statusMessage"] = self.status_message
        if self.poll_interval_ms is not None:
            body["pollIntervalMs"] = self.poll_interval_ms
        if result_type == "complete":
            if self.status == "input_required":
                body["inputRequests"] = self.input_requests
            elif self.status == "completed":
                body["result"] = self.result
            elif self.status == "failed":
                body["error"] = self.error
        return body

    def summary_wire(self) -> dict[str, Any]:
        """Task fields without `resultType` or any status payload (used by `tasks/list`)."""
        body = self.wire("complete")
        for key in ("resultType", "inputRequests", "result", "error"):
            body.pop(key, None)
        return body
