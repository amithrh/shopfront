"""Profile storage: the `ProfileStore` port plus in-memory and file-backed implementations.

Multi-tenancy rule: customer-facing code calls `get_live(business_id)`, which raises
`errors.UnknownBusiness` for missing *and* non-live profiles alike, so draft tenants are invisible.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeVar, runtime_checkable

import anyio

from shopfront_server import errors
from shopfront_server.clock import Clock
from shopfront_server.profile.directory import DirectoryEntry, build_directory_entry
from shopfront_server.profile.loader import load_profile_file
from shopfront_server.profile.model import Profile
from shopfront_server.profile.snapshots import SnapshotProvider

logger = logging.getLogger(__name__)

T = TypeVar("T")


@runtime_checkable
class ProfileStore(Protocol):
    """Port for profile persistence.

    Contract:
      * `put` is a compare-and-swap on `revision`: with `expected_revision=None` the profile must not
        exist yet; otherwise the stored revision must equal it, and the new profile's revision must be
        greater. A lost race raises `errors.ConcurrencyConflict`.
      * `put` keeps the directory in step: live profiles are (re)indexed, non-live ones removed.
      * reads are strongly consistent.
    """

    async def get(self, business_id: str) -> Profile | None: ...

    async def get_live(self, business_id: str) -> Profile: ...

    async def put(self, profile: Profile, *, expected_revision: int | None) -> None: ...

    async def list_live(self) -> Sequence[DirectoryEntry]: ...


def unknown_business(business_id: str) -> errors.UnknownBusiness:
    """The one error for missing and non-live tenants (indistinguishable on purpose)."""
    return errors.UnknownBusiness(f"Unknown business {business_id!r}")


def check_cas(business_id: str, stored_revision: int | None, new_revision: int, expected_revision: int | None) -> None:
    """The `put` compare-and-swap rule shared by every store. Raises `errors.ConcurrencyConflict`."""
    if expected_revision is None:
        if stored_revision is not None:
            raise errors.ConcurrencyConflict(f"Profile {business_id!r} already exists (revision {stored_revision})")
        return
    if stored_revision != expected_revision:
        raise errors.ConcurrencyConflict(
            f"Profile {business_id!r} is at revision {stored_revision}, expected {expected_revision}"
        )
    if new_revision <= expected_revision:
        raise errors.ConcurrencyConflict(
            f"Profile {business_id!r} revision {new_revision} does not advance past {expected_revision}"
        )


class InMemoryProfileStore:
    """Dict-backed store (tests, single-replica dev).

    Seeding with several profiles of one id keeps the highest revision. Writes are serialised by an
    `anyio.Lock`; reads see whole `Profile` objects (immutable), so they need no lock.
    """

    def __init__(self, profiles: Sequence[Profile] = ()) -> None:
        self._profiles: dict[str, Profile] = {}
        self._directory: dict[str, DirectoryEntry] = {}
        self._lock = anyio.Lock()
        for profile in profiles:
            current = self._profiles.get(profile.profile_id)
            if current is None or profile.revision > current.revision:
                self._store(profile)

    def _store(self, profile: Profile) -> None:
        self._profiles[profile.profile_id] = profile
        if profile.is_live:
            self._directory[profile.profile_id] = build_directory_entry(profile)
        else:
            self._directory.pop(profile.profile_id, None)

    async def get(self, business_id: str) -> Profile | None:
        return self._profiles.get(business_id)

    async def get_live(self, business_id: str) -> Profile:
        profile = self._profiles.get(business_id)
        if profile is None or not profile.is_live:
            raise unknown_business(business_id)
        return profile

    async def put(self, profile: Profile, *, expected_revision: int | None) -> None:
        async with self._lock:
            current = self._profiles.get(profile.profile_id)
            check_cas(
                profile.profile_id,
                current.revision if current is not None else None,
                profile.revision,
                expected_revision,
            )
            self._store(profile)

    async def list_live(self) -> Sequence[DirectoryEntry]:
        return [self._directory[business_id] for business_id in sorted(self._directory)]


def load_profile_dir(
    directories: Sequence[Path],
    *,
    schema_path: Path,
    fallback: Path | None = None,
    snapshots: SnapshotProvider | None = None,
    strict: bool = False,
) -> list[Profile]:
    """Load every `*.json` in `directories` (sorted, non-recursive). If none load, load `fallback`.

    Invalid files are logged with their `LoadReport` and skipped, never fatal, so one bad tenant cannot
    take the server down. Duplicate profile ids: the highest revision wins. Returns profiles sorted by id.
    """
    loaded: dict[str, Profile] = {}

    def load(path: Path) -> None:
        try:
            result = load_profile_file(path, schema_path=schema_path, snapshots=snapshots, strict=strict)
        except errors.ProfileInvalid as exc:
            logger.warning("Skipping invalid profile %s: %s", path, exc)
            return
        except OSError as exc:
            logger.warning("Skipping unreadable profile %s: %s", path, exc)
            return
        profile = result.profile
        if result.report.warnings:
            logger.info("Loaded profile %s with %d warning(s)", path.name, len(result.report.warnings))
        current = loaded.get(profile.profile_id)
        if current is not None:
            logger.warning(
                "Duplicate profile id %r (revisions %d and %d): keeping the higher revision",
                profile.profile_id,
                current.revision,
                profile.revision,
            )
            if profile.revision <= current.revision:
                return
        loaded[profile.profile_id] = profile

    for directory in directories:
        directory = Path(directory)
        if not directory.is_dir():
            logger.info("Profile directory %s does not exist; skipping it", directory)
            continue
        for path in sorted(directory.glob("*.json")):
            if path.is_file():
                load(path)
    if not loaded and fallback is not None:
        logger.info("No profile loaded from %s; loading fallback %s", [str(d) for d in directories], fallback)
        load(Path(fallback))
    return [loaded[business_id] for business_id in sorted(loaded)]


@dataclass(frozen=True)
class _Cached[T]:
    value: T
    expires_at: datetime


DEFAULT_CACHE_ENTRIES = 1024
"""Bound on cached profiles (hits and misses): a caller-supplied id must never grow memory without limit."""


class CachingProfileStore:
    """Read-through TTL cache in front of another store (profiles change rarely; reads are hot).

    `put` writes through and invalidates. `list_live` is cached as one entry. Misses (`None`) are cached
    too, so a repeated unknown id cannot hammer the backing store. The per-business cache is an LRU
    bounded by `max_entries` (random ids evict each other, never the whole heap). `get_live` is
    derived from the cached `get`; a stored profile that fails validation is reported as an unknown
    business (strict invariants reject it, so it must be as invisible as a draft).
    """

    def __init__(
        self, inner: ProfileStore, *, ttl_s: float, clock: Clock, max_entries: int = DEFAULT_CACHE_ENTRIES
    ) -> None:
        if ttl_s < 0:
            raise ValueError("ttl_s must be >= 0")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._inner = inner
        self._ttl = timedelta(seconds=ttl_s)
        self._clock = clock
        self._max_entries = max_entries
        self._profiles: OrderedDict[str, _Cached[Profile | None]] = OrderedDict()
        self._directory: _Cached[list[DirectoryEntry]] | None = None

    def _fresh(self, entry: _Cached[T] | None) -> bool:
        return entry is not None and self._clock.now() < entry.expires_at

    def invalidate(self, business_id: str | None = None) -> None:
        """Drop one business (and the directory), or everything when `business_id` is None."""
        if business_id is None:
            self._profiles.clear()
        else:
            self._profiles.pop(business_id, None)
        self._directory = None

    def __len__(self) -> int:
        """Number of cached profile entries (hits and misses)."""
        return len(self._profiles)

    async def get(self, business_id: str) -> Profile | None:
        entry = self._profiles.get(business_id)
        if entry is not None and self._fresh(entry):
            self._profiles.move_to_end(business_id)
            return entry.value
        profile = await self._inner.get(business_id)
        self._profiles[business_id] = _Cached(profile, self._clock.now() + self._ttl)
        self._profiles.move_to_end(business_id)
        while len(self._profiles) > self._max_entries:
            self._profiles.popitem(last=False)
        return profile

    async def get_live(self, business_id: str) -> Profile:
        try:
            profile = await self.get(business_id)
        except errors.ProfileInvalid:
            logger.warning("Stored profile %r failed validation; treating it as unknown", business_id)
            raise unknown_business(business_id) from None
        if profile is None or not profile.is_live:
            raise unknown_business(business_id)
        return profile

    async def put(self, profile: Profile, *, expected_revision: int | None) -> None:
        try:
            await self._inner.put(profile, expected_revision=expected_revision)
        finally:
            self.invalidate(profile.profile_id)

    async def list_live(self) -> Sequence[DirectoryEntry]:
        if self._directory is not None and self._fresh(self._directory):
            return list(self._directory.value)
        entries = list(await self._inner.list_live())
        self._directory = _Cached(entries, self._clock.now() + self._ttl)
        return list(entries)
