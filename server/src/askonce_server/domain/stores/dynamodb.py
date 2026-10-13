"""DynamoDB implementations of the domain ports on the shared single table.

Items (pk, sk are strings; `expires_at` is the table's TTL attribute, epoch seconds):

  Commitment   pk=BIZ#<business_id>   sk=C#<commitment_id>   (a spoken booking code is a `begins_with(sk)` query)
               attrs: doc (S, Commitment JSON), status (S), version (N), created_at (S),
                      gsi1pk=BIZ#<business_id>#S#<status>  gsi1sk=<created_at>#<commitment_id>   (inbox by status)
                      gsi2pk=TASK#<task_id>                 gsi2sk=C                              (get_by_task)
                      gsi3pk=OPEN#<yyyy-mm-dd of expires_at> gsi3sk=<expires_at iso>              (expiry sweep; only while open)
  Idempotency  pk=IDEM#<key>          sk=IDEM    attrs: commitment_id, business_id, expires_at
  Capacity     pk=CAP#<counter key>   sk=CAP     attrs: used (N), limit (N), expires_at (day after the slot)

`commit` = one TransactWriteItems:
  Put IDEM (attribute_not_exists(pk)) + Put commitment (attribute_not_exists(pk)) +
  per hold: Update CAP `SET #limit = :limit ADD used :u` with
            `(attribute_not_exists(used) AND :u <= :limit) OR used <= :max_before` (max_before = limit - units).
On TransactionCanceledException inspect CancellationReasons: index 0/1 ConditionalCheckFailed ->
Duplicate (load the existing commitment via the IDEM marker); a CAP index -> CapacityExhausted(key).

`update` = TransactWriteItems: Put commitment (`version = :expected`) [+ Update CAP `ADD used :minus`
for each hold when entering a hold-releasing status], so holds are released exactly once.

The boto3 client is injected; tests use botocore Stubber (no AWS calls).

Implementation notes:
  * Counter keys produced by `domain.availability` already start with `CAP#`; they are the pk as is.
  * Attribute names `used`, `limit`, `status` and `version` are DynamoDB reserved words, so every
    expression goes through `ExpressionAttributeNames`.
  * Timestamps in sort keys use a fixed-width UTC format (`2026-10-14T16:00:00.000000Z`) so string
    order is time order.
  * GSIs are expected to project ALL attributes (infra creates them; the doc is read from the index).
  * `update` reads the stored item first (strongly consistent) so that, like the memory store, it can
    reject a disallowed transition with `InvalidState` and release exactly the stored holds; the
    conditional write on `version` keeps it a true compare-and-swap.
  * Identical concurrent commits can cancel each other with `TransactionConflict`; those are retried a
    few times, after which the loser sees `Duplicate`.
"""

from __future__ import annotations

import base64
import functools
import json
import re
from collections.abc import Callable, Collection, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, get_args

import anyio
from botocore.exceptions import ClientError

from askonce_server.clock import Clock
from askonce_server.domain.models import (
    OPEN_STATUSES,
    RELEASES_HOLDS,
    CapacityHold,
    Commitment,
    CommitmentStatus,
    IdempotencyRecord,
    can_transition,
)
from askonce_server.domain.ports import CapacityExhausted, CommitResult, Committed, Duplicate
from askonce_server.domain.stores.memory import merge_holds
from askonce_server.errors import ConcurrencyConflict, InvalidState, NotFound

ALL_STATUSES: tuple[str, ...] = tuple(get_args(CommitmentStatus))
GSI_INBOX = "gsi1"
GSI_TASK = "gsi2"
GSI_EXPIRY = "gsi3"
CAP_SK = "CAP"
IDEM_SK = "IDEM"
MAX_TRANSACT_ITEMS = 100
BATCH_GET_LIMIT = 100

_CONDITION_FAILED = "ConditionalCheckFailed"
_TRANSACTION_CONFLICT = "TransactionConflict"
_DATE_IN_KEY = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ---------------------------------------------------------------------------- encoding helpers


def _s(value: str) -> dict[str, str]:
    return {"S": value}


def _n(value: int) -> dict[str, str]:
    return {"N": str(int(value))}


def sortable_utc(moment: datetime) -> str:
    """Fixed-width UTC timestamp whose string order is time order."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def epoch_seconds(moment: datetime) -> int:
    return int(moment.astimezone(UTC).timestamp())


def cap_pk(key: str) -> str:
    """Partition key of a capacity counter (keys from `domain.availability` already carry `CAP#`)."""
    return key if key.startswith("CAP#") else f"CAP#{key}"


def counter_expiry(key: str) -> int | None:
    """TTL for a counter: two days after the date in its key (after the slot in any timezone)."""
    found = _DATE_IN_KEY.findall(key)
    if not found:
        return None
    try:
        day = date.fromisoformat(found[-1])
    except ValueError:
        return None
    return epoch_seconds(datetime.combine(day + timedelta(days=2), time(), tzinfo=UTC))


def commitment_pk(business_id: str) -> str:
    return f"BIZ#{business_id}"


def commitment_sk(commitment_id: str) -> str:
    return f"C#{commitment_id}"


def commitment_item(commitment: Commitment) -> dict[str, Any]:
    """The single-table item for a commitment, with the GSI attributes its state calls for."""
    created = sortable_utc(commitment.created_at)
    item: dict[str, Any] = {
        "pk": _s(commitment_pk(commitment.business_id)),
        "sk": _s(commitment_sk(commitment.commitment_id)),
        "doc": _s(commitment.model_dump_json()),
        "status": _s(commitment.status),
        "version": _n(commitment.version),
        "created_at": _s(created),
        "gsi1pk": _s(f"BIZ#{commitment.business_id}#S#{commitment.status}"),
        "gsi1sk": _s(f"{created}#{commitment.commitment_id}"),
    }
    if commitment.task_id:
        item["gsi2pk"] = _s(f"TASK#{commitment.task_id}")
        item["gsi2sk"] = _s("C")
    if commitment.status in OPEN_STATUSES and commitment.expires_at is not None:
        item["gsi3pk"] = _s(f"OPEN#{commitment.expires_at.astimezone(UTC).date().isoformat()}")
        item["gsi3sk"] = _s(sortable_utc(commitment.expires_at))
    return item


def parse_commitment(item: dict[str, Any]) -> Commitment:
    return Commitment.model_validate_json(item["doc"]["S"])


def _encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"before": value}).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> str:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return str(json.loads(base64.urlsafe_b64decode(padded.encode()))["before"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc


def _reasons(exc: ClientError) -> list[str]:
    return [str(reason.get("Code") or "None") for reason in exc.response.get("CancellationReasons", []) or []]


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class _DynamoBase:
    def __init__(self, client: Any, table: str) -> None:
        if not table:
            raise ValueError("a table name is required")
        self._client = client
        self._table = table

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        operation: Callable[..., dict[str, Any]] = getattr(self._client, method)
        return await anyio.to_thread.run_sync(functools.partial(operation, **kwargs))


# ---------------------------------------------------------------------------- commitments


class DynamoCommitmentStore(_DynamoBase):
    def __init__(
        self,
        client: Any,
        table: str,
        *,
        clock: Clock,
        expiry_lookback_days: int = 7,
        conflict_retries: int = 3,
    ) -> None:
        super().__init__(client, table)
        self._clock = clock
        self._lookback = expiry_lookback_days
        self._retries = conflict_retries

    async def commit(
        self, commitment: Commitment, *, holds: Sequence[CapacityHold], idempotency: IdempotencyRecord
    ) -> CommitResult:
        merged = merge_holds(holds)
        keys = list(merged)
        items: list[dict[str, Any]] = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        "pk": _s(f"IDEM#{idempotency.key}"),
                        "sk": _s(IDEM_SK),
                        "commitment_id": _s(idempotency.commitment_id),
                        "business_id": _s(idempotency.business_id),
                        "created_at": _s(sortable_utc(idempotency.created_at)),
                        "expires_at": _n(epoch_seconds(idempotency.expires_at)),
                    },
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
            {
                "Put": {
                    "TableName": self._table,
                    "Item": commitment_item(commitment),
                    "ConditionExpression": "attribute_not_exists(pk)",
                }
            },
        ]
        for key in keys:
            units, limit = merged[key]
            items.append({"Update": self._hold_update(key, units, limit)})
        if len(items) > MAX_TRANSACT_ITEMS:
            raise ValueError(f"a commitment may take at most {MAX_TRANSACT_ITEMS - 2} capacity holds")

        for attempt in range(self._retries + 1):
            try:
                await self._call("transact_write_items", TransactItems=items)
                return Committed(commitment)
            except ClientError as exc:
                if _error_code(exc) != "TransactionCanceledException":
                    raise
                reasons = _reasons(exc)
            if _CONDITION_FAILED in reasons[:2]:
                existing = await self._existing(commitment, idempotency)
                if existing is not None:
                    return Duplicate(existing)
                raise ConcurrencyConflict("the request is being committed concurrently; retry")
            for index, reason in enumerate(reasons[2:]):
                if reason == _CONDITION_FAILED:
                    return CapacityExhausted(keys[index])
            if _TRANSACTION_CONFLICT in reasons and attempt < self._retries:
                await anyio.sleep(0.05 * (attempt + 1))
                continue
            break
        raise ConcurrencyConflict("the commit could not be completed; retry")

    def _hold_update(self, key: str, units: int, limit: int) -> dict[str, Any]:
        expiry = counter_expiry(key)
        values: dict[str, Any] = {":units": _n(units), ":limit": _n(limit), ":max_before": _n(limit - units)}
        names = {"#used": "used", "#limit": "limit"}
        update = "SET #limit = :limit ADD #used :units"
        if expiry is not None:
            update = "SET #limit = :limit, #exp = if_not_exists(#exp, :exp) ADD #used :units"
            values[":exp"] = _n(expiry)
            names["#exp"] = "expires_at"
        return {
            "TableName": self._table,
            "Key": {"pk": _s(cap_pk(key)), "sk": _s(CAP_SK)},
            "UpdateExpression": update,
            "ConditionExpression": "(attribute_not_exists(#used) AND :units <= :limit) OR #used <= :max_before",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }

    async def _existing(self, commitment: Commitment, idempotency: IdempotencyRecord) -> Commitment | None:
        found = await self.get(commitment.business_id, commitment.commitment_id)
        if found is not None:
            return found
        marker = await self._call(
            "get_item",
            TableName=self._table,
            Key={"pk": _s(f"IDEM#{idempotency.key}"), "sk": _s(IDEM_SK)},
            ConsistentRead=True,
        )
        item = marker.get("Item")
        if not item:
            return None
        return await self.get(item["business_id"]["S"], item["commitment_id"]["S"])

    async def get(self, business_id: str, commitment_id: str) -> Commitment | None:
        response = await self._call(
            "get_item",
            TableName=self._table,
            Key={"pk": _s(commitment_pk(business_id)), "sk": _s(commitment_sk(commitment_id))},
            ConsistentRead=True,
        )
        item = response.get("Item")
        return parse_commitment(item) if item else None

    async def find_by_reference_prefix(self, business_id: str, prefix: str, *, limit: int = 5) -> list[Commitment]:
        """A key-prefix query on the base table (`begins_with(sk, C#<prefix>)`): no index, no new attribute."""
        response = await self._call(
            "query",
            TableName=self._table,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :sk)",
            ExpressionAttributeValues={":pk": _s(commitment_pk(business_id)), ":sk": _s(commitment_sk(prefix))},
            ConsistentRead=True,
            Limit=limit,
        )
        return [parse_commitment(item) for item in response.get("Items") or []]

    async def get_by_task(self, task_id: str) -> Commitment | None:
        response = await self._call(
            "query",
            TableName=self._table,
            IndexName=GSI_TASK,
            KeyConditionExpression="gsi2pk = :pk",
            ExpressionAttributeValues={":pk": _s(f"TASK#{task_id}")},
            Limit=1,
        )
        items = response.get("Items") or []
        return parse_commitment(items[0]) if items else None

    async def update(self, commitment: Commitment, *, expected_version: int) -> Commitment:
        current = await self.get(commitment.business_id, commitment.commitment_id)
        if current is None:
            raise NotFound("unknown commitment")
        if current.version != expected_version:
            raise ConcurrencyConflict("the commitment changed; reload and retry")
        if commitment.status != current.status and not can_transition(current.status, commitment.status):
            raise InvalidState(f"cannot go from {current.status} to {commitment.status}")
        release = commitment.status in RELEASES_HOLDS and not current.holds_released and bool(current.holds)
        stored = commitment.model_copy(
            update={
                "version": expected_version + 1,
                "holds": current.holds,
                "holds_released": current.holds_released or release,
            }
        )
        put = {
            "TableName": self._table,
            "Item": commitment_item(stored),
            "ConditionExpression": "#version = :expected",
            "ExpressionAttributeNames": {"#version": "version"},
            "ExpressionAttributeValues": {":expected": _n(expected_version)},
        }
        try:
            if not release:
                await self._call("put_item", **put)
                return stored
            items: list[dict[str, Any]] = [{"Put": put}]
            for key, (units, _) in merge_holds(current.holds).items():
                expiry = counter_expiry(key)
                values: dict[str, Any] = {":minus": _n(-units)}
                names = {"#used": "used"}
                expression = "ADD #used :minus"
                if expiry is not None:
                    expression = "SET #exp = if_not_exists(#exp, :exp) ADD #used :minus"
                    values[":exp"] = _n(expiry)
                    names["#exp"] = "expires_at"
                items.append(
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": {"pk": _s(cap_pk(key)), "sk": _s(CAP_SK)},
                            "UpdateExpression": expression,
                            "ExpressionAttributeNames": names,
                            "ExpressionAttributeValues": values,
                        }
                    }
                )
            await self._call("transact_write_items", TransactItems=items)
            return stored
        except ClientError as exc:
            code = _error_code(exc)
            if code == "ConditionalCheckFailedException" or (
                code == "TransactionCanceledException" and _reasons(exc)[:1] == [_CONDITION_FAILED]
            ):
                raise ConcurrencyConflict("the commitment changed; reload and retry") from exc
            if code == "TransactionCanceledException":
                raise ConcurrencyConflict("the update could not be completed; retry") from exc
            raise

    async def list_for_business(
        self,
        business_id: str,
        *,
        statuses: Collection[CommitmentStatus] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Commitment], str | None]:
        wanted: list[str] = sorted(set(statuses)) if statuses is not None else list(ALL_STATUSES)
        before = _decode_cursor(cursor) if cursor else None
        rows: list[tuple[str, dict[str, Any]]] = []
        more = False
        for status in wanted:
            values: dict[str, Any] = {":pk": _s(f"BIZ#{business_id}#S#{status}")}
            condition = "gsi1pk = :pk"
            if before is not None:
                condition += " AND gsi1sk < :before"
                values[":before"] = _s(before)
            response = await self._call(
                "query",
                TableName=self._table,
                IndexName=GSI_INBOX,
                KeyConditionExpression=condition,
                ExpressionAttributeValues=values,
                ScanIndexForward=False,
                Limit=limit,
            )
            rows.extend((item["gsi1sk"]["S"], item) for item in response.get("Items") or [])
            more = more or bool(response.get("LastEvaluatedKey"))
        rows.sort(key=lambda row: row[0], reverse=True)
        page = rows[:limit]
        next_cursor = _encode_cursor(page[-1][0]) if page and (len(rows) > limit or more) else None
        return [parse_commitment(item) for _, item in page], next_cursor

    async def list_expiring(self, *, before: datetime, limit: int = 100) -> list[Commitment]:
        out: list[Commitment] = []
        last_day = before.astimezone(UTC).date()
        for offset in range(self._lookback, -1, -1):
            day = last_day - timedelta(days=offset)
            start_key: dict[str, Any] | None = None
            while len(out) < limit:
                params: dict[str, Any] = {
                    "TableName": self._table,
                    "IndexName": GSI_EXPIRY,
                    "KeyConditionExpression": "gsi3pk = :pk AND gsi3sk < :before",
                    "ExpressionAttributeValues": {":pk": _s(f"OPEN#{day.isoformat()}"), ":before": _s(sortable_utc(before))},
                    "ScanIndexForward": True,
                    "Limit": limit - len(out),
                }
                if start_key:
                    params["ExclusiveStartKey"] = start_key
                response = await self._call("query", **params)
                for item in response.get("Items") or []:
                    commitment = parse_commitment(item)
                    if commitment.status in OPEN_STATUSES:
                        out.append(commitment)
                start_key = response.get("LastEvaluatedKey")
                if not start_key:
                    break
            if len(out) >= limit:
                break
        return out[:limit]


# ---------------------------------------------------------------------------- capacity & idempotency


class DynamoCapacityStore(_DynamoBase):
    def __init__(self, client: Any, table: str, *, max_attempts: int = 5) -> None:
        super().__init__(client, table)
        self._max_attempts = max_attempts

    async def usage(self, keys: Sequence[str]) -> dict[str, int]:
        """BatchGetItem (chunks of 100, retrying UnprocessedKeys)."""
        unique = list(dict.fromkeys(keys))
        result = {key: 0 for key in unique}
        by_pk = {cap_pk(key): key for key in unique}
        for start in range(0, len(unique), BATCH_GET_LIMIT):
            chunk = unique[start : start + BATCH_GET_LIMIT]
            request: dict[str, Any] = {
                self._table: {
                    "Keys": [{"pk": _s(cap_pk(key)), "sk": _s(CAP_SK)} for key in chunk],
                    "ProjectionExpression": "pk, #used",
                    "ExpressionAttributeNames": {"#used": "used"},
                    "ConsistentRead": True,
                }
            }
            for attempt in range(self._max_attempts):
                response = await self._call("batch_get_item", RequestItems=request)
                for item in (response.get("Responses") or {}).get(self._table, []):
                    key = by_pk.get(item["pk"]["S"])
                    if key is not None:
                        result[key] = int(item["used"]["N"]) if "used" in item else 0
                request = response.get("UnprocessedKeys") or {}
                if not request:
                    break
                await anyio.sleep(0.05 * (2**attempt))
            else:
                raise ConcurrencyConflict("capacity read was throttled; retry")
        return result


class DynamoIdempotencyStore(_DynamoBase):
    def __init__(self, client: Any, table: str, *, clock: Clock) -> None:
        super().__init__(client, table)
        self._clock = clock

    async def get(self, key: str) -> IdempotencyRecord | None:
        response = await self._call(
            "get_item",
            TableName=self._table,
            Key={"pk": _s(f"IDEM#{key}"), "sk": _s(IDEM_SK)},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            return None
        expires = datetime.fromtimestamp(int(item["expires_at"]["N"]), tz=UTC)
        if expires <= self._clock.now():
            return None
        created = datetime.strptime(item["created_at"]["S"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        return IdempotencyRecord(
            key=key,
            business_id=item["business_id"]["S"],
            commitment_id=item["commitment_id"]["S"],
            created_at=created,
            expires_at=expires,
        )
