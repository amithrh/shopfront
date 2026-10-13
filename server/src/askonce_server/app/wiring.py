"""Composition root: build every collaborator from `Settings`.

    deps, signer = build_deps(settings)                # storage per settings.storage
    deps, signer = build_deps(settings, clock=FixedClock(...), profiles=[...])   # tests

Memory storage: InMemoryProfileStore loaded from `settings.profile_dirs` (fallback example profile),
MemoryState-backed domain stores, InMemoryTaskStore. DynamoDB storage: one boto3 client
(`region_name=settings.aws_region`, optional `endpoint_url`) shared by DynamoProfileStore (wrapped in
CachingProfileStore), the domain Dynamo stores (single table `settings.dynamodb_table`) and
`mcp_tasks.dynamodb.DynamoTaskStore` (table `settings.dynamodb_tasks_table`). No AWS call happens at
construction time: an injected client is only stored, and the default client is a `LazyDynamoDBClient`
that creates the boto3 client (and resolves credentials) on first use.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from mcp_tasks import InMemoryTaskStore, TaskManager, TasksExtension
from mcp_tasks.dynamodb import DynamoTaskStore
from mcp_tasks.store import TaskStore
from askonce_server.app.links import OwnerLinkSigner, signer_from_settings
from askonce_server.app.notifier import OwnerWebNotifier
from askonce_server.clock import Clock, SystemClock
from askonce_server.config import Settings
from askonce_server.deps import AskOnceDeps
from askonce_server.domain.availability import AvailabilityEngine
from askonce_server.domain.ports import CapacityStore, CommitmentStore, IdempotencyStore
from askonce_server.domain.services import (
    BookingService,
    CustomOrderService,
    DomainContext,
    OwnerService,
    QuoteService,
)
from askonce_server.domain.stores.dynamodb import DynamoCapacityStore, DynamoCommitmentStore, DynamoIdempotencyStore
from askonce_server.domain.stores.memory import (
    InMemoryCapacityStore,
    InMemoryCommitmentStore,
    InMemoryIdempotencyStore,
    MemoryState,
)
from askonce_server.ids import IdFactory
from askonce_server.profile.dynamodb import DynamoProfileStore
from askonce_server.profile.model import Profile
from askonce_server.profile.snapshots import DirectorySnapshotProvider, SnapshotProvider
from askonce_server.profile.store import CachingProfileStore, InMemoryProfileStore, ProfileStore, load_profile_dir
from askonce_server.tools.guard import TaskViewGuard
from askonce_server.tools.render import CommitmentRenderer


@dataclass(frozen=True)
class Stores:
    """One implementation per storage port (all memory, or all DynamoDB)."""

    profiles: ProfileStore
    commitments: CommitmentStore
    capacity: CapacityStore
    idempotency: IdempotencyStore
    tasks: TaskStore


class LazyDynamoDBClient:
    """A boto3 DynamoDB client created on first attribute access (thread-safe).

    `boto3.client(...)` resolves credentials when it is built (which may query the instance-metadata
    endpoint), so the composition root defers it: building the app makes no network call at all.
    Attribute access is forwarded to the real client (`get_item`, `exceptions`, `meta`, ...).
    """

    def __init__(
        self,
        *,
        region_name: str,
        endpoint_url: str | None = None,
        factory: Callable[..., Any] | None = None,
    ) -> None:
        self._region_name = region_name
        self._endpoint_url = endpoint_url
        self._factory = factory
        self._client: Any | None = None
        self._lock = threading.Lock()

    @property
    def created(self) -> bool:
        """True once the underlying boto3 client exists."""
        return self._client is not None

    def _create(self) -> Any:
        if self._factory is not None:
            return self._factory(region_name=self._region_name, endpoint_url=self._endpoint_url)
        import boto3  # deferred: only DynamoDB deployments pay for it

        session = boto3.session.Session()  # boto3's default session is not thread-safe
        return session.client("dynamodb", region_name=self._region_name, endpoint_url=self._endpoint_url)

    def client(self) -> Any:
        """The real client, created on first call."""
        if self._client is None:
            with self._lock:
                if self._client is None:
                    self._client = self._create()
        return self._client

    def __getattr__(self, name: str) -> Any:
        # Only called for names not found normally. Private and dunder names are never forwarded, so
        # copy/pickle probes and half-initialised instances cannot trigger client creation.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.client(), name)


def snapshot_provider(settings: Settings) -> SnapshotProvider | None:
    return DirectorySnapshotProvider(settings.profile_snapshot_dir) if settings.profile_snapshot_dir else None


def build_memory_profile_store(settings: Settings, profiles: Sequence[Profile] | None = None) -> ProfileStore:
    """In-memory store seeded with `profiles`, or with the files in `settings.profile_dirs`."""
    if profiles is None:
        profiles = load_profile_dir(
            settings.profile_dirs,
            schema_path=settings.profile_schema_path,
            fallback=settings.fallback_profile,
            snapshots=snapshot_provider(settings),
            strict=settings.invariants_strict,
        )
    return InMemoryProfileStore(profiles)


def dynamodb_client_for(settings: Settings) -> LazyDynamoDBClient:
    """The default (lazy) DynamoDB client for `settings` (region and optional endpoint override)."""
    return LazyDynamoDBClient(region_name=settings.aws_region, endpoint_url=settings.dynamodb_endpoint_url)


def _memory_stores(settings: Settings, *, clock: Clock, profiles: Sequence[Profile] | None) -> Stores:
    state = MemoryState()
    return Stores(
        profiles=build_memory_profile_store(settings, profiles),
        commitments=InMemoryCommitmentStore(state, clock=clock),
        capacity=InMemoryCapacityStore(state),
        idempotency=InMemoryIdempotencyStore(state, clock=clock),
        tasks=InMemoryTaskStore(),
    )


def _dynamo_stores(settings: Settings, *, clock: Clock, client: Any) -> Stores:
    """Every DynamoDB store over one shared client. Constructors only store the client (no calls)."""
    table = settings.dynamodb_table
    profiles = DynamoProfileStore(
        client,
        table,
        schema_path=settings.profile_schema_path,
        snapshots=snapshot_provider(settings),
        strict=settings.invariants_strict,
    )
    return Stores(
        profiles=CachingProfileStore(profiles, ttl_s=settings.profile_cache_ttl_s, clock=clock),
        commitments=DynamoCommitmentStore(client, table, clock=clock),
        capacity=DynamoCapacityStore(client, table),
        idempotency=DynamoIdempotencyStore(client, table, clock=clock),
        tasks=DynamoTaskStore(client, settings.dynamodb_tasks_table),
    )


def _assemble(
    settings: Settings, *, clock: Clock, ids: IdFactory, notifier: OwnerWebNotifier, stores: Stores
) -> AskOnceDeps:
    """Services and the tasks extension over `stores` (identical for every storage backend)."""
    task_manager = TaskManager(
        stores.tasks,
        default_ttl_ms=settings.task_default_ttl_ms,
        default_poll_interval_ms=settings.task_poll_interval_ms,
        now=clock.now,  # one time source: task timestamps and TTL expiry follow the injected clock
    )
    domain = DomainContext(
        commitments=stores.commitments,
        capacity=stores.capacity,
        idempotency=stores.idempotency,
        availability=AvailabilityEngine(clock),
        ids=ids,
        clock=clock,
        notifier=notifier,
        idempotency_ttl=timedelta(seconds=settings.idempotency_ttl_s),
        keep_waiting_max=timedelta(days=settings.keep_waiting_max_days),
    )
    owner = OwnerService(domain, tasks=task_manager, renderer=CommitmentRenderer(), profiles=stores.profiles)
    # No MCP auth yet: every caller is the 'anonymous' principal, so tasks/list would hand any client
    # every customer's task ids (the only capability guarding a task). It stays off. A customer's
    # tasks/cancel withdraws the request at once (holds released) through the owner service, and a
    # customer's tasks/update answer to the owner's question is stored on the request at once. Every
    # task object sent to a client (handle, tasks/get) is re-checked against the business's current
    # injection canaries (`TaskViewGuard`), as tool results are by the output middleware.
    tasks = TasksExtension(
        task_manager,
        serve_list=False,
        on_cancel=owner.on_task_cancelled,
        on_input=owner.on_task_input,
        present=TaskViewGuard(stores.profiles),
    )
    return AskOnceDeps(
        settings=settings,
        clock=clock,
        ids=ids,
        profiles=stores.profiles,
        commitments=stores.commitments,
        capacity=stores.capacity,
        idempotency=stores.idempotency,
        task_manager=task_manager,
        tasks=tasks,
        bookings=BookingService(domain),
        orders=CustomOrderService(domain),
        quotes=QuoteService(domain),
        owner=owner,
    )


def build_deps(
    settings: Settings,
    *,
    clock: Clock | None = None,
    profiles: Sequence[Profile] | None = None,
    dynamodb_client: Any | None = None,
) -> tuple[AskOnceDeps, OwnerLinkSigner]:
    """Wire everything. Returns the deps plus the owner link signer (used by the owner API).

    `profiles` seeds the in-memory profile store (memory storage only: DynamoDB profiles are published
    by onboarding). `dynamodb_client` injects a client (e.g. a botocore Stubber-wrapped one) for
    DynamoDB storage; by default a `LazyDynamoDBClient` is used.
    """
    clock = clock or SystemClock()
    ids = IdFactory(settings.id_key_bytes())
    signer = signer_from_settings(settings, clock=clock)
    notifier = OwnerWebNotifier(signer)

    if settings.storage == "dynamodb":
        if profiles is not None:
            raise ValueError("profiles can only seed memory storage; DynamoDB profiles are published by onboarding")
        return _build_dynamo_deps(
            settings, clock=clock, ids=ids, signer=signer, notifier=notifier, client=dynamodb_client
        )

    stores = _memory_stores(settings, clock=clock, profiles=profiles)
    return _assemble(settings, clock=clock, ids=ids, notifier=notifier, stores=stores), signer


def _build_dynamo_deps(
    settings: Settings,
    *,
    clock: Clock,
    ids: IdFactory,
    signer: OwnerLinkSigner,
    notifier: OwnerWebNotifier,
    client: Any | None,
) -> tuple[AskOnceDeps, OwnerLinkSigner]:
    """DynamoDB wiring (see module docstring). `client=None` builds a boto3 client lazily (no calls)."""
    if client is None:
        client = dynamodb_client_for(settings)
    stores = _dynamo_stores(settings, clock=clock, client=client)
    return _assemble(settings, clock=clock, ids=ids, notifier=notifier, stores=stores), signer
