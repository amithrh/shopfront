"""Shared fixtures for the AskOnce server test suite.

Layering (each fixture only depends on the ones above it):

    raw_profiles      gold profiles from /profiles/*.json (fallback: schema/example-profile.json), as dicts
    live_raw_profiles the same documents promoted to `live` with a passed run bound to their sha256
    clock / settings  FixedClock at FIXED_NOW, test Settings (memory storage, no files, lenient invariants)
    profiles          `Profile` objects loaded through the real loader             [profile WP]
    deps, signer      `app.wiring.build_deps` over memory stores                  [domain + app WPs]
    mcp_server        `tools.server.build_mcp_server(deps)`                       [tools WP]
    client            in-process `mcp.Client` (protocol 2026-07-28) with the tasks + apps extensions
                      and a scripted elicitation callback
    legacy_client     the same over the 2025-11-25 handshake
    http_app          `app.main.create_app(settings, deps=deps)` under FastAPI's TestClient  [app WP]

Fixtures whose implementation is still a skeleton raise NotImplementedError; `support.pending` turns that into a
skip naming the work package, so the suite always collects and runs what is ready.

No AWS: DynamoDB code is tested with botocore Stubber clients built by `stubbed_dynamodb`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from support import (
    FIXED_NOW,
    SCHEMA_PATH,
    TEST_KEY,
    ElicitationScript,
    pending,
    apps_advertisement,
    load_raw_profiles,
    make_live,
)

from askonce_server.clock import FixedClock
from askonce_server.config import Settings


# --------------------------------------------------------------------------- anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def raw_profiles() -> dict[str, dict[str, Any]]:
    return load_raw_profiles()


@pytest.fixture(scope="session")
def live_raw_profiles(raw_profiles: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {pid: make_live(doc) for pid, doc in raw_profiles.items()}


# --------------------------------------------------------------------------- settings & clock


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(FIXED_NOW)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        env="test",
        storage="memory",
        profile_dirs=[tmp_path / "no-profiles"],
        fallback_profile=None,
        profile_schema_path=SCHEMA_PATH,
        request_state_keys=[TEST_KEY],
        owner_link_keys=[TEST_KEY + "-links"],
        id_key=TEST_KEY + "-ids",
        public_base_url="http://testserver",
        allowed_hosts=["testserver", "localhost:*", "127.0.0.1:*"],
        task_poll_interval_ms=20,
        strict_invariants=False,
    )


# --------------------------------------------------------------------------- domain objects


@pytest.fixture
def profiles(live_raw_profiles: dict[str, dict[str, Any]]) -> list[Any]:
    """Live `Profile` objects for every gold profile (through the real loader, lenient mode)."""

    def build() -> list[Any]:
        from askonce_server.profile.loader import load_profile

        return [
            load_profile(doc, schema_path=SCHEMA_PATH, strict=False).profile for doc in live_raw_profiles.values()
        ]

    return pending("profile", build)


@pytest.fixture
def deps_and_signer(settings: Settings, clock: FixedClock, profiles: list[Any]) -> tuple[Any, Any]:
    def build() -> tuple[Any, Any]:
        from askonce_server.app.wiring import build_deps

        return build_deps(settings, clock=clock, profiles=profiles)

    return pending("profile/domain/app", build)


@pytest.fixture
def deps(deps_and_signer: tuple[Any, Any]) -> Any:
    return deps_and_signer[0]


@pytest.fixture
def signer(deps_and_signer: tuple[Any, Any]) -> Any:
    return deps_and_signer[1]


# --------------------------------------------------------------------------- MCP clients


@pytest.fixture
def elicitation() -> ElicitationScript:
    return ElicitationScript(intake={"customer_name": "Sam Lee", "customer_phone": "+12175550100"})


@pytest.fixture
def mcp_server(deps: Any) -> Any:
    def build() -> Any:
        from askonce_server.tools.server import build_mcp_server

        return build_mcp_server(deps)

    return pending("tools", build)


@pytest.fixture
async def client(mcp_server: Any, elicitation: ElicitationScript) -> AsyncIterator[Any]:
    """2026-07-28 in-process client with tasks + apps extensions and scripted elicitation."""
    from mcp import Client
    from mcp_tasks.client import TasksClientExtension

    async with Client(
        mcp_server,
        extensions=[TasksClientExtension(default_poll_interval_s=0.02, max_wait_s=10), apps_advertisement()],
        elicitation_callback=elicitation,
        cache=None,
    ) as c:
        yield c


@pytest.fixture
async def plain_client(mcp_server: Any, elicitation: ElicitationScript) -> AsyncIterator[Any]:
    """2026-07-28 client WITHOUT the tasks extension (exercises the get_task fallback)."""
    from mcp import Client

    async with Client(mcp_server, elicitation_callback=elicitation, cache=None) as c:
        yield c


@pytest.fixture
async def legacy_client(mcp_server: Any, elicitation: ElicitationScript) -> AsyncIterator[Any]:
    """2025-11-25 handshake client (synchronous elicitation/create path)."""
    from mcp import Client

    async with Client(mcp_server, mode="legacy", elicitation_callback=elicitation, cache=None) as c:
        yield c


# --------------------------------------------------------------------------- HTTP app


@pytest.fixture
def http_app(settings: Settings, deps: Any) -> Iterator[Any]:
    """FastAPI TestClient around `create_app` (runs the lifespan, so /mcp works)."""

    def build() -> Any:
        from askonce_server.app.main import create_app

        return create_app(settings, deps=deps)

    app = pending("app", build)
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="http://testserver") as test_client:
        yield test_client


# --------------------------------------------------------------------------- DynamoDB (offline)


@pytest.fixture
def stubbed_dynamodb() -> Iterator[tuple[Any, Any]]:
    """`(client, Stubber)` for ap-southeast-2 with dummy credentials. Never reaches AWS."""
    import boto3
    from botocore.stub import Stubber

    ddb = boto3.client(
        "dynamodb", region_name="ap-southeast-2", aws_access_key_id="test", aws_secret_access_key="test"
    )
    with Stubber(ddb) as stub:
        yield ddb, stub
        stub.assert_no_pending_responses()
