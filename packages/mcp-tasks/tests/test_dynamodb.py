"""`DynamoTaskStore` against botocore's `Stubber` (ap-southeast-2, dummy credentials, never real AWS).

The Stubber validates every request against the DynamoDB service model and answers from a queue, so
these tests prove the request shapes without any network access.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import botocore.session
import mcp_types as types
import pytest
from botocore.stub import ANY, Stubber

from mcp_tasks import DuplicateTask, TaskManager, TaskRecord
from mcp_tasks.dynamodb import (
    DynamoTaskStore,
    record_from_doc,
    record_to_doc,
    sort_key_timestamp,
    table_definition,
    ttl_definition,
)
from support import EPOCH, ManualClock, form_question

pytestmark = pytest.mark.anyio

TABLE = "mcp-tasks-test"


@pytest.fixture
def ddb(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, Stubber]]:
    """A DynamoDB client wrapped in an active Stubber; no profile, config or credentials are read."""
    for var in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", os.devnull)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", os.devnull)
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    client = botocore.session.Session().create_client(
        "dynamodb", region_name="ap-southeast-2", aws_access_key_id="testing", aws_secret_access_key="testing"
    )
    with Stubber(client) as stub:
        yield client, stub
        stub.assert_no_pending_responses()


@pytest.fixture
def store(ddb: tuple[Any, Stubber]) -> DynamoTaskStore:
    return DynamoTaskStore(ddb[0], TABLE)


@pytest.fixture
def stub(ddb: tuple[Any, Stubber]) -> Stubber:
    return ddb[1]


def _record(task_id: str = "t-1", **overrides: Any) -> TaskRecord:
    base: dict[str, Any] = {
        "task_id": task_id,
        "principal": "alice",
        "tool_name": "approve",
        "arguments": {"summary": "cake"},
        "status": "working",
        "created_at": EPOCH,
        "last_updated_at": EPOCH,
        "ttl_ms": 90_000,
        "poll_interval_ms": 2000,
        "labels": {"tenant": "t1"},
    }
    base.update(overrides)
    return TaskRecord(**base)


def _get_params(task_id: str, *, key_attr: str = "taskId", prefix: str = "") -> dict[str, Any]:
    return {"TableName": TABLE, "Key": {key_attr: {"S": f"{prefix}{task_id}"}}, "ConsistentRead": True}


def _cas_params(version: int) -> dict[str, Any]:
    return {
        "TableName": TABLE,
        "Item": ANY,
        "ConditionExpression": "#v = :expected",
        "ExpressionAttributeNames": {"#v": "version"},
        "ExpressionAttributeValues": {":expected": {"N": str(version)}},
    }


def _conditional_failure(stub: Stubber, operation: str, expected: dict[str, Any]) -> None:
    stub.add_client_error(
        operation,
        service_error_code="ConditionalCheckFailedException",
        service_message="The conditional request failed",
        http_status_code=400,
        expected_params=expected,
    )


# --------------------------------------------------------------------------- items and documents


def test_item_layout(store: DynamoTaskStore) -> None:
    record = _record()
    item = store.item_for(record)
    assert item["taskId"] == {"S": "t-1"}
    assert item["principal"] == {"S": "alice"}
    assert item["createdAt"] == {"S": "2026-10-14T16:00:00.000000Z"}
    assert item["status"] == {"S": "working"} and item["version"] == {"N": "0"}
    assert item["expiresAt"] == {"N": str(int((EPOCH + timedelta(seconds=90)).timestamp()))}
    assert record_from_doc(item["doc"]["S"]) == record
    assert "expiresAt" not in store.item_for(_record(ttl_ms=None))


def test_expires_at_rounds_up_to_whole_seconds(store: DynamoTaskStore) -> None:
    item = store.item_for(_record(ttl_ms=1500))
    assert item["expiresAt"] == {"N": str(int(EPOCH.timestamp()) + 2)}


def test_sort_key_is_fixed_width_utc() -> None:
    from datetime import timezone

    sydney = EPOCH.astimezone(timezone(timedelta(hours=11)))
    assert sort_key_timestamp(sydney) == "2026-10-14T16:00:00.000000Z"
    later = sort_key_timestamp(EPOCH + timedelta(microseconds=5))
    assert sort_key_timestamp(EPOCH) < later  # lexicographic order == chronological order


def test_document_round_trip_with_every_payload() -> None:
    record = _record(
        status="completed",
        status_message="Done",
        input_requests={"q": form_question("?").model_dump(by_alias=True, mode="json", exclude_none=True)},
        input_responses={"q": {"action": "accept", "content": {"answer": "x"}}},
        result={"content": [{"type": "text", "text": "ok"}], "isError": False},
        error={"code": 1, "message": "m", "data": {"k": [1, 2]}},
        version=7,
    )
    assert record_from_doc(record_to_doc(record)) == record


def test_unknown_document_keys_are_ignored() -> None:
    doc = record_to_doc(_record())
    newer = doc[:-1] + ',"field_from_the_future":42}'
    assert record_from_doc(newer) == _record()


# --------------------------------------------------------------------------- put_new / get / replace


async def test_put_new_is_conditional_and_duplicates_raise(store: DynamoTaskStore, stub: Stubber) -> None:
    record = _record()
    expected = {
        "TableName": TABLE,
        "Item": store.item_for(record),
        "ConditionExpression": "attribute_not_exists(#k)",
        "ExpressionAttributeNames": {"#k": "taskId"},
    }
    stub.add_response("put_item", {}, expected)
    await store.put_new(record)
    _conditional_failure(stub, "put_item", expected)
    with pytest.raises(DuplicateTask):
        await store.put_new(record)


async def test_other_client_errors_propagate(store: DynamoTaskStore, stub: Stubber, ddb: tuple[Any, Stubber]) -> None:
    stub.add_client_error("put_item", service_error_code="ProvisionedThroughputExceededException", http_status_code=400)
    with pytest.raises(ddb[0].exceptions.ProvisionedThroughputExceededException):
        await store.put_new(_record())


async def test_get_round_trip_and_missing(store: DynamoTaskStore, stub: Stubber) -> None:
    record = _record(status="input_required", input_requests={"q": {"method": "elicitation/create"}}, version=3)
    stub.add_response("get_item", {"Item": store.item_for(record)}, _get_params("t-1"))
    assert await store.get("t-1") == record
    stub.add_response("get_item", {}, _get_params("nope"))
    assert await store.get("nope") is None


async def test_replace_is_a_cas_on_version(store: DynamoTaskStore, stub: Stubber) -> None:
    updated = _record(status_message="x", version=4)
    stub.add_response("put_item", {}, {**_cas_params(3), "Item": store.item_for(updated)})
    assert await store.replace(updated, expected_version=3) is True
    _conditional_failure(stub, "put_item", _cas_params(3))
    assert await store.replace(updated, expected_version=3) is False


# --------------------------------------------------------------------------- through TaskManager


async def test_manager_create_then_cas_conflict_then_retry(store: DynamoTaskStore, stub: Stubber) -> None:
    clock = ManualClock(tick=timedelta(milliseconds=10))
    manager = TaskManager(store, now=clock)
    stub.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": ANY,
            "ConditionExpression": "attribute_not_exists(#k)",
            "ExpressionAttributeNames": {"#k": "taskId"},
        },
    )
    created = await manager.create(principal="alice", tool_name="approve", arguments={}, status_message="Waiting")

    # Another replica wrote version 1 between our read and our write: the first CAS fails, the retry
    # re-reads version 1 and writes version 2 on top of it.
    v1 = replace(created, status_message="Owner opened the link", version=1)
    stub.add_response("get_item", {"Item": store.item_for(created)}, _get_params(created.task_id))
    _conditional_failure(stub, "put_item", _cas_params(0))
    stub.add_response("get_item", {"Item": store.item_for(v1)}, _get_params(created.task_id))
    stub.add_response("put_item", {}, _cas_params(1))
    done = await manager.complete(
        created.task_id, types.CallToolResult(content=[types.TextContent(type="text", text="ok")])
    )
    assert (done.status, done.version, done.created_at) == ("completed", 2, created.created_at)

    stub.add_response("get_item", {"Item": store.item_for(done)}, _get_params(created.task_id))
    assert await store.get(created.task_id) == done


async def test_manager_reused_id_is_idempotent_through_dynamodb(store: DynamoTaskStore, stub: Stubber) -> None:
    manager = TaskManager(store, now=ManualClock())
    existing = _record("idem", principal="alice", tool_name="approve", status="completed", version=2)
    _conditional_failure(
        stub, "put_item", {"TableName": TABLE, "Item": ANY, "ConditionExpression": ANY, "ExpressionAttributeNames": ANY}
    )
    stub.add_response("get_item", {"Item": store.item_for(existing)}, _get_params("idem"))
    again = await manager.create(
        principal="alice", tool_name="approve", arguments={}, status_message="x", task_id="idem"
    )
    assert again == existing

    _conditional_failure(
        stub, "put_item", {"TableName": TABLE, "Item": ANY, "ConditionExpression": ANY, "ExpressionAttributeNames": ANY}
    )
    stub.add_response("get_item", {"Item": store.item_for(existing)}, _get_params("idem"))
    with pytest.raises(DuplicateTask):
        await manager.create(principal="mallory", tool_name="approve", arguments={}, status_message="x", task_id="idem")


async def test_round2_capacity_race_refresh_ttl_rewrites_expires_at_through_dynamodb(
    store: DynamoTaskStore, stub: Stubber
) -> None:
    """The extended TTL reaches DynamoDB's `expiresAt`, so the table's TTL sweep cannot delete the task
    at the first attempt's deadline."""
    clock = ManualClock()
    manager = TaskManager(store, now=clock)
    existing = _record("idem", version=3)  # created at EPOCH with a 90 s TTL
    clock.advance(seconds=60)
    refreshed = replace(existing, ttl_ms=150_000, last_updated_at=EPOCH + timedelta(seconds=60), version=4)
    _conditional_failure(
        stub, "put_item", {"TableName": TABLE, "Item": ANY, "ConditionExpression": ANY, "ExpressionAttributeNames": ANY}
    )
    stub.add_response("get_item", {"Item": store.item_for(existing)}, _get_params("idem"))
    stub.add_response("put_item", {}, {**_cas_params(3), "Item": store.item_for(refreshed)})
    again = await manager.create(
        principal="alice",
        tool_name="approve",
        arguments={},
        status_message="x",
        task_id="idem",
        ttl_ms=90_000,
        refresh_ttl=True,
    )
    assert again == refreshed
    assert store.item_for(again)["expiresAt"] == {"N": str(int((EPOCH + timedelta(seconds=150)).timestamp()))}


# --------------------------------------------------------------------------- list_for


def _query_params(principal: str, limit: int, start: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "TableName": TABLE,
        "IndexName": "byPrincipal",
        "KeyConditionExpression": "#p = :p",
        "ExpressionAttributeNames": {"#p": "principal"},
        "ExpressionAttributeValues": {":p": {"S": principal}},
        "ScanIndexForward": True,
        "Limit": limit,
    }
    if start is not None:
        params["ExclusiveStartKey"] = start
    return params


async def test_list_for_round_trips_last_evaluated_key_through_the_cursor(
    store: DynamoTaskStore, stub: Stubber
) -> None:
    rows = [
        _record(f"t-{i}", created_at=EPOCH + timedelta(minutes=i), last_updated_at=EPOCH + timedelta(minutes=i))
        for i in range(3)
    ]
    last_key = {
        "taskId": {"S": "t-1"},
        "principal": {"S": "alice"},
        "createdAt": {"S": sort_key_timestamp(rows[1].created_at)},
    }
    stub.add_response(
        "query",
        {"Items": [store.item_for(r) for r in rows[:2]], "Count": 2, "LastEvaluatedKey": last_key},
        _query_params("alice", 2),
    )
    page1, cursor = await store.list_for("alice", cursor=None, limit=2)
    assert page1 == rows[:2] and isinstance(cursor, str)
    assert "=" not in cursor.rstrip("=") and "/" not in cursor and "+" not in cursor  # URL-safe

    stub.add_response("query", {"Items": [store.item_for(rows[2])], "Count": 1}, _query_params("alice", 2, last_key))
    page2, cursor2 = await store.list_for("alice", cursor=cursor, limit=2)
    assert page2 == rows[2:] and cursor2 is None


async def test_list_for_rejects_malformed_or_foreign_cursors(store: DynamoTaskStore, stub: Stubber) -> None:
    stub.add_response(
        "query",
        {
            "Items": [],
            "Count": 0,
            "LastEvaluatedKey": {"taskId": {"S": "t"}, "principal": {"S": "alice"}, "createdAt": {"S": "x"}},
        },
        _query_params("alice", 1),
    )
    _, alice_cursor = await store.list_for("alice", cursor=None, limit=1)
    assert alice_cursor is not None
    for cursor in ("%%%not-base64", "bm90LWpzb24=", "WzEsMl0=", alice_cursor):  # garbage, not JSON, not a dict, alice's
        with pytest.raises(ValueError):
            await store.list_for("bob", cursor=cursor, limit=1)
    with pytest.raises(ValueError):
        await store.list_for("alice", cursor=None, limit=0)


async def test_manager_list_for_drops_expired_rows(store: DynamoTaskStore, stub: Stubber) -> None:
    clock = ManualClock(EPOCH + timedelta(minutes=10))
    manager = TaskManager(store, now=clock)
    expired = _record("old", ttl_ms=60_000)
    live = _record("new", ttl_ms=None)
    stub.add_response(
        "query", {"Items": [store.item_for(expired), store.item_for(live)], "Count": 2}, _query_params("alice", 5)
    )
    rows, cursor = await manager.list_for("alice", cursor=None, limit=5)
    assert [r.task_id for r in rows] == ["new"] and cursor is None


# --------------------------------------------------------------------------- shared-table configuration


async def test_key_attr_and_prefix_namespace_ids_in_a_shared_table(ddb: tuple[Any, Stubber]) -> None:
    client, stub = ddb
    store = DynamoTaskStore(client, TABLE, key_attr="pk", key_prefix="TASK#", principal_index="gsiTasks")
    record = _record("abc")
    item = store.item_for(record)
    assert item["pk"] == {"S": "TASK#abc"} and "taskId" not in item
    stub.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": item,
            "ConditionExpression": "attribute_not_exists(#k)",
            "ExpressionAttributeNames": {"#k": "pk"},
        },
    )
    await store.put_new(record)
    stub.add_response("get_item", {"Item": item}, _get_params("abc", key_attr="pk", prefix="TASK#"))
    loaded = await store.get("abc")
    assert loaded is not None and loaded.task_id == "abc"  # the prefix never leaks into the record
    stub.add_response("query", {"Items": [item], "Count": 1}, {**_query_params("alice", 3), "IndexName": "gsiTasks"})
    rows, _ = await store.list_for("alice", cursor=None, limit=3)
    assert [r.task_id for r in rows] == ["abc"]


# --------------------------------------------------------------------------- table definition


async def test_table_and_ttl_definitions_are_valid_requests(ddb: tuple[Any, Stubber]) -> None:
    client, stub = ddb
    spec = table_definition("my-tasks")
    assert spec["KeySchema"] == [{"AttributeName": "taskId", "KeyType": "HASH"}]
    (gsi,) = spec["GlobalSecondaryIndexes"]
    assert gsi["IndexName"] == "byPrincipal" and [k["AttributeName"] for k in gsi["KeySchema"]] == [
        "principal",
        "createdAt",
    ]
    ttl = ttl_definition("my-tasks")
    assert ttl["TimeToLiveSpecification"] == {"Enabled": True, "AttributeName": "expiresAt"}
    # The Stubber validates request parameters against the service model before answering.
    stub.add_response("create_table", {"TableDescription": {"TableName": "my-tasks"}}, spec)
    stub.add_response("update_time_to_live", {"TimeToLiveSpecification": ttl["TimeToLiveSpecification"]}, ttl)
    client.create_table(**spec)
    client.update_time_to_live(**ttl)
