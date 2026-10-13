"""Ports (protocols) the domain depends on. Implementations live in `domain.stores` (memory, DynamoDB),
`app.notifier` (owner notification) and `tools.render` (task result rendering).

Atomicity contract (the heart of "no double booking, even across replicas"):

`CommitmentStore.commit(commitment, holds=..., idempotency=...)` is ONE all-or-nothing unit:
  1. put the idempotency marker            (condition: key absent)
  2. put the commitment                    (condition: commitment id absent)
  3. add `units` to every capacity counter (condition: counter + units <= limit)
DynamoDB: a single TransactWriteItems. Memory: one lock. The commitment id is derived from the
idempotency key (`IdFactory.commitment_id`), so a retried call on any replica targets the same item and
either sees `Duplicate` (replay the existing commitment) or wins the race exactly once.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

import mcp_types as types

from shopfront_server.domain.models import CapacityHold, Commitment, CommitmentStatus, IdempotencyRecord
from shopfront_server.profile.model import Profile


@dataclass(frozen=True)
class Committed:
    commitment: Commitment


@dataclass(frozen=True)
class Duplicate:
    """The idempotency key (or commitment id) already exists: `existing` is the stored commitment."""

    existing: Commitment


@dataclass(frozen=True)
class CapacityExhausted:
    """A capacity counter would exceed its limit; nothing was written."""

    key: str


CommitResult = Committed | Duplicate | CapacityExhausted


@runtime_checkable
class CommitmentStore(Protocol):
    async def commit(
        self, commitment: Commitment, *, holds: Sequence[CapacityHold], idempotency: IdempotencyRecord
    ) -> CommitResult:
        """Atomically create the commitment, its idempotency marker and its capacity holds (see module doc)."""
        ...

    async def get(self, business_id: str, commitment_id: str) -> Commitment | None: ...

    async def get_by_task(self, task_id: str) -> Commitment | None: ...

    async def find_by_reference_prefix(self, business_id: str, prefix: str, *, limit: int = 5) -> list[Commitment]:
        """Up to `limit` of `business_id`'s commitments whose id starts with `prefix` (strongly consistent).

        Resolves a spoken booking code (`ids.reference_prefix`) without any extra index: the id is the
        sort key, so this is a key-prefix query. The caller filters by kind, status and principal."""
        ...

    async def update(self, commitment: Commitment, *, expected_version: int) -> Commitment:
        """Compare-and-swap write; returns the stored record with `version` bumped.

        If the new status is in `models.RELEASES_HOLDS` and `holds_released` was False, the holds are
        released in the same atomic unit and `holds_released` is set. Raises
        `errors.ConcurrencyConflict` when the stored version differs.
        """
        ...

    async def list_for_business(
        self,
        business_id: str,
        *,
        statuses: Collection[CommitmentStatus] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Commitment], str | None]:
        """Newest first. `statuses=None` means all."""
        ...

    async def list_expiring(self, *, before: datetime, limit: int = 100) -> list[Commitment]:
        """Open commitments (`OPEN_STATUSES`) whose `expires_at` is before `before`, across businesses."""
        ...


@runtime_checkable
class CapacityStore(Protocol):
    """Read side of the capacity counters (writes happen inside `CommitmentStore.commit/update`)."""

    async def usage(self, keys: Sequence[str]) -> dict[str, int]:
        """Current units used per key (missing keys = 0)."""
        ...


@runtime_checkable
class IdempotencyStore(Protocol):
    """Read side of the idempotency markers (written inside `CommitmentStore.commit`)."""

    async def get(self, key: str) -> IdempotencyRecord | None:
        """The live (unexpired) marker for `key`, if any."""
        ...


@runtime_checkable
class OwnerNotifier(Protocol):
    """Tells the owner a request is waiting (owner_web channel: a signed inbox link; no SMS)."""

    async def notify(self, profile: Profile, commitment: Commitment) -> None: ...


@runtime_checkable
class DecisionRenderer(Protocol):
    """Builds the final `CallToolResult` of a commitment's task (implemented by the tools layer).

    The result must validate against the originating tool's output schema, because the MCP client
    re-validates task results against the tool's `outputSchema`.
    """

    def render(self, profile: Profile, commitment: Commitment) -> types.CallToolResult: ...
