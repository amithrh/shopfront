"""`profile.dynamodb.DynamoProfileStore` against botocore Stubber (never AWS).

Asserts the exact request shapes of the single-table layout (GetItem with ConsistentRead, the
TransactWriteItems of `put`, the paginated directory Query) and the `CancellationReasons` mapping.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber
from support import SCHEMA_PATH, make_live

from askonce_server import errors
from askonce_server.profile.directory import DirectoryEntry, build_directory_entry
from askonce_server.profile.dynamodb import (
    DynamoProfileStore,
    directory_key,
    profile_key,
    request_token,
)
from askonce_server.profile.model import Profile

pytestmark = pytest.mark.anyio

TABLE = "askonce"
CANCELLED = "Transaction cancelled, please refer cancellation reasons for specific reasons"


class Capture:
    """Matches anything in Stubber expected params and remembers what it matched."""

    def __init__(self) -> None:
        self.value: Any = None

    def __eq__(self, other: object) -> bool:
        self.value = other
        return True

    def __hash__(self) -> int:
        return 0


def _profile(doc: dict[str, Any], *, revision: int | None = None, live: bool = True) -> Profile:
    doc = json.loads(json.dumps(doc))
    doc["certification"] = {"status": "draft", "runs": []}
    if revision is not None:
        doc["revision"] = revision
    return Profile.unchecked(make_live(doc) if live else doc)


@pytest.fixture
def live(example_raw: dict[str, Any]) -> Profile:
    return _profile(example_raw)


@pytest.fixture
def ddb(stubbed_dynamodb: tuple[Any, Stubber]) -> tuple[DynamoProfileStore, Stubber]:
    client, stub = stubbed_dynamodb
    return DynamoProfileStore(client, TABLE, schema_path=SCHEMA_PATH, strict=True), stub


def _item(profile: Profile, *, doc: str | None = None) -> dict[str, Any]:
    return {
        **profile_key(profile.profile_id),
        "doc": {"S": doc if doc is not None else json.dumps(profile.raw)},
        "revision": {"N": str(profile.revision)},
        "status": {"S": profile.status},
        "sha256": {"S": profile.sha256},
    }


def _expect_get(stub: Stubber, business_id: str, item: dict[str, Any] | None) -> None:
    stub.add_response(
        "get_item",
        {"Item": item} if item is not None else {},
        {"TableName": TABLE, "Key": profile_key(business_id), "ConsistentRead": True},
    )


# --------------------------------------------------------------------------- reads


async def test_get_reads_current_consistently_and_revalidates(
    ddb: tuple[DynamoProfileStore, Stubber], live: Profile
) -> None:
    store, stub = ddb
    _expect_get(stub, "thimbleberry-bakehouse", _item(live))
    profile = await store.get("thimbleberry-bakehouse")
    assert profile is not None and profile.sha256 == live.sha256 and profile.is_live


async def test_get_missing_is_none_and_get_live_unknown(ddb: tuple[DynamoProfileStore, Stubber]) -> None:
    store, stub = ddb
    _expect_get(stub, "nobody", None)
    assert await store.get("nobody") is None
    _expect_get(stub, "nobody", None)
    with pytest.raises(errors.UnknownBusiness):
        await store.get_live("nobody")


async def test_get_live_hides_drafts(ddb: tuple[DynamoProfileStore, Stubber], example_raw: dict[str, Any]) -> None:
    store, stub = ddb
    draft = _profile(example_raw, live=False)
    _expect_get(stub, "thimbleberry-bakehouse", _item(draft))
    with pytest.raises(errors.UnknownBusiness):
        await store.get_live("thimbleberry-bakehouse")


async def test_corrupt_items_never_reach_a_tool(
    ddb: tuple[DynamoProfileStore, Stubber], live: Profile, caplog: pytest.LogCaptureFixture
) -> None:
    store, stub = ddb
    broken = live.to_dict()
    broken["modules"]["custom-cakes"]["approval"]["module"] = "ghost"  # L1
    _expect_get(stub, "thimbleberry-bakehouse", _item(live, doc=json.dumps(broken)))
    with pytest.raises(errors.ProfileInvalid):
        await store.get("thimbleberry-bakehouse")
    _expect_get(stub, "thimbleberry-bakehouse", _item(live, doc="{not json"))
    with pytest.raises(errors.ProfileInvalid):
        await store.get("thimbleberry-bakehouse")
    _expect_get(stub, "thimbleberry-bakehouse", _item(live, doc=json.dumps(broken)))
    with caplog.at_level(logging.ERROR), pytest.raises(errors.UnknownBusiness):
        await store.get_live("thimbleberry-bakehouse")
    assert "failed validation" in caplog.text


async def test_item_holding_another_tenant_is_invalid(ddb: tuple[DynamoProfileStore, Stubber], live: Profile) -> None:
    store, stub = ddb
    _expect_get(stub, "other-bakery", _item(live))
    with pytest.raises(errors.ProfileInvalid, match="holds profile"):
        await store.get("other-bakery")


async def test_sha_mismatch_is_logged(
    ddb: tuple[DynamoProfileStore, Stubber], live: Profile, caplog: pytest.LogCaptureFixture
) -> None:
    store, stub = ddb
    item = _item(live)
    item["sha256"] = {"S": "0" * 64}
    _expect_get(stub, "thimbleberry-bakehouse", item)
    with caplog.at_level(logging.WARNING):
        assert await store.get("thimbleberry-bakehouse") is not None
    assert "does not match" in caplog.text


async def test_list_live_paginates_and_sorts(
    ddb: tuple[DynamoProfileStore, Stubber], live: Profile, caplog: pytest.LogCaptureFixture
) -> None:
    store, stub = ddb
    entry = build_directory_entry(live)
    zeta = entry.model_copy(update={"business_id": "zeta"})
    alpha = entry.model_copy(update={"business_id": "alpha"})
    query = {
        "TableName": TABLE,
        "KeyConditionExpression": "pk = :pk",
        "ExpressionAttributeValues": {":pk": {"S": "DIRECTORY"}},
        "ConsistentRead": True,
    }
    page_key = {"pk": {"S": "DIRECTORY"}, "sk": {"S": "BUSINESS#zeta"}}
    stub.add_response(
        "query",
        {"Items": [{**directory_key("zeta"), "entry": {"S": zeta.model_dump_json()}}], "LastEvaluatedKey": page_key},
        query,
    )
    stub.add_response(
        "query",
        {
            "Items": [
                {**directory_key("alpha"), "entry": {"S": alpha.model_dump_json()}},
                {**directory_key("broken"), "entry": {"S": "{}"}},
            ]
        },
        {**query, "ExclusiveStartKey": page_key},
    )
    with caplog.at_level(logging.WARNING):
        entries = await store.list_live()
    assert [e.business_id for e in entries] == ["alpha", "zeta"]
    assert all(isinstance(e, DirectoryEntry) for e in entries)
    assert "unreadable directory item" in caplog.text


# --------------------------------------------------------------------------- writes


async def test_put_new_live_profile_transaction_shape(ddb: tuple[DynamoProfileStore, Stubber], live: Profile) -> None:
    store, stub = ddb
    doc, entry = Capture(), Capture()
    attributes = {
        "doc": {"S": doc},
        "revision": {"N": "6"},
        "status": {"S": "live"},
        "sha256": {"S": live.sha256},
    }
    expected = {
        "TransactItems": [
            {
                "Put": {
                    "TableName": TABLE,
                    "Item": {"pk": {"S": "PROFILE#thimbleberry-bakehouse"}, "sk": {"S": "CURRENT"}, **attributes},
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Put": {
                    "TableName": TABLE,
                    "Item": {"pk": {"S": "PROFILE#thimbleberry-bakehouse"}, "sk": {"S": "REV#00000006"}, **attributes},
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Put": {
                    "TableName": TABLE,
                    "Item": {
                        "pk": {"S": "DIRECTORY"},
                        "sk": {"S": "BUSINESS#thimbleberry-bakehouse"},
                        "entry": {"S": entry},
                        "revision": {"N": "6"},
                    },
                }
            },
        ],
        "ClientRequestToken": request_token(live),
    }
    stub.add_response("transact_write_items", {}, expected)
    await store.put(live, expected_revision=None)
    assert json.loads(doc.value) == live.raw
    assert DirectoryEntry.model_validate_json(entry.value) == build_directory_entry(live)


async def test_put_update_is_conditional_on_the_expected_revision(
    ddb: tuple[DynamoProfileStore, Stubber], example_raw: dict[str, Any]
) -> None:
    store, stub = ddb
    draft = _profile(example_raw, revision=7, live=False)
    expected = {
        "TransactItems": [
            {
                "Put": {
                    "TableName": TABLE,
                    "Item": ANY,
                    "ConditionExpression": "#revision = :expected",
                    "ExpressionAttributeNames": {"#revision": "revision"},
                    "ExpressionAttributeValues": {":expected": {"N": "6"}},
                }
            },
            {"Put": {"TableName": TABLE, "Item": ANY, "ConditionExpression": "attribute_not_exists(pk)"}},
            {"Delete": {"TableName": TABLE, "Key": directory_key("thimbleberry-bakehouse")}},  # no longer live
        ],
        "ClientRequestToken": ANY,
    }
    stub.add_response("transact_write_items", {}, expected)
    await store.put(draft, expected_revision=6)


def _cancelled(stub: Stubber, *reasons: str) -> None:
    stub.add_client_error(
        "transact_write_items",
        service_error_code="TransactionCanceledException",
        service_message=f"{CANCELLED} [{', '.join(reasons)}]",
        modeled_fields={"CancellationReasons": [{"Code": reason} for reason in reasons]},
    )


@pytest.mark.parametrize(
    "reasons",
    [
        ("ConditionalCheckFailed", "None", "None"),
        ("None", "ConditionalCheckFailed", "None"),
        ("TransactionConflict", "None", "None"),
    ],
)
async def test_lost_races_map_to_concurrency_conflict(
    ddb: tuple[DynamoProfileStore, Stubber], live: Profile, reasons: tuple[str, ...]
) -> None:
    store, stub = ddb
    _cancelled(stub, *reasons)
    with pytest.raises(errors.ConcurrencyConflict):
        await store.put(live, expected_revision=None)


async def test_other_cancellations_propagate(ddb: tuple[DynamoProfileStore, Stubber], live: Profile) -> None:
    store, stub = ddb
    _cancelled(stub, "None", "ValidationError", "None")
    with pytest.raises(ClientError) as excinfo:
        await store.put(live, expected_revision=None)
    assert not isinstance(excinfo.value, errors.ConcurrencyConflict)
    stub.add_client_error("transact_write_items", service_error_code="ProvisionedThroughputExceededException")
    with pytest.raises(ClientError):
        await store.put(live, expected_revision=None)


async def test_cancellation_without_reasons_falls_back_to_the_message(
    ddb: tuple[DynamoProfileStore, Stubber], live: Profile
) -> None:
    store, stub = ddb
    stub.add_client_error(
        "transact_write_items",
        service_error_code="TransactionCanceledException",
        service_message=f"{CANCELLED} [ConditionalCheckFailed]",
    )
    with pytest.raises(errors.ConcurrencyConflict):
        await store.put(live, expected_revision=None)


async def test_non_advancing_revision_never_calls_dynamodb(
    ddb: tuple[DynamoProfileStore, Stubber], live: Profile
) -> None:
    store, _ = ddb
    with pytest.raises(errors.ConcurrencyConflict):
        await store.put(live, expected_revision=live.revision)  # nothing queued: a call would fail


async def test_oversized_documents_are_refused_before_any_call(
    ddb: tuple[DynamoProfileStore, Stubber], example_raw: dict[str, Any]
) -> None:
    store, _ = ddb
    example_raw["x-blob"] = "a" * 400_000
    with pytest.raises(ValueError, match="too large"):
        await store.put(_profile(example_raw), expected_revision=None)


def test_request_token_is_deterministic_and_short(live: Profile, example_raw: dict[str, Any]) -> None:
    assert request_token(live) == request_token(Profile.unchecked(live.to_dict()))
    assert len(request_token(live)) <= 36
    assert request_token(live) != request_token(_profile(example_raw, revision=7))
