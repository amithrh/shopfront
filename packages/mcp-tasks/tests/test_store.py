"""The `TaskStore` contract, checked on `InMemoryTaskStore` (DynamoDB has its own Stubber tests)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from mcp_tasks import DuplicateTask, InMemoryTaskStore, TaskRecord, TaskStore
from mcp_tasks.dynamodb import DynamoTaskStore
from support import EPOCH

pytestmark = pytest.mark.anyio


def _record(task_id: str, *, principal: str = "alice", minutes: int = 0) -> TaskRecord:
    at = EPOCH + timedelta(minutes=minutes)
    return TaskRecord(
        task_id=task_id,
        principal=principal,
        tool_name="tool",
        arguments={},
        status="working",
        created_at=at,
        last_updated_at=at,
        ttl_ms=None,
        poll_interval_ms=None,
    )


def test_both_stores_satisfy_the_protocol() -> None:
    assert isinstance(InMemoryTaskStore(), TaskStore)
    assert isinstance(DynamoTaskStore(object(), "table"), TaskStore)


async def test_put_new_is_a_conditional_create() -> None:
    store = InMemoryTaskStore()
    await store.put_new(_record("a"))
    with pytest.raises(DuplicateTask):
        await store.put_new(_record("a", principal="bob"))
    stored = await store.get("a")
    assert stored is not None and stored.principal == "alice"
    assert await store.get("missing") is None


async def test_replace_is_a_compare_and_swap() -> None:
    store = InMemoryTaskStore()
    original = _record("a")
    await store.put_new(original)
    v1 = replace(original, status_message="one", version=1)
    assert await store.replace(v1, expected_version=0) is True
    stale = replace(original, status_message="stale", version=1)
    assert await store.replace(stale, expected_version=0) is False  # someone already wrote v1
    assert (await store.get("a")) == v1
    assert await store.replace(_record("ghost"), expected_version=0) is False  # missing: no upsert
    assert await store.get("ghost") is None


async def test_list_for_pages_oldest_first_per_principal() -> None:
    store = InMemoryTaskStore()
    for minutes, task_id in [(3, "c"), (1, "a"), (2, "b"), (4, "d")]:
        await store.put_new(_record(task_id, minutes=minutes))
    await store.put_new(_record("other", principal="bob"))
    page1, cursor = await store.list_for("alice", cursor=None, limit=3)
    assert [r.task_id for r in page1] == ["a", "b", "c"] and cursor is not None
    page2, cursor = await store.list_for("alice", cursor=cursor, limit=3)
    assert [r.task_id for r in page2] == ["d"] and cursor is None
    assert await store.list_for("nobody", cursor=None, limit=3) == ([], None)


async def test_list_for_rejects_bad_input() -> None:
    store = InMemoryTaskStore()
    with pytest.raises(ValueError):
        await store.list_for("alice", cursor="not-a-number", limit=3)
    with pytest.raises(ValueError):
        await store.list_for("alice", cursor=None, limit=0)
