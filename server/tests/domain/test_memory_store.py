"""The in-memory stores: one lock makes commit atomic; update is a CAS that frees holds exactly once."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import pytest

from shopfront_server.clock import FixedClock
from shopfront_server.domain.idempotency import new_record
from shopfront_server.domain.models import CapacityHold, Commitment
from shopfront_server.domain.ports import CapacityExhausted, Committed, Duplicate
from shopfront_server.domain.stores.memory import (
    InMemoryCapacityStore,
    InMemoryCommitmentStore,
    InMemoryIdempotencyStore,
    MemoryState,
)
from shopfront_server.errors import ConcurrencyConflict, InvalidState, NotFound

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)
HOLD = CapacityHold(key="CAP#b#m#DAY#2026-10-18", units=1, limit=2)


def make(cid: str, *, status: str = "pending_owner", minutes: int = 0, **extra: Any) -> Commitment:
    fields: dict[str, Any] = {
        "commitment_id": cid,
        "business_id": "b",
        "module_id": "m",
        "kind": "custom_order",
        "status": status,
        "principal": "anonymous",
        "idempotency_key": f"idem_{cid}",
        "profile_revision": 1,
        "holds": (HOLD,),
        "created_at": NOW + timedelta(minutes=minutes),
        "updated_at": NOW + timedelta(minutes=minutes),
        "expires_at": NOW + timedelta(hours=48, minutes=minutes),
    }
    fields.update(extra)
    return Commitment(**fields)


@pytest.fixture
def stores(clock: FixedClock) -> tuple[MemoryState, InMemoryCommitmentStore, InMemoryCapacityStore, InMemoryIdempotencyStore]:
    state = MemoryState()
    return (
        state,
        InMemoryCommitmentStore(state, clock=clock),
        InMemoryCapacityStore(state),
        InMemoryIdempotencyStore(state, clock=clock),
    )


def record(commitment: Commitment, ttl: timedelta = timedelta(hours=24)) -> Any:
    return new_record(
        commitment.idempotency_key, business_id=commitment.business_id, commitment_id=commitment.commitment_id, now=NOW, ttl=ttl
    )


async def test_commit_writes_everything_or_nothing(stores: Any) -> None:
    state, commitments, capacity, idempotency = stores
    first, second, third = make("c_1"), make("c_2"), make("c_3")
    assert isinstance(await commitments.commit(first, holds=first.holds, idempotency=record(first)), Committed)
    assert isinstance(await commitments.commit(second, holds=second.holds, idempotency=record(second)), Committed)
    full = await commitments.commit(third, holds=third.holds, idempotency=record(third))
    assert full == CapacityExhausted(HOLD.key)
    assert await capacity.usage([HOLD.key, "CAP#unknown"]) == {HOLD.key: 2, "CAP#unknown": 0}
    assert await commitments.get("b", "c_3") is None and await idempotency.get("idem_c_3") is None
    assert set(state.commitments) == {"c_1", "c_2"}


async def test_replays_are_duplicates(stores: Any) -> None:
    _, commitments, capacity, idempotency = stores
    first = make("c_1")
    await commitments.commit(first, holds=first.holds, idempotency=record(first))
    again = await commitments.commit(first, holds=first.holds, idempotency=record(first))
    assert again == Duplicate(first)
    # Same idempotency key, different commitment id (cannot happen with derived ids, but must still dedupe).
    twin = make("c_9", idempotency_key=first.idempotency_key)
    assert await commitments.commit(twin, holds=twin.holds, idempotency=record(twin)) == Duplicate(first)
    assert await capacity.usage([HOLD.key]) == {HOLD.key: 1}
    marker = await idempotency.get(first.idempotency_key)
    assert marker is not None and marker.commitment_id == "c_1"


async def test_expired_marker_is_invisible_but_the_derived_id_still_dedupes(stores: Any, clock: FixedClock) -> None:
    _, commitments, _, idempotency = stores
    first = make("c_1")
    await commitments.commit(first, holds=(), idempotency=record(first, ttl=timedelta(hours=1)))
    clock.advance(timedelta(hours=2))
    assert await idempotency.get(first.idempotency_key) is None
    assert isinstance(await commitments.commit(first, holds=(), idempotency=record(first)), Duplicate)


async def test_holds_listed_twice_need_their_sum(stores: Any) -> None:
    _, commitments, _, _ = stores
    double = make("c_1", holds=(HOLD, HOLD, HOLD))
    assert await commitments.commit(double, holds=double.holds, idempotency=record(double)) == CapacityExhausted(HOLD.key)


async def test_concurrent_commits_never_exceed_capacity(stores: Any) -> None:
    _, commitments, capacity, _ = stores
    results: list[Any] = []

    async def attempt(index: int) -> None:
        c = make(f"c_{index}")
        results.append(await commitments.commit(c, holds=c.holds, idempotency=record(c)))

    async with anyio.create_task_group() as tg:
        for index in range(10):
            tg.start_soon(attempt, index)
    assert sum(isinstance(r, Committed) for r in results) == 2
    assert sum(isinstance(r, CapacityExhausted) for r in results) == 8
    assert await capacity.usage([HOLD.key]) == {HOLD.key: 2}


async def test_update_is_a_cas_and_releases_holds_once(stores: Any) -> None:
    _, commitments, capacity, _ = stores
    first = make("c_1")
    await commitments.commit(first, holds=first.holds, idempotency=record(first))
    declined = await commitments.update(first.model_copy(update={"status": "declined"}), expected_version=0)
    assert declined.version == 1 and declined.holds_released
    assert await capacity.usage([HOLD.key]) == {HOLD.key: 0}
    with pytest.raises(ConcurrencyConflict):
        await commitments.update(first.model_copy(update={"status": "approved"}), expected_version=0)
    with pytest.raises(InvalidState):
        await commitments.update(declined.model_copy(update={"status": "approved"}), expected_version=1)
    # A same-status edit never releases twice, and holds_released cannot be reset by the caller.
    edited = await commitments.update(declined.model_copy(update={"holds_released": False}), expected_version=1)
    assert edited.holds_released and await capacity.usage([HOLD.key]) == {HOLD.key: 0}
    with pytest.raises(NotFound):
        await commitments.update(make("c_missing"), expected_version=0)


async def test_non_releasing_transitions_keep_holds(stores: Any) -> None:
    _, commitments, capacity, _ = stores
    first = make("c_1")
    await commitments.commit(first, holds=first.holds, idempotency=record(first))
    approved = await commitments.update(first.model_copy(update={"status": "approved"}), expected_version=0)
    assert not approved.holds_released and await capacity.usage([HOLD.key]) == {HOLD.key: 1}
    cancelled = await commitments.update(approved.model_copy(update={"status": "cancelled"}), expected_version=1)
    assert cancelled.holds_released and await capacity.usage([HOLD.key]) == {HOLD.key: 0}


async def test_tenant_isolation(stores: Any) -> None:
    _, commitments, _, _ = stores
    first = make("c_1")
    await commitments.commit(first, holds=(), idempotency=record(first))
    assert await commitments.get("other-business", "c_1") is None
    with pytest.raises(NotFound):
        await commitments.update(first.model_copy(update={"business_id": "other-business"}), expected_version=0)
    items, _ = await commitments.list_for_business("other-business")
    assert items == []


async def test_list_for_business_is_newest_first_with_a_cursor(stores: Any) -> None:
    _, commitments, _, _ = stores
    for index in range(5):
        c = make(f"c_{index}", minutes=index, status="confirmed" if index % 2 else "pending_owner", holds=())
        await commitments.commit(c, holds=(), idempotency=record(c))
    page, cursor = await commitments.list_for_business("b", limit=2)
    assert [c.commitment_id for c in page] == ["c_4", "c_3"] and cursor
    page, cursor = await commitments.list_for_business("b", limit=2, cursor=cursor)
    assert [c.commitment_id for c in page] == ["c_2", "c_1"] and cursor
    page, cursor = await commitments.list_for_business("b", limit=2, cursor=cursor)
    assert [c.commitment_id for c in page] == ["c_0"] and cursor is None
    pending, _ = await commitments.list_for_business("b", statuses=["pending_owner"])
    assert [c.commitment_id for c in pending] == ["c_4", "c_2", "c_0"]
    with pytest.raises(ValueError):
        await commitments.list_for_business("b", cursor="not-a-cursor")


async def test_list_expiring_and_get_by_task(stores: Any) -> None:
    _, commitments, _, _ = stores
    soon = make("c_soon", minutes=-60, task_id="t_1", holds=())
    later = make("c_later", minutes=600, holds=())
    done = make("c_done", minutes=-120, status="confirmed", holds=())
    for c in (soon, later, done):
        await commitments.commit(c, holds=(), idempotency=record(c))
    due = await commitments.list_expiring(before=NOW + timedelta(hours=48))
    assert [c.commitment_id for c in due] == ["c_soon"]
    assert await commitments.get_by_task("t_1") == soon
    assert await commitments.get_by_task("t_unknown") is None


async def test_round2_misc_low_find_by_reference_prefix_is_tenant_scoped_and_bounded(stores: Any) -> None:
    """A spoken booking code resolves by id prefix, within one business only (VUX-11)."""
    _, commitments, _, _ = stores
    rows = [make("c_exyqrb"), make("c_exyqra"), make("c_exyqrc"), make("c_exyqzz"), make("c_exyqrd", business_id="other")]
    for row in rows:
        await commitments.commit(row, holds=(), idempotency=record(row))
    found = await commitments.find_by_reference_prefix("b", "c_exyqr")
    assert [c.commitment_id for c in found] == ["c_exyqra", "c_exyqrb", "c_exyqrc"]  # sort-key order
    assert [c.commitment_id for c in await commitments.find_by_reference_prefix("b", "c_exyqr", limit=2)] == ["c_exyqra", "c_exyqrb"]
    assert [c.commitment_id for c in await commitments.find_by_reference_prefix("other", "c_exyqr")] == ["c_exyqrd"]
    assert await commitments.find_by_reference_prefix("b", "c_zzzzz") == []
