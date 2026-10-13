"""DynamoDB `ProfileStore` on the shared single table (see docs/design/server-architecture.md, Storage).

Items:
  pk=PROFILE#<business_id>  sk=CURRENT          doc (S, raw JSON), revision (N), status (S), sha256 (S)
  pk=PROFILE#<business_id>  sk=REV#<00000006>   doc snapshot of each revision (audit, optional)
  pk=DIRECTORY              sk=BUSINESS#<id>    entry (S, DirectoryEntry JSON), only while live

`put` is one TransactWriteItems: conditional Put of CURRENT (`attribute_not_exists(pk)` or
`revision = :expected`) + Put/Delete of the directory item. The boto3 client is injected (no AWS calls
in tests: use botocore Stubber). Profiles are re-validated by the loader on read (cheap, and a corrupted
item can never reach a tool).

The transaction also writes the immutable `REV#` audit copy (condition `attribute_not_exists(pk)`), and
carries a `ClientRequestToken` derived from (business, revision, sha256), so a retried put of the same
document is idempotent instead of failing its own condition. Every client call runs in a worker thread
(`anyio.to_thread.run_sync`): boto3 is synchronous.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import anyio
from botocore.exceptions import ClientError
from pydantic import ValidationError

from askonce_server import errors
from askonce_server.profile.directory import DirectoryEntry, build_directory_entry
from askonce_server.profile.loader import load_profile
from askonce_server.profile.model import Profile
from askonce_server.profile.snapshots import SnapshotProvider
from askonce_server.profile.store import unknown_business

logger = logging.getLogger(__name__)

CURRENT_SK = "CURRENT"
DIRECTORY_PK = "DIRECTORY"
MAX_DOC_BYTES = 350_000
"""DynamoDB items are capped at 400 KB; keep headroom for the other attributes."""

CONFLICT_REASONS: frozenset[str] = frozenset({"ConditionalCheckFailed", "TransactionConflict"})
CONFLICT_CODES: frozenset[str] = frozenset({"ConditionalCheckFailedException", "TransactionConflictException"})


def profile_pk(business_id: str) -> str:
    return f"PROFILE#{business_id}"


def revision_sk(revision: int) -> str:
    return f"REV#{revision:08d}"


def directory_sk(business_id: str) -> str:
    return f"BUSINESS#{business_id}"


def profile_key(business_id: str) -> dict[str, dict[str, str]]:
    """Key of the CURRENT item."""
    return {"pk": {"S": profile_pk(business_id)}, "sk": {"S": CURRENT_SK}}


def directory_key(business_id: str) -> dict[str, dict[str, str]]:
    return {"pk": {"S": DIRECTORY_PK}, "sk": {"S": directory_sk(business_id)}}


def encode_doc(profile: Profile) -> str:
    """Compact JSON of the whole document (what `doc` stores)."""
    return json.dumps(profile.raw, ensure_ascii=False, separators=(",", ":"))


def request_token(profile: Profile) -> str:
    """Deterministic `ClientRequestToken` (36 chars max) for putting exactly this document."""
    seed = f"{profile.profile_id}|{profile.revision}|{profile.sha256}".encode()
    return hashlib.sha256(seed).hexdigest()[:36]


def conflict_from(exc: ClientError) -> errors.ConcurrencyConflict | None:
    """Map a lost conditional write or transaction race to `ConcurrencyConflict` (else None)."""
    response: dict[str, Any] = exc.response  # type: ignore[assignment]
    error = response.get("Error") or {}
    code = error.get("Code")
    if code in CONFLICT_CODES:
        return errors.ConcurrencyConflict("Profile write lost a race")
    if code != "TransactionCanceledException":
        return None
    reasons = [r.get("Code") for r in response.get("CancellationReasons") or () if isinstance(r, dict)]
    message = str(error.get("Message") or "")
    if any(reason in CONFLICT_REASONS for reason in reasons) or (
        not reasons and any(reason in message for reason in CONFLICT_REASONS)
    ):
        return errors.ConcurrencyConflict("Profile write lost a race (revision changed or concurrent write)")
    return None


class DynamoProfileStore:
    """`ProfileStore` on the single table. See the module docstring for the item layout."""

    def __init__(
        self,
        client: Any,
        table: str,
        *,
        schema_path: Path,
        snapshots: SnapshotProvider | None = None,
        strict: bool = True,
    ) -> None:
        self._client = client
        self._table = table
        self._schema_path = schema_path
        self._snapshots = snapshots
        self._strict = strict

    async def _call(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        method = getattr(self._client, operation)
        result: dict[str, Any] = await anyio.to_thread.run_sync(functools.partial(method, **kwargs))
        return result

    # ---- reads -----------------------------------------------------------------------------------------

    async def get(self, business_id: str) -> Profile | None:
        """The CURRENT profile (strongly consistent), re-validated. Raises `errors.ProfileInvalid`."""
        response = await self._call(
            "get_item", TableName=self._table, Key=profile_key(business_id), ConsistentRead=True
        )
        item = response.get("Item")
        if not item:
            return None
        return self._decode(business_id, item)

    def _decode(self, business_id: str, item: dict[str, Any]) -> Profile:
        try:
            doc = json.loads(item["doc"]["S"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise errors.ProfileInvalid(f"Stored profile {business_id!r} has no readable doc") from exc
        loaded = load_profile(doc, schema_path=self._schema_path, snapshots=self._snapshots, strict=self._strict)
        profile = loaded.profile
        if profile.profile_id != business_id:
            raise errors.ProfileInvalid(f"Item {profile_pk(business_id)!r} holds profile {profile.profile_id!r}")
        stored_sha = (item.get("sha256") or {}).get("S")
        if stored_sha and stored_sha != profile.sha256:
            logger.warning("Stored sha256 of profile %r does not match its document (edited out of band?)", business_id)
        return profile

    async def get_live(self, business_id: str) -> Profile:
        try:
            profile = await self.get(business_id)
        except errors.ProfileInvalid as exc:
            logger.error("Stored profile %r failed validation; hiding it: %s", business_id, exc)
            raise unknown_business(business_id) from exc
        if profile is None or not profile.is_live:
            raise unknown_business(business_id)
        return profile

    async def list_live(self) -> Sequence[DirectoryEntry]:
        """Every directory entry (paginated query on `pk=DIRECTORY`), sorted by business id."""
        entries: list[DirectoryEntry] = []
        request: dict[str, Any] = {
            "TableName": self._table,
            "KeyConditionExpression": "pk = :pk",
            "ExpressionAttributeValues": {":pk": {"S": DIRECTORY_PK}},
            "ConsistentRead": True,
        }
        while True:
            response = await self._call("query", **request)
            for item in response.get("Items") or ():
                try:
                    entries.append(DirectoryEntry.model_validate_json(item["entry"]["S"]))
                except (KeyError, TypeError, ValidationError) as exc:
                    logger.warning("Skipping unreadable directory item %s: %s", item.get("sk"), exc)
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            request["ExclusiveStartKey"] = last_key
        return sorted(entries, key=lambda entry: entry.business_id)

    # ---- writes ----------------------------------------------------------------------------------------

    def transact_items(self, profile: Profile, *, expected_revision: int | None) -> list[dict[str, Any]]:
        """The TransactWriteItems entries for one `put` (exposed for tests and tooling)."""
        doc = encode_doc(profile)
        if len(doc.encode()) > MAX_DOC_BYTES:
            raise ValueError(f"Profile {profile.profile_id!r} is too large for one DynamoDB item")
        pk = {"S": profile_pk(profile.profile_id)}
        attributes = {
            "doc": {"S": doc},
            "revision": {"N": str(profile.revision)},
            "status": {"S": profile.status},
            "sha256": {"S": profile.sha256},
        }
        current: dict[str, Any] = {"TableName": self._table, "Item": {"pk": pk, "sk": {"S": CURRENT_SK}, **attributes}}
        if expected_revision is None:
            current["ConditionExpression"] = "attribute_not_exists(pk)"
        else:
            current["ConditionExpression"] = "#revision = :expected"
            current["ExpressionAttributeNames"] = {"#revision": "revision"}
            current["ExpressionAttributeValues"] = {":expected": {"N": str(expected_revision)}}
        history = {
            "TableName": self._table,
            "Item": {"pk": pk, "sk": {"S": revision_sk(profile.revision)}, **attributes},
            "ConditionExpression": "attribute_not_exists(pk)",
        }
        if profile.is_live:
            entry = build_directory_entry(profile)
            directory: dict[str, Any] = {
                "Put": {
                    "TableName": self._table,
                    "Item": {
                        **directory_key(profile.profile_id),
                        "entry": {"S": entry.model_dump_json()},
                        "revision": {"N": str(profile.revision)},
                    },
                }
            }
        else:
            directory = {"Delete": {"TableName": self._table, "Key": directory_key(profile.profile_id)}}
        return [{"Put": current}, {"Put": history}, directory]

    async def put(self, profile: Profile, *, expected_revision: int | None) -> None:
        if expected_revision is not None and profile.revision <= expected_revision:
            raise errors.ConcurrencyConflict(
                f"Profile {profile.profile_id!r} revision {profile.revision} does not advance past {expected_revision}"
            )
        items = self.transact_items(profile, expected_revision=expected_revision)
        try:
            await self._call("transact_write_items", TransactItems=items, ClientRequestToken=request_token(profile))
        except ClientError as exc:
            conflict = conflict_from(exc)
            if conflict is not None:
                raise errors.ConcurrencyConflict(
                    f"Profile {profile.profile_id!r}: expected revision {expected_revision} no longer holds"
                ) from exc
            raise
