"""In-memory implementations of the domain ports, sharing one `MemoryState` (and one lock) so that
`CommitmentStore.commit` is atomic across commitments, idempotency markers and capacity counters,
exactly like the DynamoDB transaction it stands in for.

Semantics match `stores.dynamodb` exactly (the test suite runs the same scenarios against both):
  * `commit`: Duplicate if the idempotency marker (unexpired) or the commitment id exists; otherwise
    CapacityExhausted if any counter would exceed its limit; otherwise everything is written.
  * `update`: compare-and-swap on `version`; the status change must be allowed by
    `models.ALLOWED_TRANSITIONS`; entering a `RELEASES_HOLDS` status frees the holds exactly once.
  * `list_for_business`: newest first (`created_at`, then id), opaque cursor.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Collection, Sequence
from datetime import UTC, datetime

import anyio

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
from askonce_server.errors import ConcurrencyConflict, InvalidState, NotFound


class MemoryState:
    """Process-local tables. Tests may inspect them directly."""

    def __init__(self) -> None:
        self.lock = anyio.Lock()
        self.commitments: dict[str, Commitment] = {}  # commitment_id -> record
        self.idempotency: dict[str, IdempotencyRecord] = {}  # key -> marker
        self.capacity: dict[str, int] = {}  # counter key -> units used


def merge_holds(holds: Sequence[CapacityHold]) -> dict[str, tuple[int, int]]:
    """Units and (tightest) limit per counter key; a key listed twice needs the sum of its units."""
    merged: dict[str, tuple[int, int]] = {}
    for hold in holds:
        units, limit = merged.get(hold.key, (0, hold.limit))
        merged[hold.key] = (units + hold.units, min(limit, hold.limit))
    return merged


def _sort_key(commitment: Commitment) -> tuple[str, str]:
    return commitment.created_at.astimezone(UTC).isoformat(), commitment.commitment_id


def _encode_cursor(commitment: Commitment) -> str:
    raw = json.dumps(list(_sort_key(commitment))).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created, commitment_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        return str(created), str(commitment_id)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid cursor") from exc


class InMemoryCommitmentStore:
    def __init__(self, state: MemoryState, *, clock: Clock) -> None:
        self._state = state
        self._clock = clock

    async def commit(
        self, commitment: Commitment, *, holds: Sequence[CapacityHold], idempotency: IdempotencyRecord
    ) -> CommitResult:
        state = self._state
        async with state.lock:
            marker = state.idempotency.get(idempotency.key)
            if marker is not None and marker.expires_at > self._clock.now():
                existing = state.commitments.get(marker.commitment_id)
                if existing is not None:
                    return Duplicate(existing)
            existing = state.commitments.get(commitment.commitment_id)
            if existing is not None:
                return Duplicate(existing)
            merged = merge_holds(holds)
            for key, (units, limit) in merged.items():
                if state.capacity.get(key, 0) + units > limit:
                    return CapacityExhausted(key)
            for key, (units, _) in merged.items():
                state.capacity[key] = state.capacity.get(key, 0) + units
            state.idempotency[idempotency.key] = idempotency
            state.commitments[commitment.commitment_id] = commitment
            return Committed(commitment)

    async def get(self, business_id: str, commitment_id: str) -> Commitment | None:
        found = self._state.commitments.get(commitment_id)
        return found if found is not None and found.business_id == business_id else None

    async def get_by_task(self, task_id: str) -> Commitment | None:
        return next((c for c in self._state.commitments.values() if c.task_id == task_id), None)

    async def find_by_reference_prefix(self, business_id: str, prefix: str, *, limit: int = 5) -> list[Commitment]:
        """Sort-key order (by id), like the DynamoDB `begins_with` query."""
        rows = [c for c in self._state.commitments.values() if c.business_id == business_id and c.commitment_id.startswith(prefix)]
        return sorted(rows, key=lambda c: c.commitment_id)[:limit]

    async def update(self, commitment: Commitment, *, expected_version: int) -> Commitment:
        state = self._state
        async with state.lock:
            current = state.commitments.get(commitment.commitment_id)
            if current is None or current.business_id != commitment.business_id:
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
            if release:
                for key, (units, _) in merge_holds(current.holds).items():
                    state.capacity[key] = max(0, state.capacity.get(key, 0) - units)
            state.commitments[stored.commitment_id] = stored
            return stored

    async def list_for_business(
        self,
        business_id: str,
        *,
        statuses: Collection[CommitmentStatus] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[Commitment], str | None]:
        wanted = set(statuses) if statuses is not None else None
        rows = sorted(
            (
                c
                for c in self._state.commitments.values()
                if c.business_id == business_id and (wanted is None or c.status in wanted)
            ),
            key=_sort_key,
            reverse=True,
        )
        if cursor:
            after = _decode_cursor(cursor)
            rows = [c for c in rows if _sort_key(c) < after]
        page = rows[:limit]
        next_cursor = _encode_cursor(page[-1]) if len(rows) > limit and page else None
        return page, next_cursor

    async def list_expiring(self, *, before: datetime, limit: int = 100) -> list[Commitment]:
        due = [
            c
            for c in self._state.commitments.values()
            if c.status in OPEN_STATUSES and c.expires_at is not None and c.expires_at < before
        ]
        due.sort(key=lambda c: (c.expires_at or before, c.commitment_id))
        return due[:limit]


class InMemoryCapacityStore:
    def __init__(self, state: MemoryState) -> None:
        self._state = state

    async def usage(self, keys: Sequence[str]) -> dict[str, int]:
        return {key: self._state.capacity.get(key, 0) for key in keys}


class InMemoryIdempotencyStore:
    def __init__(self, state: MemoryState, *, clock: Clock) -> None:
        self._state = state
        self._clock = clock

    async def get(self, key: str) -> IdempotencyRecord | None:
        marker = self._state.idempotency.get(key)
        if marker is None or marker.expires_at <= self._clock.now():
            return None
        return marker
