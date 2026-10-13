"""DynamoDB-backed `TaskStore`.

Table design (one table, on-demand billing; `table_definition()` / `ttl_definition()` build the
`CreateTable` / `UpdateTimeToLive` requests)::

    PK   taskId (S)                      key_prefix + task id
    attrs principal (S), createdAt (S, fixed-width UTC timestamp, sorts chronologically),
          status (S), version (N), doc (S, the full JSON record),
          expiresAt (N, epoch seconds; enable DynamoDB TTL on it)
    GSI  byPrincipal: PK principal, SK createdAt (projection ALL), used by tasks/list

Every write is conditional: `put_new` on `attribute_not_exists(<key>)`, `replace` on the stored
`version` (compare-and-swap), so concurrent writers on different replicas never lose updates.
`get` is a strongly consistent read. `list_for` queries the GSI, which is eventually consistent.

The boto3 client is injected (sync API; calls run in a worker thread), so this module does not import
boto3 and tests can pass a client wrapped in `botocore.stub.Stubber`. Key attribute names are
configurable so the store can share a single-table design with other entities; give it a dedicated
principal index there, since every item in that index must be a task document.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
from dataclasses import asdict, fields
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast

import anyio.to_thread

from mcp_tasks.models import TaskRecord
from mcp_tasks.store import DuplicateTask

_RECORD_FIELDS = frozenset(f.name for f in fields(TaskRecord))


def record_to_doc(record: TaskRecord) -> str:
    """Serialise a record to its compact JSON document."""
    data = asdict(record)
    data["created_at"] = record.created_at.isoformat()
    data["last_updated_at"] = record.last_updated_at.isoformat()
    return json.dumps(data, separators=(",", ":"), sort_keys=True)


def record_from_doc(doc: str) -> TaskRecord:
    """Inverse of `record_to_doc`. Unknown keys (written by a newer version) are ignored."""
    data = {k: v for k, v in json.loads(doc).items() if k in _RECORD_FIELDS}
    data["created_at"] = datetime.fromisoformat(data["created_at"])
    data["last_updated_at"] = datetime.fromisoformat(data["last_updated_at"])
    return TaskRecord(**data)


def sort_key_timestamp(dt: datetime) -> str:
    """Fixed-width UTC timestamp (microseconds, `Z`) so the GSI sort key orders chronologically."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def table_definition(table: str, *, principal_index: str = "byPrincipal", key_attr: str = "taskId") -> dict[str, Any]:
    """`CreateTable` request for a dedicated task table (on-demand billing).

    Pass it as `client.create_table(**table_definition("my-tasks"))`, then enable TTL with
    `client.update_time_to_live(**ttl_definition("my-tasks"))`.
    """
    return {
        "TableName": table,
        "AttributeDefinitions": [
            {"AttributeName": key_attr, "AttributeType": "S"},
            {"AttributeName": "principal", "AttributeType": "S"},
            {"AttributeName": "createdAt", "AttributeType": "S"},
        ],
        "KeySchema": [{"AttributeName": key_attr, "KeyType": "HASH"}],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": principal_index,
                "KeySchema": [
                    {"AttributeName": "principal", "KeyType": "HASH"},
                    {"AttributeName": "createdAt", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        "BillingMode": "PAY_PER_REQUEST",
    }


def ttl_definition(table: str) -> dict[str, Any]:
    """`UpdateTimeToLive` request that lets DynamoDB garbage-collect expired tasks via `expiresAt`."""
    return {"TableName": table, "TimeToLiveSpecification": {"Enabled": True, "AttributeName": "expiresAt"}}


class DynamoTaskStore:
    """`TaskStore` over a DynamoDB table (see module docstring for the table layout)."""

    def __init__(
        self,
        client: Any,
        table: str,
        *,
        principal_index: str = "byPrincipal",
        key_attr: str = "taskId",
        key_prefix: str = "",
    ) -> None:
        """`client` is a `boto3.client("dynamodb")`. `key_prefix` namespaces ids in a shared table."""
        self._ddb = client
        self._table = table
        self._index = principal_index
        self._key_attr = key_attr
        self._prefix = key_prefix

    async def _call(self, op: str, **kwargs: Any) -> dict[str, Any]:
        result: dict[str, Any] = await anyio.to_thread.run_sync(partial(getattr(self._ddb, op), **kwargs))
        return result

    def _key(self, task_id: str) -> dict[str, Any]:
        return {self._key_attr: {"S": f"{self._prefix}{task_id}"}}

    def item_for(self, record: TaskRecord) -> dict[str, Any]:
        """The DynamoDB item written for `record` (public for tests and stubs)."""
        item: dict[str, Any] = {
            **self._key(record.task_id),
            "principal": {"S": record.principal},
            "createdAt": {"S": sort_key_timestamp(record.created_at)},
            "status": {"S": record.status},
            "version": {"N": str(record.version)},
            "doc": {"S": record_to_doc(record)},
        }
        expires = record.expires_at()
        if expires is not None:
            # Rounded up: DynamoDB may delete an item any time after expiresAt, never before expiry.
            item["expiresAt"] = {"N": str(math.ceil(expires.timestamp()))}
        return item

    async def put_new(self, record: TaskRecord) -> None:
        try:
            await self._call(
                "put_item",
                TableName=self._table,
                Item=self.item_for(record),
                ConditionExpression="attribute_not_exists(#k)",
                ExpressionAttributeNames={"#k": self._key_attr},
            )
        except self._ddb.exceptions.ConditionalCheckFailedException:
            raise DuplicateTask(record.task_id) from None

    async def get(self, task_id: str) -> TaskRecord | None:
        out = await self._call("get_item", TableName=self._table, Key=self._key(task_id), ConsistentRead=True)
        item = out.get("Item")
        return record_from_doc(item["doc"]["S"]) if item else None

    async def replace(self, record: TaskRecord, *, expected_version: int) -> bool:
        try:
            await self._call(
                "put_item",
                TableName=self._table,
                Item=self.item_for(record),
                ConditionExpression="#v = :expected",
                ExpressionAttributeNames={"#v": "version"},
                ExpressionAttributeValues={":expected": {"N": str(expected_version)}},
            )
            return True
        except self._ddb.exceptions.ConditionalCheckFailedException:
            return False

    async def list_for(self, principal: str, *, cursor: str | None, limit: int) -> tuple[list[TaskRecord], str | None]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        kwargs: dict[str, Any] = {
            "TableName": self._table,
            "IndexName": self._index,
            "KeyConditionExpression": "#p = :p",
            "ExpressionAttributeNames": {"#p": "principal"},
            "ExpressionAttributeValues": {":p": {"S": principal}},
            "ScanIndexForward": True,
            "Limit": limit,
        }
        if cursor:
            kwargs["ExclusiveStartKey"] = _decode_cursor(cursor, principal)
        out = await self._call("query", **kwargs)
        rows = [record_from_doc(i["doc"]["S"]) for i in out.get("Items", [])]
        last = out.get("LastEvaluatedKey")
        return rows, (_encode_cursor(last) if last else None)


def _encode_cursor(last_evaluated_key: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(last_evaluated_key, separators=(",", ":")).encode()).decode()


def _decode_cursor(cursor: str, principal: str) -> dict[str, Any]:
    """Decode a `list_for` cursor; `ValueError` if it is malformed or belongs to another principal."""
    try:
        decoded: Any = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (binascii.Error, UnicodeError, ValueError):
        raise ValueError("invalid cursor") from None
    if not isinstance(decoded, dict):
        raise ValueError("invalid cursor")
    key = cast(dict[str, Any], decoded)
    if key.get("principal") != {"S": principal}:
        raise ValueError("invalid cursor")
    return key
