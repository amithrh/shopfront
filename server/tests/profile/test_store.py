"""`profile.store`: the in-memory store (CAS, tenant gate, directory), directory loading and caching."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from support import SCHEMA_PATH, make_live

from shopfront_server import errors
from shopfront_server.clock import FixedClock
from shopfront_server.profile.directory import DirectoryEntry
from shopfront_server.profile.model import Profile
from shopfront_server.profile.store import (
    CachingProfileStore,
    InMemoryProfileStore,
    ProfileStore,
    check_cas,
    load_profile_dir,
)

pytestmark = pytest.mark.anyio


def _profile(doc: dict[str, Any], *, live: bool = True, revision: int | None = None, pid: str | None = None) -> Profile:
    doc = json.loads(json.dumps(doc))
    if pid is not None:
        doc["profile_id"] = pid
    if revision is not None:
        doc["revision"] = revision
    if live:
        doc["certification"] = {"status": "draft", "runs": []}
        doc = make_live(doc)
    else:
        doc["certification"] = {"status": "draft", "runs": []}
    return Profile.unchecked(doc)


@pytest.fixture
def live(example_raw: dict[str, Any]) -> Profile:
    return _profile(example_raw)


# --------------------------------------------------------------------------- InMemoryProfileStore


async def test_in_memory_store_satisfies_the_port(live: Profile) -> None:
    store = InMemoryProfileStore([live])
    assert isinstance(store, ProfileStore)
    assert await store.get("thimbleberry-bakehouse") is live
    assert await store.get_live("thimbleberry-bakehouse") is live
    assert await store.get("nobody") is None


async def test_get_live_hides_missing_and_non_live_alike(example_raw: dict[str, Any]) -> None:
    draft = _profile(example_raw, live=False, pid="draft-bakery")
    store = InMemoryProfileStore([draft])
    with pytest.raises(errors.UnknownBusiness) as missing:
        await store.get_live("nobody")
    with pytest.raises(errors.UnknownBusiness) as hidden:
        await store.get_live("draft-bakery")
    assert type(missing.value) is type(hidden.value) and missing.value.code == hidden.value.code
    assert await store.get("draft-bakery") is draft  # admin reads still see drafts


async def test_put_is_a_compare_and_swap(example_raw: dict[str, Any]) -> None:
    store = InMemoryProfileStore()
    first = _profile(example_raw, revision=6)
    await store.put(first, expected_revision=None)
    with pytest.raises(errors.ConcurrencyConflict):
        await store.put(first, expected_revision=None)  # already exists
    with pytest.raises(errors.ConcurrencyConflict):
        await store.put(_profile(example_raw, revision=8), expected_revision=5)  # stale expectation
    with pytest.raises(errors.ConcurrencyConflict):
        await store.put(_profile(example_raw, revision=6), expected_revision=6)  # does not advance
    second = _profile(example_raw, revision=7)
    await store.put(second, expected_revision=6)
    assert await store.get("thimbleberry-bakehouse") is second


async def test_concurrent_writers_exactly_one_wins(example_raw: dict[str, Any]) -> None:
    store = InMemoryProfileStore([_profile(example_raw, revision=6)])
    candidates = [_profile(example_raw, revision=7) for _ in range(8)]
    outcomes: list[str] = []

    async def write(profile: Profile) -> None:
        try:
            await store.put(profile, expected_revision=6)
            outcomes.append("won")
        except errors.ConcurrencyConflict:
            outcomes.append("lost")

    async with anyio.create_task_group() as group:
        for candidate in candidates:
            group.start_soon(write, candidate)
    assert sorted(outcomes) == ["lost"] * 7 + ["won"]
    stored = await store.get("thimbleberry-bakehouse")
    assert stored is not None and stored.revision == 7


async def test_directory_follows_the_live_status(example_raw: dict[str, Any]) -> None:
    store = InMemoryProfileStore(
        [
            _profile(example_raw, pid="zeta"),
            _profile(example_raw, pid="alpha"),
            _profile(example_raw, pid="mid", live=False),
        ]
    )
    entries = await store.list_live()
    assert [e.business_id for e in entries] == ["alpha", "zeta"]
    assert all(isinstance(e, DirectoryEntry) for e in entries)

    await store.put(_profile(example_raw, pid="zeta", revision=7, live=False), expected_revision=6)
    await store.put(_profile(example_raw, pid="mid", revision=7), expected_revision=6)
    assert [e.business_id for e in await store.list_live()] == ["alpha", "mid"]
    with pytest.raises(errors.UnknownBusiness):
        await store.get_live("zeta")


async def test_seeding_keeps_the_highest_revision(example_raw: dict[str, Any]) -> None:
    newer, older = _profile(example_raw, revision=9), _profile(example_raw, revision=3)
    store = InMemoryProfileStore([newer, older])
    assert await store.get("thimbleberry-bakehouse") is newer
    assert [e.revision for e in await store.list_live()] == [9]


def test_check_cas_rules() -> None:
    check_cas("b", None, 1, None)
    check_cas("b", 4, 5, 4)
    for stored, new, expected in [(1, 2, None), (None, 2, 1), (3, 4, 2), (3, 3, 3)]:
        with pytest.raises(errors.ConcurrencyConflict):
            check_cas("b", stored, new, expected)


# --------------------------------------------------------------------------- load_profile_dir


def _write(path: Path, doc: Any) -> Path:
    path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")
    return path


def test_load_profile_dir_skips_bad_files(
    tmp_path: Path,
    example_raw: dict[str, Any],
    gold_raw: Callable[[str], dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write(tmp_path / "a-salon.json", gold_raw("quillfern-salon"))
    _write(tmp_path / "b-broken.json", "{ not json")
    invalid = dict(example_raw, profile_id="invalid-bakery")
    del invalid["business"]
    _write(tmp_path / "c-invalid.json", invalid)
    _write(tmp_path / "d-bakery.json", example_raw)
    (tmp_path / "notes.txt").write_text("ignored")
    (tmp_path / "sub").mkdir()
    _write(tmp_path / "sub" / "nested.json", gold_raw("kettlewick-home"))  # non-recursive

    with caplog.at_level(logging.WARNING):
        profiles = load_profile_dir([tmp_path, tmp_path / "missing"], schema_path=SCHEMA_PATH)
    assert [p.profile_id for p in profiles] == ["quillfern-salon", "thimbleberry-bakehouse"]
    assert "b-broken.json" in caplog.text and "c-invalid.json" in caplog.text


def test_load_profile_dir_highest_revision_wins(tmp_path: Path, example_raw: dict[str, Any]) -> None:
    _write(tmp_path / "a.json", dict(example_raw, revision=9))
    _write(tmp_path / "b.json", example_raw)
    first, second = tmp_path / "x", tmp_path / "y"
    first.mkdir()
    second.mkdir()
    _write(first / "bakery.json", dict(example_raw, revision=3))
    _write(second / "bakery.json", dict(example_raw, revision=11))
    assert [p.revision for p in load_profile_dir([tmp_path], schema_path=SCHEMA_PATH)] == [9]
    assert [p.revision for p in load_profile_dir([first, second], schema_path=SCHEMA_PATH)] == [11]


def test_load_profile_dir_fallback(tmp_path: Path, example_raw: dict[str, Any]) -> None:
    fallback = _write(tmp_path / "fallback.json", example_raw)
    empty = tmp_path / "empty"
    empty.mkdir()
    loaded = load_profile_dir([empty], schema_path=SCHEMA_PATH, fallback=fallback)
    assert [p.profile_id for p in loaded] == ["thimbleberry-bakehouse"]

    only_bad = tmp_path / "bad"
    only_bad.mkdir()
    _write(only_bad / "broken.json", "nope")
    assert [p.profile_id for p in load_profile_dir([only_bad], schema_path=SCHEMA_PATH, fallback=fallback)] == [
        "thimbleberry-bakehouse"
    ]
    assert load_profile_dir([empty], schema_path=SCHEMA_PATH, fallback=tmp_path / "gone.json") == []
    assert load_profile_dir([empty], schema_path=SCHEMA_PATH) == []


def test_load_profile_dir_fallback_unused_when_something_loads(
    tmp_path: Path, example_raw: dict[str, Any], gold_raw: Callable[[str], dict[str, Any]]
) -> None:
    fallback = _write(tmp_path / "fallback.json", example_raw)
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    _write(profiles_dir / "salon.json", gold_raw("quillfern-salon"))
    loaded = load_profile_dir([profiles_dir], schema_path=SCHEMA_PATH, fallback=fallback)
    assert [p.profile_id for p in loaded] == ["quillfern-salon"]


def test_load_profile_dir_strict_rejects_unbound_live_profiles(tmp_path: Path, example_raw: dict[str, Any]) -> None:
    _write(tmp_path / "bakery.json", example_raw)  # live, but bound to a placeholder sha
    assert load_profile_dir([tmp_path], schema_path=SCHEMA_PATH, strict=True) == []
    assert len(load_profile_dir([tmp_path], schema_path=SCHEMA_PATH, strict=False)) == 1


# --------------------------------------------------------------------------- CachingProfileStore


class CountingStore:
    """A ProfileStore that counts calls to the inner in-memory store."""

    def __init__(self, inner: InMemoryProfileStore) -> None:
        self.inner = inner
        self.calls: dict[str, int] = {"get": 0, "list_live": 0, "put": 0}

    async def get(self, business_id: str) -> Profile | None:
        self.calls["get"] += 1
        return await self.inner.get(business_id)

    async def get_live(self, business_id: str) -> Profile:
        raise AssertionError("the cache derives get_live from get")

    async def put(self, profile: Profile, *, expected_revision: int | None) -> None:
        self.calls["put"] += 1
        await self.inner.put(profile, expected_revision=expected_revision)

    async def list_live(self) -> Sequence[DirectoryEntry]:
        self.calls["list_live"] += 1
        return await self.inner.list_live()


@pytest.fixture
def cached(live: Profile, clock: FixedClock) -> tuple[CachingProfileStore, CountingStore]:
    counting = CountingStore(InMemoryProfileStore([live]))
    return CachingProfileStore(counting, ttl_s=30, clock=clock), counting


async def test_cache_serves_reads_until_the_ttl(
    cached: tuple[CachingProfileStore, CountingStore], clock: FixedClock
) -> None:
    store, inner = cached
    assert isinstance(store, ProfileStore)
    for _ in range(3):
        await store.get_live("thimbleberry-bakehouse")
        await store.list_live()
    assert inner.calls == {"get": 1, "list_live": 1, "put": 0}
    clock.advance(timedelta(seconds=29))
    await store.get("thimbleberry-bakehouse")
    assert inner.calls["get"] == 1
    clock.advance(timedelta(seconds=1))
    await store.get("thimbleberry-bakehouse")
    await store.list_live()
    assert inner.calls == {"get": 2, "list_live": 2, "put": 0}


async def test_cache_caches_misses_and_hides_non_live(
    cached: tuple[CachingProfileStore, CountingStore], example_raw: dict[str, Any]
) -> None:
    store, inner = cached
    for _ in range(2):
        with pytest.raises(errors.UnknownBusiness):
            await store.get_live("nobody")
    assert inner.calls["get"] == 1
    await inner.inner.put(_profile(example_raw, pid="draft", live=False), expected_revision=None)
    with pytest.raises(errors.UnknownBusiness):
        await store.get_live("draft")


async def test_put_writes_through_and_invalidates(
    cached: tuple[CachingProfileStore, CountingStore], example_raw: dict[str, Any]
) -> None:
    store, inner = cached
    assert (await store.get_live("thimbleberry-bakehouse")).revision == 6
    await store.list_live()
    await store.put(_profile(example_raw, revision=7, live=False), expected_revision=6)
    assert inner.calls["put"] == 1
    with pytest.raises(errors.UnknownBusiness):
        await store.get_live("thimbleberry-bakehouse")  # re-read immediately, not after the TTL
    assert await store.list_live() == []


async def test_failed_put_still_invalidates(
    cached: tuple[CachingProfileStore, CountingStore], example_raw: dict[str, Any]
) -> None:
    store, inner = cached
    await store.get("thimbleberry-bakehouse")
    await inner.inner.put(_profile(example_raw, revision=7), expected_revision=6)  # another replica wrote
    with pytest.raises(errors.ConcurrencyConflict):
        await store.put(_profile(example_raw, revision=7), expected_revision=6)
    fresh = await store.get("thimbleberry-bakehouse")
    assert fresh is not None and fresh.revision == 7


async def test_list_live_returns_a_copy(cached: tuple[CachingProfileStore, CountingStore]) -> None:
    store, inner = cached
    entries = await store.list_live()
    assert isinstance(entries, list)
    entries.clear()  # a caller mutating its result must not empty the cache
    assert len(await store.list_live()) == 1
    assert inner.calls["list_live"] == 1


def test_negative_ttl_is_rejected(clock: FixedClock) -> None:
    with pytest.raises(ValueError):
        CachingProfileStore(InMemoryProfileStore(), ttl_s=-1, clock=clock)


async def test_round2_misc_low_cache_is_a_bounded_lru(live: Profile, clock: FixedClock) -> None:
    """SEC-8: random unknown ids evict each other (and the least recently read entry), never grow the heap."""
    counting = CountingStore(InMemoryProfileStore([live]))
    store = CachingProfileStore(counting, ttl_s=30, clock=clock, max_entries=3)
    assert (await store.get_live("thimbleberry-bakehouse")).profile_id == "thimbleberry-bakehouse"
    for n in range(10):
        with pytest.raises(errors.UnknownBusiness):
            await store.get_live(f"nobody-{n}")
        if n % 2 == 0:
            await store.get("thimbleberry-bakehouse")  # recently read: stays cached
        assert len(store) <= 3
    assert len(store) == 3
    reads = counting.calls["get"]
    await store.get("thimbleberry-bakehouse")
    assert counting.calls["get"] == reads  # still cached (it was read within the last 3 entries)
    await store.get("nobody-0")  # evicted long ago: read from the inner store again
    assert counting.calls["get"] == reads + 1
    with pytest.raises(ValueError):
        CachingProfileStore(InMemoryProfileStore(), ttl_s=30, clock=clock, max_entries=0)
