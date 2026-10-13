"""DynamoDB stores against botocore Stubber (no AWS): request shapes and error mapping.

Every call the stores make is stubbed with its exact expected parameters, so a change to a key,
condition or GSI attribute fails here before it can fail in AWS.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from botocore.stub import ANY, Stubber

from askonce_server.clock import FixedClock
from askonce_server.domain.models import CapacityHold, Commitment
from askonce_server.domain.ports import CapacityExhausted, Committed, Duplicate
from askonce_server.domain.idempotency import new_record
from askonce_server.domain.stores.dynamodb import (
    DynamoCapacityStore,
    DynamoCommitmentStore,
    DynamoIdempotencyStore,
    commitment_item,
    counter_expiry,
    parse_commitment,
    sortable_utc,
)
from askonce_server.errors import ConcurrencyConflict, InvalidState, NotFound

pytestmark = pytest.mark.anyio

TABLE = "askonce"
NOW = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)
DAY = CapacityHold(key="CAP#thimbleberry-bakehouse#custom-cakes#DAY#2026-10-18", units=1, limit=2)
CELL = CapacityHold(key="CAP#quillfern-salon#appointments#CELL#2026-10-15T15:00:00Z#wren", units=1, limit=1)


def make(status: str = "pending_owner", **extra: Any) -> Commitment:
    fields: dict[str, Any] = {
        "commitment_id": "c_abc",
        "business_id": "thimbleberry-bakehouse",
        "module_id": "custom-cakes",
        "kind": "custom_order",
        "status": status,
        "principal": "anonymous",
        "idempotency_key": "idem_k",
        "profile_revision": 3,
        "day": date(2026, 10, 18),
        "holds": (DAY, CELL),
        "task_id": "t_xyz",
        "created_at": NOW,
        "updated_at": NOW,
        "expires_at": NOW + timedelta(hours=48),
    }
    fields.update(extra)
    return Commitment(**fields)


def idem() -> Any:
    return new_record("idem_k", business_id="thimbleberry-bakehouse", commitment_id="c_abc", now=NOW, ttl=timedelta(hours=24))


def cap_update(hold: CapacityHold, expires: int) -> dict[str, Any]:
    return {
        "Update": {
            "TableName": TABLE,
            "Key": {"pk": {"S": hold.key}, "sk": {"S": "CAP"}},
            "UpdateExpression": "SET #limit = :limit, #exp = if_not_exists(#exp, :exp) ADD #used :units",
            "ConditionExpression": "(attribute_not_exists(#used) AND :units <= :limit) OR #used <= :max_before",
            "ExpressionAttributeNames": {"#used": "used", "#limit": "limit", "#exp": "expires_at"},
            "ExpressionAttributeValues": {
                ":units": {"N": str(hold.units)},
                ":limit": {"N": str(hold.limit)},
                ":max_before": {"N": str(hold.limit - hold.units)},
                ":exp": {"N": str(expires)},
            },
        }
    }


def get_item_params(business_id: str = "thimbleberry-bakehouse", commitment_id: str = "c_abc") -> dict[str, Any]:
    return {
        "TableName": TABLE,
        "Key": {"pk": {"S": f"BIZ#{business_id}"}, "sk": {"S": f"C#{commitment_id}"}},
        "ConsistentRead": True,
    }


def cancelled(stub: Stubber, *codes: str) -> None:
    stub.add_client_error(
        "transact_write_items",
        service_error_code="TransactionCanceledException",
        service_message="Transaction cancelled",
        http_status_code=400,
        modeled_fields={"CancellationReasons": [{"Code": code} for code in codes]},
    )


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


# ---------------------------------------------------------------------------- item encoding


def test_item_encoding_round_trips_and_indexes_by_state() -> None:
    commitment = make()
    item = commitment_item(commitment)
    assert parse_commitment(item) == commitment
    assert item["pk"] == {"S": "BIZ#thimbleberry-bakehouse"} and item["sk"] == {"S": "C#c_abc"}
    assert item["gsi1pk"] == {"S": "BIZ#thimbleberry-bakehouse#S#pending_owner"}
    assert item["gsi1sk"] == {"S": "2026-10-14T16:00:00.000000Z#c_abc"}
    assert item["gsi2pk"] == {"S": "TASK#t_xyz"} and item["gsi2sk"] == {"S": "C"}
    assert item["gsi3pk"] == {"S": "OPEN#2026-10-16"} and item["gsi3sk"] == {"S": "2026-10-16T16:00:00.000000Z"}
    assert json.loads(item["doc"]["S"])["status"] == "pending_owner" and item["version"] == {"N": "0"}
    closed = commitment_item(make(status="approved", task_id=None))
    assert "gsi3pk" not in closed and "gsi2pk" not in closed  # leaves the expiry index once decided


def test_counter_expiry_is_two_days_after_the_slot() -> None:
    assert counter_expiry(DAY.key) == int(datetime(2026, 10, 20, tzinfo=UTC).timestamp())
    assert counter_expiry(CELL.key) == int(datetime(2026, 10, 17, tzinfo=UTC).timestamp())
    assert counter_expiry("CAP#no-date") is None


# ---------------------------------------------------------------------------- commit


async def test_commit_is_one_transaction(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    commitment = make()
    expected = {
        "TransactItems": [
            {
                "Put": {
                    "TableName": TABLE,
                    "Item": {
                        "pk": {"S": "IDEM#idem_k"},
                        "sk": {"S": "IDEM"},
                        "commitment_id": {"S": "c_abc"},
                        "business_id": {"S": "thimbleberry-bakehouse"},
                        "created_at": {"S": sortable_utc(NOW)},
                        "expires_at": {"N": str(int((NOW + timedelta(hours=24)).timestamp()))},
                    },
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {"Put": {"TableName": TABLE, "Item": commitment_item(commitment), "ConditionExpression": "attribute_not_exists(pk)"}},
            cap_update(DAY, counter_expiry(DAY.key) or 0),
            cap_update(CELL, counter_expiry(CELL.key) or 0),
        ]
    }
    stub.add_response("transact_write_items", {}, expected)
    assert await store.commit(commitment, holds=commitment.holds, idempotency=idem()) == Committed(commitment)


async def test_duplicate_is_mapped_from_the_conditional_puts(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    existing = make()
    cancelled(stub, "ConditionalCheckFailed", "ConditionalCheckFailed", "None", "None")
    stub.add_response("get_item", {"Item": commitment_item(existing)}, get_item_params())
    assert await store.commit(make(), holds=existing.holds, idempotency=idem()) == Duplicate(existing)


async def test_duplicate_via_the_idempotency_marker(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    existing = make(commitment_id="c_first")
    cancelled(stub, "ConditionalCheckFailed", "None", "None", "None")
    stub.add_response("get_item", {}, get_item_params())  # not under the new id...
    stub.add_response(
        "get_item",
        {"Item": {"pk": {"S": "IDEM#idem_k"}, "sk": {"S": "IDEM"}, "commitment_id": {"S": "c_first"}, "business_id": {"S": "thimbleberry-bakehouse"}}},
        {"TableName": TABLE, "Key": {"pk": {"S": "IDEM#idem_k"}, "sk": {"S": "IDEM"}}, "ConsistentRead": True},
    )
    stub.add_response("get_item", {"Item": commitment_item(existing)}, get_item_params(commitment_id="c_first"))
    assert await store.commit(make(), holds=(DAY,), idempotency=idem()) == Duplicate(existing)


async def test_capacity_exhausted_names_the_full_counter(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    cancelled(stub, "None", "None", "None", "ConditionalCheckFailed")
    assert await store.commit(make(), holds=(DAY, CELL), idempotency=idem()) == CapacityExhausted(CELL.key)


async def test_transaction_conflicts_are_retried(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock, conflict_retries=1)
    cancelled(stub, "None", "TransactionConflict", "None", "None")
    stub.add_response("transact_write_items", {}, {"TransactItems": ANY})
    assert isinstance(await store.commit(make(), holds=(DAY, CELL), idempotency=idem()), Committed)
    cancelled(stub, "None", "TransactionConflict", "None", "None")
    cancelled(stub, "None", "TransactionConflict", "None", "None")
    with pytest.raises(ConcurrencyConflict):
        await store.commit(make(), holds=(DAY, CELL), idempotency=idem())


async def test_other_errors_propagate(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    from botocore.exceptions import ClientError

    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    stub.add_client_error("transact_write_items", service_error_code="ProvisionedThroughputExceededException", http_status_code=400)
    with pytest.raises(ClientError):
        await store.commit(make(), holds=(), idempotency=idem())


async def test_too_many_holds_are_refused(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, _ = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    holds = tuple(CapacityHold(key=f"CAP#b#m#CELL#2026-10-15T{h:02d}:{m:02d}:00Z#-", units=1, limit=1) for h in range(24) for m in range(0, 60, 10))
    with pytest.raises(ValueError, match="at most"):
        await store.commit(make(holds=holds), holds=holds, idempotency=idem())


# ---------------------------------------------------------------------------- reads


async def test_get_and_get_by_task(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    commitment = make()
    stub.add_response("get_item", {"Item": commitment_item(commitment)}, get_item_params())
    stub.add_response("get_item", {}, get_item_params(commitment_id="c_missing"))
    stub.add_response(
        "query",
        {"Items": [commitment_item(commitment)]},
        {
            "TableName": TABLE,
            "IndexName": "gsi2",
            "KeyConditionExpression": "gsi2pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": "TASK#t_xyz"}},
            "Limit": 1,
        },
    )
    assert await store.get("thimbleberry-bakehouse", "c_abc") == commitment
    assert await store.get("thimbleberry-bakehouse", "c_missing") is None
    assert await store.get_by_task("t_xyz") == commitment


async def test_round2_misc_low_find_by_reference_prefix_is_a_begins_with_key_query(
    stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock
) -> None:
    """A spoken booking code is resolved on the base table's own keys: no index, no extra attribute."""
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    booking = make(commitment_id="c_exyqratrum2x44qc423hhejvjm", kind="booking")
    expected = {
        "TableName": TABLE,
        "KeyConditionExpression": "pk = :pk AND begins_with(sk, :sk)",
        "ExpressionAttributeValues": {":pk": {"S": "BIZ#thimbleberry-bakehouse"}, ":sk": {"S": "C#c_exyqr"}},
        "ConsistentRead": True,
        "Limit": 5,
    }
    stub.add_response("query", {"Items": [commitment_item(booking)]}, expected)
    stub.add_response("query", {"Items": []}, {**expected, "ExpressionAttributeValues": {**expected["ExpressionAttributeValues"], ":sk": {"S": "C#c_zzzzz"}}, "Limit": 2})
    assert await store.find_by_reference_prefix("thimbleberry-bakehouse", "c_exyqr") == [booking]
    assert await store.find_by_reference_prefix("thimbleberry-bakehouse", "c_zzzzz", limit=2) == []
    stub.assert_no_pending_responses()


async def test_list_for_business_merges_status_partitions_newest_first(
    stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock
) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    older = make(commitment_id="c_old", status="info_requested")
    newer = make(commitment_id="c_new", created_at=NOW + timedelta(minutes=5))

    def query(status: str, items: list[Commitment], before: str | None = None, more: bool = False) -> None:
        values: dict[str, Any] = {":pk": {"S": f"BIZ#thimbleberry-bakehouse#S#{status}"}}
        condition = "gsi1pk = :pk"
        if before:
            condition += " AND gsi1sk < :before"
            values[":before"] = {"S": before}
        response: dict[str, Any] = {"Items": [commitment_item(c) for c in items]}
        if more:
            response["LastEvaluatedKey"] = {"pk": {"S": "x"}, "sk": {"S": "y"}, "gsi1pk": {"S": "z"}, "gsi1sk": {"S": "w"}}
        stub.add_response(
            "query",
            response,
            {
                "TableName": TABLE,
                "IndexName": "gsi1",
                "KeyConditionExpression": condition,
                "ExpressionAttributeValues": values,
                "ScanIndexForward": False,
                "Limit": 1,
            },
        )

    query("info_requested", [older])
    query("pending_owner", [newer], more=True)
    page, cursor = await store.list_for_business("thimbleberry-bakehouse", statuses=["pending_owner", "info_requested"], limit=1)
    assert [c.commitment_id for c in page] == ["c_new"] and cursor
    before = commitment_item(newer)["gsi1sk"]["S"]
    query("info_requested", [older], before=before)
    query("pending_owner", [], before=before)
    page, cursor = await store.list_for_business("thimbleberry-bakehouse", statuses=["pending_owner", "info_requested"], limit=1, cursor=cursor)
    assert [c.commitment_id for c in page] == ["c_old"] and cursor is None


async def test_list_expiring_scans_recent_expiry_days(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock, expiry_lookback_days=1)
    due = make()
    before = NOW + timedelta(days=3)
    for day, items, limit in (("2026-10-16", [due], 100), ("2026-10-17", [], 99)):
        stub.add_response(
            "query",
            {"Items": [commitment_item(c) for c in items]},
            {
                "TableName": TABLE,
                "IndexName": "gsi3",
                "KeyConditionExpression": "gsi3pk = :pk AND gsi3sk < :before",
                "ExpressionAttributeValues": {":pk": {"S": f"OPEN#{day}"}, ":before": {"S": sortable_utc(before)}},
                "ScanIndexForward": True,
                "Limit": limit,  # what is left of the overall limit
            },
        )
    assert await store.list_expiring(before=before) == [due]


# ---------------------------------------------------------------------------- update


async def test_update_without_release_is_a_conditional_put(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    current = make()
    approved = current.model_copy(update={"status": "approved", "expires_at": None})
    stored = approved.model_copy(update={"version": 1})
    stub.add_response("get_item", {"Item": commitment_item(current)}, get_item_params())
    stub.add_response(
        "put_item",
        {},
        {
            "TableName": TABLE,
            "Item": commitment_item(stored),
            "ConditionExpression": "#version = :expected",
            "ExpressionAttributeNames": {"#version": "version"},
            "ExpressionAttributeValues": {":expected": {"N": "0"}},
        },
    )
    assert await store.update(approved, expected_version=0) == stored


async def test_update_into_a_releasing_status_frees_the_holds_in_the_same_transaction(
    stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock
) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    current = make()
    declined = current.model_copy(update={"status": "declined", "expires_at": None})
    stored = declined.model_copy(update={"version": 1, "holds_released": True})

    def release(hold: CapacityHold) -> dict[str, Any]:
        return {
            "Update": {
                "TableName": TABLE,
                "Key": {"pk": {"S": hold.key}, "sk": {"S": "CAP"}},
                "UpdateExpression": "SET #exp = if_not_exists(#exp, :exp) ADD #used :minus",
                "ExpressionAttributeNames": {"#used": "used", "#exp": "expires_at"},
                "ExpressionAttributeValues": {":minus": {"N": "-1"}, ":exp": {"N": str(counter_expiry(hold.key))}},
            }
        }

    stub.add_response("get_item", {"Item": commitment_item(current)}, get_item_params())
    stub.add_response(
        "transact_write_items",
        {},
        {
            "TransactItems": [
                {
                    "Put": {
                        "TableName": TABLE,
                        "Item": commitment_item(stored),
                        "ConditionExpression": "#version = :expected",
                        "ExpressionAttributeNames": {"#version": "version"},
                        "ExpressionAttributeValues": {":expected": {"N": "0"}},
                    }
                },
                release(DAY),
                release(CELL),
            ]
        },
    )
    result = await store.update(declined, expected_version=0)
    assert result == stored and result.holds_released
    # Released once: a later edit of the declined record writes no capacity update at all.
    stub.add_response("get_item", {"Item": commitment_item(stored)}, get_item_params())
    stub.add_response("put_item", {}, {"TableName": TABLE, "Item": ANY, "ConditionExpression": ANY, "ExpressionAttributeNames": ANY, "ExpressionAttributeValues": ANY})
    assert (await store.update(stored.model_copy(update={"holds_released": False}), expected_version=1)).holds_released


async def test_update_errors(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    current = make()
    stub.add_response("get_item", {}, get_item_params())
    with pytest.raises(NotFound):
        await store.update(current, expected_version=0)
    stub.add_response("get_item", {"Item": commitment_item(current.model_copy(update={"version": 2}))}, get_item_params())
    with pytest.raises(ConcurrencyConflict):
        await store.update(current, expected_version=0)
    stub.add_response("get_item", {"Item": commitment_item(current)}, get_item_params())
    with pytest.raises(InvalidState):
        await store.update(current.model_copy(update={"status": "confirmed"}), expected_version=0)
    stub.add_response("get_item", {"Item": commitment_item(current)}, get_item_params())
    stub.add_client_error("put_item", service_error_code="ConditionalCheckFailedException", http_status_code=400)
    with pytest.raises(ConcurrencyConflict):
        await store.update(current.model_copy(update={"status": "approved"}), expected_version=0)
    stub.add_response("get_item", {"Item": commitment_item(current)}, get_item_params())
    cancelled(stub, "ConditionalCheckFailed", "None", "None")
    with pytest.raises(ConcurrencyConflict):
        await store.update(current.model_copy(update={"status": "declined"}), expected_version=0)


# ---------------------------------------------------------------------------- capacity & idempotency


async def test_usage_batches_and_retries_unprocessed_keys(stubbed_dynamodb: tuple[Any, Stubber]) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoCapacityStore(client, TABLE)
    keys = [f"CAP#b#m#DAY#2026-10-{d:02d}" for d in range(1, 31)] * 2 + [f"CAP#b#m#CELL#{n}" for n in range(120)]
    unique = list(dict.fromkeys(keys))

    def request(chunk: list[str]) -> dict[str, Any]:
        return {
            "RequestItems": {
                TABLE: {
                    "Keys": [{"pk": {"S": k}, "sk": {"S": "CAP"}} for k in chunk],
                    "ProjectionExpression": "pk, #used",
                    "ExpressionAttributeNames": {"#used": "used"},
                    "ConsistentRead": True,
                }
            }
        }

    first, second = unique[:100], unique[100:]
    unprocessed = request(first[50:])["RequestItems"]
    stub.add_response(
        "batch_get_item",
        {"Responses": {TABLE: [{"pk": {"S": first[0]}, "used": {"N": "2"}}]}, "UnprocessedKeys": unprocessed},
        request(first),
    )
    stub.add_response("batch_get_item", {"Responses": {TABLE: [{"pk": {"S": first[60]}, "used": {"N": "1"}}]}}, {"RequestItems": unprocessed})
    stub.add_response("batch_get_item", {"Responses": {TABLE: []}}, request(second))
    usage = await store.usage(keys)
    assert len(usage) == 150 and usage[first[0]] == 2 and usage[first[60]] == 1
    assert sum(usage.values()) == 3
    assert await store.usage([]) == {}


async def test_idempotency_marker(stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock) -> None:
    client, stub = stubbed_dynamodb
    store = DynamoIdempotencyStore(client, TABLE, clock=clock)
    params = {"TableName": TABLE, "Key": {"pk": {"S": "IDEM#idem_k"}, "sk": {"S": "IDEM"}}, "ConsistentRead": True}
    item = {
        "pk": {"S": "IDEM#idem_k"},
        "sk": {"S": "IDEM"},
        "commitment_id": {"S": "c_abc"},
        "business_id": {"S": "thimbleberry-bakehouse"},
        "created_at": {"S": sortable_utc(NOW)},
        "expires_at": {"N": str(int((NOW + timedelta(hours=24)).timestamp()))},
    }
    stub.add_response("get_item", {"Item": item}, params)
    record = await store.get("idem_k")
    assert record == idem()
    clock.advance(timedelta(hours=25))  # expired but not yet deleted by TTL
    stub.add_response("get_item", {"Item": item}, params)
    assert await store.get("idem_k") is None
    stub.add_response("get_item", {}, params)
    assert await store.get("idem_k") is None


# ---------------------------------------------------------------------------- round 2: cancel releases


def owner_over(store: DynamoCommitmentStore, clock: FixedClock) -> Any:
    """An OwnerService whose commitments live in the stubbed table (the hook touches nothing else)."""
    from support import TEST_KEY

    from mcp_tasks import InMemoryTaskStore, TaskManager
    from askonce_server.domain.availability import AvailabilityEngine
    from askonce_server.domain.services import DomainContext, OwnerService
    from askonce_server.domain.stores.memory import InMemoryCapacityStore, InMemoryIdempotencyStore, MemoryState
    from askonce_server.ids import IdFactory

    state = MemoryState()
    ctx = DomainContext(
        commitments=store,
        capacity=InMemoryCapacityStore(state),
        idempotency=InMemoryIdempotencyStore(state, clock=clock),
        availability=AvailabilityEngine(clock),
        ids=IdFactory(TEST_KEY.encode()),
        clock=clock,
        notifier=None,  # type: ignore[arg-type]  # unused by the cancel hook
    )
    return OwnerService(ctx, tasks=TaskManager(InMemoryTaskStore()), renderer=None, profiles=None)  # type: ignore[arg-type]


def cancelled_task(labels: dict[str, str]) -> Any:
    from mcp_tasks import TaskRecord

    return TaskRecord(
        task_id="t_xyz",
        principal="anonymous",
        tool_name="place_custom_order",
        arguments={},
        status="cancelled",
        created_at=NOW,
        last_updated_at=NOW,
        ttl_ms=None,
        poll_interval_ms=None,
        labels=labels,
    )


def withdraw_transaction(current: Commitment) -> dict[str, Any]:
    stored = current.model_copy(
        update={"status": "cancelled", "updated_at": NOW, "expires_at": None, "version": current.version + 1, "holds_released": True}
    )
    put = {
        "TableName": TABLE,
        "Item": commitment_item(stored),
        "ConditionExpression": "#version = :expected",
        "ExpressionAttributeNames": {"#version": "version"},
        "ExpressionAttributeValues": {":expected": {"N": str(current.version)}},
    }
    return {"TransactItems": [{"Put": put}, ANY, ANY]}


@pytest.mark.parametrize("status", ["pending_owner", "approved"])
async def test_round2_cancel_releases_the_hook_reads_the_commitment_consistently_by_key(
    stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock, status: str
) -> None:
    """With the task's labels the hook never queries the eventually consistent task index: it reads
    the commitment by primary key (ConsistentRead) and releases the holds in one transaction. An
    `approved` commitment behind a cancelled task was never delivered, so it is withdrawn too."""
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    current = make(status, version=1, expires_at=None if status == "approved" else NOW + timedelta(hours=48))
    stub.add_response("get_item", {"Item": commitment_item(current)}, get_item_params())  # the hook's read
    stub.add_response("get_item", {"Item": commitment_item(current)}, get_item_params())  # update's own check
    stub.add_response("transact_write_items", {}, withdraw_transaction(current))
    await owner_over(store, clock).on_task_cancelled(cancelled_task({"business_id": "thimbleberry-bakehouse", "commitment_id": "c_abc"}))


async def test_round2_cancel_releases_a_lagging_index_only_names_the_commitment(
    stubbed_dynamodb: tuple[Any, Stubber], clock: FixedClock
) -> None:
    """A task without labels (created before them) falls back to the GSI, whose copy may be stale; the
    write is made against the consistent primary-key read instead (version 2, not the index's 0)."""
    client, stub = stubbed_dynamodb
    store = DynamoCommitmentStore(client, TABLE, clock=clock)
    stale = make()
    current = make("info_requested", version=2)
    stub.add_response(
        "query",
        {"Items": [commitment_item(stale)]},
        {
            "TableName": TABLE,
            "IndexName": "gsi2",
            "KeyConditionExpression": "gsi2pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": "TASK#t_xyz"}},
            "Limit": 1,
        },
    )
    stub.add_response("get_item", {"Item": commitment_item(current)}, get_item_params())
    stub.add_response("get_item", {"Item": commitment_item(current)}, get_item_params())
    stub.add_response("transact_write_items", {}, withdraw_transaction(current))
    await owner_over(store, clock).on_task_cancelled(cancelled_task({}))
