"""Composition root: memory and DynamoDB wiring. No AWS: clients are Stubber-wrapped or lazy fakes."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from botocore.stub import Stubber
from pydantic import SecretStr
from support import PROFILES_DIR, SCHEMA_PATH, TEST_KEY, pending

from mcp_tasks import InMemoryTaskStore
from mcp_tasks.dynamodb import DynamoTaskStore
from askonce_server.app import wiring
from askonce_server.app.links import OwnerLinkSigner
from askonce_server.app.notifier import OwnerWebNotifier
from askonce_server.app.wiring import LazyDynamoDBClient, build_deps
from askonce_server.clock import FixedClock
from askonce_server.config import Settings
from askonce_server.deps import AskOnceDeps


def dynamo_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "env": "test",
        "storage": "dynamodb",
        "dynamodb_table": "sf-main",
        "dynamodb_tasks_table": "sf-tasks",
        "profile_cache_ttl_s": 12.5,
        "profile_schema_path": SCHEMA_PATH,
        "profile_dirs": [tmp_path],
        "request_state_keys": [SecretStr(TEST_KEY)],
        "owner_link_keys": [SecretStr(TEST_KEY + "-links")],
        "id_key": SecretStr(TEST_KEY + "-ids"),
        **overrides,
    }
    return Settings(**values)


class Recorder:
    """Stands in for a store class: records its constructor arguments, does nothing else."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


@pytest.fixture
def recorded_stores(monkeypatch: pytest.MonkeyPatch) -> dict[str, type[Recorder]]:
    """Replace the profile/domain DynamoDB store classes (other work packages) with recorders."""
    names = ["DynamoProfileStore", "CachingProfileStore", "DynamoCommitmentStore", "DynamoCapacityStore",
             "DynamoIdempotencyStore"]
    classes = {name: type(name, (Recorder,), {}) for name in names}
    for name, cls in classes.items():
        monkeypatch.setattr(wiring, name, cls)
    return classes


# --------------------------------------------------------------------------- DynamoDB wiring


def test_dynamo_wiring_shares_one_client_and_makes_no_calls(
    tmp_path: Path, clock: FixedClock, stubbed_dynamodb: tuple[Any, Stubber], recorded_stores: dict[str, type]
) -> None:
    client, _stub = stubbed_dynamodb  # no responses queued: any API call would raise
    settings = dynamo_settings(tmp_path, strict_invariants=True)
    deps, signer = build_deps(settings, clock=clock, dynamodb_client=client)

    assert isinstance(deps, AskOnceDeps) and isinstance(signer, OwnerLinkSigner)
    caching = deps.profiles
    assert isinstance(caching, recorded_stores["CachingProfileStore"])
    inner = caching.args[0]
    assert isinstance(inner, recorded_stores["DynamoProfileStore"])
    assert inner.args == (client, "sf-main")
    assert inner.kwargs["schema_path"] == SCHEMA_PATH and inner.kwargs["strict"] is True
    assert caching.kwargs == {"ttl_s": 12.5, "clock": clock}

    assert deps.commitments.args == (client, "sf-main") and deps.commitments.kwargs == {"clock": clock}
    assert deps.capacity.args == (client, "sf-main")
    assert deps.idempotency.args == (client, "sf-main") and deps.idempotency.kwargs == {"clock": clock}

    task_store = deps.task_manager.store
    assert isinstance(task_store, DynamoTaskStore)
    assert task_store._ddb is client and task_store._table == "sf-tasks"
    assert deps.tasks.manager is deps.task_manager
    assert deps.task_manager.default_ttl_ms == settings.task_default_ttl_ms
    assert deps.task_manager.default_poll_interval_ms == settings.task_poll_interval_ms

    assert deps.owner.profiles is deps.profiles and deps.owner.tasks is deps.task_manager
    domain = deps.bookings.ctx
    assert domain.commitments is deps.commitments and domain.capacity is deps.capacity
    assert isinstance(domain.notifier, OwnerWebNotifier)
    assert domain.idempotency_ttl.total_seconds() == settings.idempotency_ttl_s
    assert domain.keep_waiting_max.days == settings.keep_waiting_max_days
    assert deps.orders.ctx is domain and deps.quotes.ctx is domain and deps.owner.ctx is domain


def test_dynamo_wiring_defaults_to_a_lazy_client(
    tmp_path: Path, clock: FixedClock, recorded_stores: dict[str, type]
) -> None:
    settings = dynamo_settings(tmp_path, dynamodb_endpoint_url="http://localhost:8001")
    deps, _ = build_deps(settings, clock=clock)
    client = deps.commitments.args[0]
    assert isinstance(client, LazyDynamoDBClient) and not client.created
    assert deps.task_manager.store._ddb is client
    assert deps.profiles.args[0].args[0] is client
    assert (client._region_name, client._endpoint_url) == ("ap-southeast-2", "http://localhost:8001")


def test_profiles_cannot_seed_dynamodb(tmp_path: Path, clock: FixedClock, recorded_stores: dict[str, type]) -> None:
    with pytest.raises(ValueError, match="memory storage"):
        build_deps(dynamo_settings(tmp_path), clock=clock, profiles=[])


def test_dynamo_wiring_with_the_real_stores_makes_no_calls(
    tmp_path: Path, clock: FixedClock, stubbed_dynamodb: tuple[Any, Stubber]
) -> None:
    from askonce_server.domain.stores.dynamodb import DynamoCommitmentStore
    from askonce_server.profile.store import CachingProfileStore

    client, _stub = stubbed_dynamodb
    deps, _ = pending(
        "profile/domain", lambda: build_deps(dynamo_settings(tmp_path), clock=clock, dynamodb_client=client)
    )
    assert isinstance(deps.profiles, CachingProfileStore)
    assert isinstance(deps.commitments, DynamoCommitmentStore)
    assert isinstance(deps.task_manager.store, DynamoTaskStore)


# --------------------------------------------------------------------------- lazy client


def test_lazy_client_is_created_once_on_first_use() -> None:
    created: list[dict[str, Any]] = []

    class FakeClient:
        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            return {"Item": kwargs}

    def factory(**kwargs: Any) -> FakeClient:
        created.append(kwargs)
        return FakeClient()

    lazy = LazyDynamoDBClient(region_name="ap-southeast-2", endpoint_url=None, factory=factory)
    assert created == [] and not lazy.created
    with pytest.raises(AttributeError):
        lazy._private  # noqa: B018 - private names never trigger creation
    assert created == []
    assert lazy.get_item(Key=1) == {"Item": {"Key": 1}}
    assert lazy.get_item(Key=2) == {"Item": {"Key": 2}}
    assert created == [{"region_name": "ap-southeast-2", "endpoint_url": None}] and lazy.created


def test_lazy_client_creation_is_thread_safe() -> None:
    calls = 0
    gate = threading.Event()

    def factory(**_kwargs: Any) -> object:
        nonlocal calls
        calls += 1
        gate.wait(0.05)
        return object()

    lazy = LazyDynamoDBClient(region_name="ap-southeast-2", factory=factory)
    threads = [threading.Thread(target=lazy.client) for _ in range(8)]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join()
    assert calls == 1


def test_lazy_client_builds_a_regional_boto3_client_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit env credentials: botocore resolves them first, so no metadata endpoint is ever queried.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    lazy = LazyDynamoDBClient(region_name="ap-southeast-2", endpoint_url="http://localhost:8001")
    assert lazy.meta.region_name == "ap-southeast-2"
    assert lazy.meta.endpoint_url == "http://localhost:8001"
    assert lazy.meta.service_model.service_name == "dynamodb"


# --------------------------------------------------------------------------- memory wiring


def test_memory_wiring(deps: Any, settings: Settings, signer: Any) -> None:
    assert deps.settings is settings
    assert isinstance(deps.task_manager.store, InMemoryTaskStore)
    assert deps.owner.profiles is deps.profiles and deps.owner.tasks is deps.task_manager
    assert deps.bookings.ctx.commitments is deps.commitments is deps.owner.ctx.commitments
    assert isinstance(deps.bookings.ctx.notifier, OwnerWebNotifier)
    assert isinstance(signer, OwnerLinkSigner)


@pytest.mark.anyio
async def test_memory_profiles_load_from_profile_dirs(tmp_path: Path, clock: FixedClock) -> None:
    import json

    settings = Settings(
        env="test",
        profile_dirs=[PROFILES_DIR],
        fallback_profile=None,
        profile_schema_path=SCHEMA_PATH,
        request_state_keys=[SecretStr(TEST_KEY)],
        owner_link_keys=[SecretStr(TEST_KEY + "-links")],
        id_key=SecretStr(TEST_KEY + "-ids"),
    )
    deps, _ = pending("profile/domain", lambda: build_deps(settings, clock=clock))
    expected = {json.loads(p.read_text())["profile_id"] for p in sorted(PROFILES_DIR.glob("*.json"))}
    for business_id in expected:
        assert await deps.profiles.get(business_id) is not None, business_id
