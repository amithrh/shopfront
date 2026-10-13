"""`TaskRecord` and its SEP-2663 wire rendering (the field rules strands-agents 1.57.1 validates)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import mcp_types as types
import pytest

from mcp_tasks import InMemoryTaskStore, TaskManager, TaskRecord, iso_utc
from support import EPOCH, ManualClock, check_get_body, check_handle_body, form_question

pytestmark = pytest.mark.anyio


def _record(**overrides: object) -> TaskRecord:
    base: dict[str, object] = {
        "task_id": "t-1",
        "principal": "anonymous",
        "tool_name": "tool",
        "arguments": {},
        "status": "working",
        "created_at": EPOCH,
        "last_updated_at": EPOCH,
        "ttl_ms": 60_000,
        "poll_interval_ms": 500,
    }
    base.update(overrides)
    return TaskRecord(**base)  # type: ignore[arg-type]


async def _all_statuses() -> dict[str, TaskRecord]:
    """One record per status, reached through real transitions on a ticking clock."""
    clock = ManualClock(tick=timedelta(milliseconds=7))
    manager = TaskManager(InMemoryTaskStore(), now=clock, default_poll_interval_ms=1500)
    out: dict[str, TaskRecord] = {}

    async def fresh(name: str) -> TaskRecord:
        return await manager.create(principal="p", tool_name="tool", arguments={}, status_message=f"{name} task")

    out["working"] = await fresh("working")
    record = await fresh("input_required")
    out["input_required"] = await manager.require_input(record.task_id, {"q1": form_question("Size?")}, "Question")
    record = await fresh("completed")
    result = types.CallToolResult(content=[types.TextContent(type="text", text="done")], structured_content={"a": 1})
    out["completed"] = await manager.complete(record.task_id, result, message="Done")
    record = await fresh("failed")
    out["failed"] = await manager.fail(record.task_id, "boom", data={"why": "test"})
    record = await fresh("cancelled")
    out["cancelled"] = await manager.cancel(record.task_id, "Cancelled by the client")
    return out


async def test_wire_bodies_for_all_five_statuses_follow_the_strands_field_rules() -> None:
    records = await _all_statuses()
    assert set(records) == {"working", "input_required", "completed", "failed", "cancelled"}
    for status, record in records.items():
        get_body = record.wire("complete")
        handle_body = record.wire("task")
        assert get_body["status"] == status
        check_get_body(get_body)
        check_handle_body(handle_body)
        assert get_body["createdAt"] < get_body["lastUpdatedAt"] or status == "working"


async def test_status_payload_shapes() -> None:
    records = await _all_statuses()
    asked = records["input_required"].wire("complete")["inputRequests"]
    assert asked["q1"]["method"] == "elicitation/create"
    assert asked["q1"]["params"]["message"] == "Size?"
    completed = records["completed"].wire("complete")["result"]
    assert completed["structuredContent"] == {"a": 1}
    assert completed["content"] == [{"type": "text", "text": "done"}]
    assert records["failed"].wire("complete")["error"] == {"code": -32603, "message": "boom", "data": {"why": "test"}}


def test_ttl_ms_is_present_and_null_when_unlimited() -> None:
    body = _record(ttl_ms=None, poll_interval_ms=None, status_message=None).wire("task")
    assert "ttlMs" in body and body["ttlMs"] is None
    assert "pollIntervalMs" not in body and "statusMessage" not in body
    check_handle_body(body)


def test_timestamps_are_utc_with_z_and_millisecond_precision() -> None:
    sydney = timezone(timedelta(hours=11))
    created = datetime(2026, 10, 15, 3, 0, 0, 123456, tzinfo=sydney)
    body = _record(created_at=created, last_updated_at=created).wire("complete")
    assert body["createdAt"] == "2026-10-14T16:00:00.123Z"
    assert iso_utc(EPOCH) == "2026-10-14T16:00:00.000Z"


def test_summary_wire_drops_result_type_and_payloads() -> None:
    record = _record(status="failed", error={"code": 1, "message": "x"})
    summary = record.summary_wire()
    assert "resultType" not in summary and "error" not in summary
    assert summary["taskId"] == "t-1" and summary["status"] == "failed"


def test_expiry_is_measured_from_created_at() -> None:
    record = _record(ttl_ms=60_000)
    assert record.expires_at() == EPOCH + timedelta(minutes=1)
    assert not record.expired(EPOCH + timedelta(minutes=1))
    assert record.expired(EPOCH + timedelta(minutes=1, milliseconds=1))
    unlimited = _record(ttl_ms=None)
    assert unlimited.expires_at() is None
    assert not unlimited.expired(EPOCH + timedelta(days=10_000))


def test_terminal_flag() -> None:
    assert not _record(status="working").terminal
    assert not _record(status="input_required").terminal
    assert all(_record(status=s).terminal for s in ("completed", "failed", "cancelled"))
