"""Task persistence seam plus a process-local implementation.

Implement `TaskStore` over any store with a conditional write (DynamoDB, Redis, Postgres) to run the
extension on several replicas. `mcp_tasks.dynamodb.DynamoTaskStore` is the reference durable store.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import anyio

from mcp_tasks.models import TaskRecord


class DuplicateTask(Exception):
    """A task id is already taken: by `put_new`, or by `TaskManager.create` for another principal/tool."""


@runtime_checkable
class TaskStore(Protocol):
    """Persistence seam for task records.

    Contract:
      * `put_new` is a conditional create: it raises `DuplicateTask` if the id exists.
      * `replace` is a compare-and-swap on `version`: it writes only if the stored version equals
        `expected_version` and returns False otherwise (never raises for a conflict, including a
        missing record).
      * `get` is strongly consistent (a write is visible to the next `get` on any replica).
      * `list_for` pages a principal's tasks oldest first; the cursor is opaque and a malformed one
        raises `ValueError`. It may lag writes slightly (e.g. a DynamoDB GSI) and may include expired
        records; `TaskManager.list_for` filters those out.
    """

    async def put_new(self, record: TaskRecord) -> None: ...

    async def get(self, task_id: str) -> TaskRecord | None: ...

    async def replace(self, record: TaskRecord, *, expected_version: int) -> bool: ...

    async def list_for(
        self, principal: str, *, cursor: str | None, limit: int
    ) -> tuple[list[TaskRecord], str | None]: ...


class InMemoryTaskStore:
    """Process-local store for tests and single-replica development. Lost on restart."""

    def __init__(self) -> None:
        self._rows: dict[str, TaskRecord] = {}
        self._lock = anyio.Lock()

    async def put_new(self, record: TaskRecord) -> None:
        async with self._lock:
            if record.task_id in self._rows:
                raise DuplicateTask(record.task_id)
            self._rows[record.task_id] = record

    async def get(self, task_id: str) -> TaskRecord | None:
        return self._rows.get(task_id)

    async def replace(self, record: TaskRecord, *, expected_version: int) -> bool:
        async with self._lock:
            current = self._rows.get(record.task_id)
            if current is None or current.version != expected_version:
                return False
            self._rows[record.task_id] = record
            return True

    async def list_for(self, principal: str, *, cursor: str | None, limit: int) -> tuple[list[TaskRecord], str | None]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        start = _offset(cursor)
        mine = sorted(
            (r for r in self._rows.values() if r.principal == principal), key=lambda r: (r.created_at, r.task_id)
        )
        page = mine[start : start + limit]
        nxt = str(start + limit) if start + limit < len(mine) else None
        return page, nxt

    def all(self) -> list[TaskRecord]:
        """Every stored record (test helper)."""
        return list(self._rows.values())


def _offset(cursor: str | None) -> int:
    """Decode an `InMemoryTaskStore` cursor (a non-negative offset)."""
    if not cursor:
        return 0
    if not cursor.isdigit():
        raise ValueError("invalid cursor")
    return int(cursor)
