"""`TaskManager`: creation, idempotent ids, the transition table, TTL expiry on an injected clock, CAS retries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import anyio
import mcp_types as types
import pytest

from mcp_tasks import DuplicateTask, InMemoryTaskStore, InvalidTransition, TaskManager, TaskNotFound, TaskRecord
from support import EPOCH, ManualClock, form_question

pytestmark = pytest.mark.anyio

DONE = types.CallToolResult(content=[types.TextContent(type="text", text="done")])


@pytest.fixture
def manager(clock: ManualClock) -> TaskManager:
    return TaskManager(InMemoryTaskStore(), now=clock, default_ttl_ms=60_000, default_poll_interval_ms=1000)


async def _new(manager: TaskManager, **kwargs: object) -> TaskRecord:
    params: dict[str, object] = {"principal": "alice", "tool_name": "approve", "arguments": {"x": 1}}
    params["status_message"] = "Waiting"
    params.update(kwargs)
    return await manager.create(**params)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- creation


async def test_create_uses_the_injected_clock_and_defaults(manager: TaskManager) -> None:
    record = await _new(manager, labels={"tenant": "t1"})
    assert record.status == "working" and record.version == 0
    assert record.created_at == record.last_updated_at == EPOCH
    assert record.ttl_ms == 60_000 and record.poll_interval_ms == 1000
    assert record.labels == {"tenant": "t1"} and record.arguments == {"x": 1}
    assert len(record.task_id) >= 22  # 128-bit token_urlsafe
    assert await manager.get(record.task_id) == record


async def test_random_ids_are_unique(manager: TaskManager) -> None:
    ids = {(await _new(manager)).task_id for _ in range(20)}
    assert len(ids) == 20


async def test_per_call_ttl_and_poll_interval_override_defaults(manager: TaskManager) -> None:
    record = await _new(manager, ttl_ms=5, poll_interval_ms=0)
    assert record.ttl_ms == 5 and record.poll_interval_ms == 0


async def test_unlimited_ttl_never_expires(clock: ManualClock) -> None:
    manager = TaskManager(InMemoryTaskStore(), now=clock, default_ttl_ms=None)
    record = await _new(manager)
    clock.advance(days=3650)
    assert (await manager.get(record.task_id)).ttl_ms is None


@pytest.mark.parametrize("field", ["ttl_ms", "poll_interval_ms"])
async def test_negative_durations_are_rejected(manager: TaskManager, field: str) -> None:
    with pytest.raises(ValueError):
        await _new(manager, **{field: -1})


def test_negative_defaults_are_rejected() -> None:
    with pytest.raises(ValueError):
        TaskManager(InMemoryTaskStore(), default_ttl_ms=-1)


async def test_empty_task_id_is_rejected(manager: TaskManager) -> None:
    with pytest.raises(ValueError):
        await _new(manager, task_id="")


async def test_naive_clock_is_rejected() -> None:
    manager = TaskManager(InMemoryTaskStore(), now=lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        await _new(manager)


async def test_reused_id_returns_the_stored_record_unchanged(manager: TaskManager, clock: ManualClock) -> None:
    first = await _new(manager, task_id="idem-1")
    done = await manager.complete("idem-1", DONE)
    clock.advance(seconds=5)
    again = await _new(manager, task_id="idem-1", status_message="ignored", arguments={"other": True})
    assert again == done and again.status == "completed" and again.created_at == first.created_at


async def test_round2_capacity_race_refresh_ttl_extends_a_reused_active_task(
    manager: TaskManager, clock: ManualClock
) -> None:
    """A retry that starts its own deadline long after the first attempt (e.g. a commit that lost a
    capacity race, retried later) must not inherit the first attempt's expiry."""
    first = await _new(manager, task_id="idem-r")
    clock.advance(seconds=30)
    plain = await _new(manager, task_id="idem-r")
    assert plain == first  # without refresh_ttl: unchanged, expires 30 s after the retry
    refreshed = await _new(manager, task_id="idem-r", refresh_ttl=True, status_message="ignored")
    assert refreshed.ttl_ms == 90_000 and refreshed.expires_at() == EPOCH + timedelta(seconds=90)
    assert refreshed.created_at == first.created_at and refreshed.status_message == "Waiting"
    assert refreshed.version == 1 and refreshed.last_updated_at == EPOCH + timedelta(seconds=30)
    assert await manager.get("idem-r") == refreshed
    clock.advance(seconds=59)
    assert (await manager.get("idem-r")).status == "working"  # the first TTL alone would have expired
    again = await _new(manager, task_id="idem-r", refresh_ttl=True, ttl_ms=1000)
    assert again == refreshed  # already lives long enough: never shortened, no write


async def test_round2_capacity_race_refresh_ttl_leaves_terminal_and_unlimited_tasks_alone(
    manager: TaskManager, clock: ManualClock
) -> None:
    await _new(manager, task_id="idem-done")
    done = await manager.complete("idem-done", DONE)
    await _new(manager, task_id="idem-open")
    unlimited = await manager.set_ttl("idem-open", None)
    clock.advance(seconds=50)
    assert await _new(manager, task_id="idem-done", refresh_ttl=True) == done
    assert await _new(manager, task_id="idem-open", refresh_ttl=True) == unlimited


async def test_round2_capacity_race_refresh_ttl_can_lift_the_expiry_and_survives_contention(
    clock: ManualClock,
) -> None:
    store = _ConflictingStore(conflicts=0)
    manager = TaskManager(store, now=clock, default_ttl_ms=None)
    first = await _new(manager, task_id="idem-u", ttl_ms=1000)
    store.conflicts = 1  # another replica writes between our read and our CAS
    lifted = await _new(manager, task_id="idem-u", refresh_ttl=True)  # default (unlimited) TTL requested
    assert lifted.ttl_ms is None and lifted.version == first.version + 1 and store.replace_calls == 2
    clock.advance(days=30)
    assert (await manager.get("idem-u")).status == "working"


@pytest.mark.parametrize("change", [{"principal": "mallory"}, {"tool_name": "other_tool"}])
async def test_reused_id_with_another_principal_or_tool_raises_duplicate(
    manager: TaskManager, change: dict[str, str]
) -> None:
    await _new(manager, task_id="idem-2")
    with pytest.raises(DuplicateTask):
        await _new(manager, task_id="idem-2", **change)


async def test_random_id_collision_is_not_swallowed() -> None:
    class CollidingStore(InMemoryTaskStore):
        async def put_new(self, record: TaskRecord) -> None:
            raise DuplicateTask(record.task_id)

    colliding = TaskManager(CollidingStore())
    with pytest.raises(DuplicateTask):
        await _new(colliding)


# --------------------------------------------------------------------------- reads, principals, expiry


async def test_get_hides_foreign_and_unknown_tasks(manager: TaskManager) -> None:
    record = await _new(manager)
    assert await manager.get(record.task_id, principal="alice") == record
    with pytest.raises(TaskNotFound):
        await manager.get(record.task_id, principal="bob")
    with pytest.raises(TaskNotFound):
        await manager.get("nope")


async def test_ttl_expiry_is_deterministic_on_the_injected_clock(manager: TaskManager, clock: ManualClock) -> None:
    record = await _new(manager)
    clock.advance(seconds=60)
    assert (await manager.get(record.task_id)).task_id == record.task_id  # exactly at createdAt + ttl: live
    clock.advance(milliseconds=1)
    with pytest.raises(TaskNotFound):
        await manager.get(record.task_id)
    with pytest.raises(TaskNotFound):
        await manager.complete(record.task_id, DONE)  # expired tasks cannot transition either
    with pytest.raises(TaskNotFound):
        await manager.cancel(record.task_id)


async def test_list_for_filters_expired_and_foreign_tasks(manager: TaskManager, clock: ManualClock) -> None:
    old = await _new(manager, ttl_ms=1000)
    clock.advance(seconds=1)
    live = await _new(manager)
    await _new(manager, principal="bob")
    clock.advance(seconds=1)
    rows, nxt = await manager.list_for("alice", cursor=None, limit=10)
    assert [r.task_id for r in rows] == [live.task_id] and nxt is None
    assert old.task_id not in {r.task_id for r in rows}


async def test_list_for_pages_oldest_first(manager: TaskManager, clock: ManualClock) -> None:
    created = []
    for _ in range(5):
        created.append((await _new(manager)).task_id)
        clock.advance(seconds=1)
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        rows, cursor = await manager.list_for("alice", cursor=cursor, limit=2)
        seen += [r.task_id for r in rows]
        pages += 1
        if cursor is None:
            break
    assert seen == created and pages == 3


async def test_list_for_rejects_a_malformed_cursor(manager: TaskManager) -> None:
    for cursor in ("abc", "-1", "1.5"):
        with pytest.raises(ValueError):
            await manager.list_for("alice", cursor=cursor, limit=2)


# --------------------------------------------------------------------------- transitions


async def test_set_message_keeps_status_and_bumps_version(manager: TaskManager, clock: ManualClock) -> None:
    record = await _new(manager)
    clock.advance(seconds=2)
    updated = await manager.set_message(record.task_id, "Owner opened the link")
    assert updated.status == "working" and updated.status_message == "Owner opened the link"
    assert updated.version == 1 and updated.last_updated_at == EPOCH + timedelta(seconds=2)
    assert updated.created_at == EPOCH


async def test_last_updated_at_never_moves_backwards(manager: TaskManager, clock: ManualClock) -> None:
    record = await _new(manager)
    clock.advance(seconds=10)
    later = await manager.set_message(record.task_id, "a")
    clock.current -= timedelta(seconds=30)  # the wall clock steps back (NTP, another replica)
    again = await manager.set_message(record.task_id, "b")
    assert again.last_updated_at == later.last_updated_at >= again.created_at


async def test_complete_fail_cancel_are_terminal(manager: TaskManager) -> None:
    a, b, c = (await _new(manager)), (await _new(manager)), (await _new(manager))
    completed = await manager.complete(a.task_id, DONE, message="Approved")
    failed = await manager.fail(b.task_id, "gateway down", code=-32000)
    cancelled = await manager.cancel(c.task_id, "Expired")
    assert completed.result is not None
    assert completed.result["content"] == [{"type": "text", "text": "done"}] and completed.result["isError"] is False
    assert completed.status_message == "Approved"
    assert failed.error == {"code": -32000, "message": "gateway down"}
    assert cancelled.status_message == "Expired"
    for record in (completed, failed, cancelled):
        assert record.terminal
        tid = record.task_id
        attempts: list[Callable[[], Awaitable[TaskRecord]]] = [
            lambda tid=tid: manager.complete(tid, DONE),
            lambda tid=tid: manager.fail(tid, "x"),
            lambda tid=tid: manager.cancel(tid),
            lambda tid=tid: manager.set_message(tid, "x"),
            lambda tid=tid: manager.require_input(tid, {"q": form_question("?")}, "q"),
        ]
        for attempt in attempts:
            with pytest.raises(InvalidTransition):
                await attempt()
        assert await manager.get(tid) == record  # nothing changed


async def test_input_round_trip_partial_then_complete(manager: TaskManager) -> None:
    record = await _new(manager)
    asked = await manager.require_input(
        record.task_id, {"q1": form_question("Size?"), "q2": form_question("Colour?")}, "Two questions"
    )
    assert asked.status == "input_required" and set(asked.input_requests or {}) == {"q1", "q2"}
    with pytest.raises(InvalidTransition):  # only a working task can ask
        await manager.require_input(record.task_id, {"q3": form_question("?")}, "again")
    partial = await manager.submit_input(record.task_id, "alice", {"q1": {"action": "accept", "content": {"answer": "L"}}})
    assert partial.status == "input_required" and set(partial.input_requests or {}) == {"q2"}
    assert partial.status_message == "Two questions"
    done = await manager.submit_input(record.task_id, "alice", {"q2": {"action": "decline"}})
    assert done.status == "working" and done.input_requests is None
    assert done.status_message == "Input received; continuing"
    assert done.input_responses == {"q1": {"action": "accept", "content": {"answer": "L"}}, "q2": {"action": "decline"}}


@pytest.mark.parametrize("action", ["decline", "cancel"])
async def test_round2_misc_low_non_accept_answers_are_stored_as_their_action_only(
    manager: TaskManager, action: str
) -> None:
    record = await _new(manager)
    await manager.require_input(record.task_id, {"q1": form_question("Size?")}, "Q")
    smuggled = {"action": action, "content": {"answer": "x" * 30_000, "card_number": "4111111111111111"}, "_meta": {"k": 1}}
    done = await manager.submit_input(record.task_id, "alice", {"q1": smuggled})
    assert done.status == "working" and done.input_requests is None
    assert done.input_responses == {"q1": {"action": action}}
    assert (await manager.get(record.task_id)).input_responses == {"q1": {"action": action}}


async def test_round2_misc_low_accepted_answers_keep_only_the_validated_content(manager: TaskManager) -> None:
    record = await _new(manager)
    url_question = types.ElicitRequest(
        params=types.ElicitRequestURLParams(message="Pay", url="https://example.com/pay", elicitation_id="e1")
    )
    await manager.require_input(record.task_id, {"q1": form_question("Size?"), "q2": url_question}, "Q")
    done = await manager.submit_input(
        record.task_id,
        "alice",
        {
            "q1": {"action": "accept", "content": {"answer": "L"}, "_meta": {"blob": "y" * 1000}},
            "q2": {"action": "accept", "content": {"never": "checked"}},  # no form: nothing to validate it against
        },
    )
    assert done.input_responses == {"q1": {"action": "accept", "content": {"answer": "L"}}, "q2": {"action": "accept"}}


async def test_round2_misc_low_non_elicitation_answers_are_kept_as_given(manager: TaskManager) -> None:
    record = await _new(manager)
    roots_question = types.ListRootsRequest()
    await manager.require_input(record.task_id, {"roots": roots_question}, "Q")
    answer = {"roots": [{"uri": "file:///tmp", "name": "tmp"}]}
    done = await manager.submit_input(record.task_id, "alice", {"roots": answer})
    assert done.input_responses == {"roots": answer}


async def test_submit_input_guards(manager: TaskManager) -> None:
    record = await _new(manager)
    with pytest.raises(InvalidTransition, match="not waiting"):
        await manager.submit_input(record.task_id, "alice", {"q1": {"action": "accept"}})
    await manager.require_input(record.task_id, {"q1": form_question("?")}, "Q")
    with pytest.raises(InvalidTransition, match="unknown input keys"):
        await manager.submit_input(record.task_id, "alice", {"zzz": {"action": "accept"}})
    with pytest.raises(TaskNotFound):  # another principal cannot answer
        await manager.submit_input(record.task_id, "bob", {"q1": {"action": "accept"}})


async def test_require_input_needs_a_request(manager: TaskManager) -> None:
    record = await _new(manager)
    with pytest.raises(ValueError):
        await manager.require_input(record.task_id, {}, "nothing")


async def test_input_required_task_can_be_completed_or_cancelled(manager: TaskManager) -> None:
    a, b = (await _new(manager)), (await _new(manager))
    for record in (a, b):
        await manager.require_input(record.task_id, {"q": form_question("?")}, "Q")
    assert (await manager.complete(a.task_id, DONE)).input_requests is None
    assert (await manager.cancel(b.task_id)).input_requests is None


# --------------------------------------------------------------------------- concurrency


class _ConflictingStore(InMemoryTaskStore):
    """Rejects the first `conflicts` CAS writes, as if another replica had just written."""

    def __init__(self, conflicts: int) -> None:
        super().__init__()
        self.conflicts = conflicts
        self.replace_calls = 0

    async def replace(self, record: TaskRecord, *, expected_version: int) -> bool:
        self.replace_calls += 1
        if self.conflicts > 0:
            self.conflicts -= 1
            return False
        return await super().replace(record, expected_version=expected_version)


async def test_cas_conflict_is_retried() -> None:
    store = _ConflictingStore(conflicts=3)
    manager = TaskManager(store)
    record = await _new(manager)
    done = await manager.complete(record.task_id, DONE)
    assert done.status == "completed" and done.version == 1 and store.replace_calls == 4


async def test_persistent_contention_gives_up() -> None:
    manager = TaskManager(_ConflictingStore(conflicts=1000))
    record = await _new(manager)
    with pytest.raises(RuntimeError, match="contention"):
        await manager.set_message(record.task_id, "x")


async def test_racing_decisions_have_exactly_one_winner() -> None:
    manager = TaskManager(InMemoryTaskStore())
    record = await _new(manager)
    outcomes: list[str] = []

    async def decide(kind: str) -> None:
        try:
            if kind == "complete":
                await manager.complete(record.task_id, DONE)
            else:
                await manager.cancel(record.task_id)
            outcomes.append(kind)
        except InvalidTransition:
            outcomes.append("rejected")

    async with anyio.create_task_group() as tg:
        for kind in ("complete", "cancel", "complete", "cancel"):
            tg.start_soon(decide, kind)
    assert outcomes.count("rejected") == 3
    final = await manager.get(record.task_id)
    assert final.version == 1 and final.status in {"completed", "cancelled"}


async def test_wait_for_change_wakes_on_a_transition() -> None:
    manager = TaskManager(InMemoryTaskStore())
    record = await _new(manager)
    assert await manager.wait_for_change(record.task_id, 0.01) is False
    woke: list[bool] = []
    async with anyio.create_task_group() as tg:

        async def waiter() -> None:
            woke.append(await manager.wait_for_change(record.task_id, 5))

        tg.start_soon(waiter)
        await anyio.sleep(0.01)
        await manager.set_message(record.task_id, "changed")
    assert woke == [True]


async def test_set_ttl_extends_or_lifts_the_expiry_of_an_active_task() -> None:
    from support import ManualClock

    clock = ManualClock()
    manager = TaskManager(InMemoryTaskStore(), default_ttl_ms=1000, now=clock)
    record = await manager.create(principal="p", tool_name="t", arguments={}, status_message="Waiting")
    await manager.set_ttl(record.task_id, None)
    clock.advance(days=30)
    assert (await manager.get(record.task_id)).ttl_ms is None
    await manager.complete(record.task_id, types.CallToolResult(content=[]))
    with pytest.raises(InvalidTransition):
        await manager.set_ttl(record.task_id, 5)
