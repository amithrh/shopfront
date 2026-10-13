"""Fixtures for the domain work package tests.

Profiles are the gold documents (`profiles/*.json`) promoted to live and wrapped with
`Profile.unchecked`, so these tests exercise real profile lookups without depending on the loader's
strictness. `raw` hands out mutable copies for tests that build synthetic variants (DST, fixed
sessions, buffers); `build_profile` wraps such a copy.

Domain collaborators are real (memory stores, `AvailabilityEngine`, `IdFactory`, `FixedClock`), except
the owner notifier and the decision renderer, which are small recording fakes (the real ones belong to
the app and tools work packages).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import mcp_types as types
import pytest
from support import TEST_KEY

from mcp_tasks import InMemoryTaskStore, TaskManager
from askonce_server.clock import FixedClock
from askonce_server.domain.availability import AvailabilityEngine
from askonce_server.domain.models import Commitment
from askonce_server.domain.services import (
    BookingService,
    CustomOrderService,
    DomainContext,
    OwnerService,
    QuoteService,
)
from askonce_server.domain.stores.memory import (
    InMemoryCapacityStore,
    InMemoryCommitmentStore,
    InMemoryIdempotencyStore,
    MemoryState,
)
from askonce_server.errors import UnknownBusiness
from askonce_server.ids import IdFactory
from askonce_server.profile.model import Profile

BAKERY = "thimbleberry-bakehouse"
SALON = "quillfern-salon"
HOME = "kettlewick-home"


@dataclass
class RecordingNotifier:
    """OwnerNotifier fake: records (business_id, commitment_id); can be told to fail."""

    calls: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    async def notify(self, profile: Profile, commitment: Commitment) -> None:
        self.calls.append((profile.profile_id, commitment.commitment_id))
        if self.fail:
            raise RuntimeError("notification channel down")


@dataclass
class FakeRenderer:
    """DecisionRenderer fake: a minimal structured result carrying the commitment status."""

    rendered: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False

    def render(self, profile: Profile, commitment: Commitment) -> types.CallToolResult:
        if self.fail:
            raise RuntimeError("renderer bug")
        self.rendered.append((commitment.commitment_id, commitment.status))
        payload = {"status": commitment.status, "reference": commitment.commitment_id}
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(payload))], structured_content=payload
        )


class FakeProfileStore:
    """ProfileStore fake over a dict (only the reads the domain uses)."""

    def __init__(self, profiles: list[Profile]) -> None:
        self._by_id = {p.profile_id: p for p in profiles}

    async def get(self, business_id: str) -> Profile | None:
        return self._by_id.get(business_id)

    async def get_live(self, business_id: str) -> Profile:
        found = self._by_id.get(business_id)
        if found is None or not found.is_live:
            raise UnknownBusiness("Unknown business")
        return found

    def replace(self, profile: Profile) -> None:
        self._by_id[profile.profile_id] = profile


@dataclass
class Domain:
    """Everything a domain test needs, wired like `app.wiring` does for memory storage."""

    clock: FixedClock
    state: MemoryState
    ctx: DomainContext
    notifier: RecordingNotifier
    renderer: FakeRenderer
    tasks: TaskManager
    profiles: FakeProfileStore
    bookings: BookingService
    orders: CustomOrderService
    quotes: QuoteService
    owner: OwnerService

    def capacity_used(self, prefix: str = "") -> dict[str, int]:
        return {k: v for k, v in self.state.capacity.items() if k.startswith(prefix) and v}


@pytest.fixture
def raw(live_raw_profiles: dict[str, dict[str, Any]]) -> Callable[[str], dict[str, Any]]:
    """`raw("quillfern-salon")` -> a mutable deep copy of the live gold document."""

    def get(profile_id: str) -> dict[str, Any]:
        if profile_id not in live_raw_profiles:
            pytest.skip(f"gold profile {profile_id} not present")
        return copy.deepcopy(live_raw_profiles[profile_id])

    return get


@pytest.fixture
def build_profile() -> Callable[[dict[str, Any]], Profile]:
    return Profile.unchecked


@pytest.fixture
def bakery(raw: Callable[[str], dict[str, Any]]) -> Profile:
    return Profile.unchecked(raw(BAKERY))


@pytest.fixture
def salon(raw: Callable[[str], dict[str, Any]]) -> Profile:
    return Profile.unchecked(raw(SALON))


@pytest.fixture
def home(raw: Callable[[str], dict[str, Any]]) -> Profile:
    return Profile.unchecked(raw(HOME))


@pytest.fixture
def engine(clock: FixedClock) -> AvailabilityEngine:
    return AvailabilityEngine(clock)


@pytest.fixture
def domain(clock: FixedClock, bakery: Profile, salon: Profile, home: Profile) -> Domain:
    state = MemoryState()
    notifier = RecordingNotifier()
    renderer = FakeRenderer()
    ctx = DomainContext(
        commitments=InMemoryCommitmentStore(state, clock=clock),
        capacity=InMemoryCapacityStore(state),
        idempotency=InMemoryIdempotencyStore(state, clock=clock),
        availability=AvailabilityEngine(clock),
        ids=IdFactory((TEST_KEY + "-ids").encode()),
        clock=clock,
        notifier=notifier,
        idempotency_ttl=timedelta(hours=24),
    )
    tasks = TaskManager(InMemoryTaskStore(), default_ttl_ms=7 * 24 * 3600 * 1000, default_poll_interval_ms=20)
    profiles = FakeProfileStore([bakery, salon, home])
    return Domain(
        clock=clock,
        state=state,
        ctx=ctx,
        notifier=notifier,
        renderer=renderer,
        tasks=tasks,
        profiles=profiles,
        bookings=BookingService(ctx),
        orders=CustomOrderService(ctx),
        quotes=QuoteService(ctx),
        owner=OwnerService(ctx, tasks=tasks, renderer=renderer, profiles=profiles),
    )
