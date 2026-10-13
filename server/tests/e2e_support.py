"""A real Shopfront server for end-to-end tests: the full app under uvicorn on a free 127.0.0.1 port.

`running_shop(tmp_path, profiles, canaries)` wires the real `build_deps` (memory storage, fixed clock) and
`create_app`, serves it on its own thread and event loop, and yields a `Shop`: the base URL plus read-only
handles on the server's deps (the notifier outbox stands in for the owner's notification channel). On exit
it asserts that no injection canary reached any tool output the tests recorded in `Shop.outputs`.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from support import FIXED_NOW, SCHEMA_PATH, TEST_KEY, bind_localhost, serve

from shopfront_server.app.links import OwnerLinkSigner
from shopfront_server.app.main import create_app
from shopfront_server.app.notifier import OwnerWebNotifier
from shopfront_server.app.wiring import build_deps
from shopfront_server.clock import FixedClock
from shopfront_server.config import Settings
from shopfront_server.deps import ShopfrontDeps
from shopfront_server.profile.loader import load_profile
from shopfront_server.profile.model import Profile

BAKERY = "thimbleberry-bakehouse"
SALON = "quillfern-salon"
HOME = "kettlewick-home"
DRAFT = "thimbleberry-bakehouse-preview"
"""A draft (non-live) copy of the bakery that is loaded into the store but must never be discoverable."""

ADMIN_TOKEN = "test-only-admin-token-0123456789abcdef"
"""Bearer token for the admin link-minting route of e2e servers (test value)."""


@dataclass
class Shop:
    """One running server: its base URL, its (thread-owned) deps, and what every client saw."""

    url: str
    deps: ShopfrontDeps
    settings: Settings
    signer: OwnerLinkSigner
    canaries: list[str]
    outputs: list[str] = field(default_factory=list)

    @property
    def notifier(self) -> OwnerWebNotifier:
        """The owner_web notifier: its outbox is the owner's notification channel (read-only here)."""
        notifier = self.deps.orders.ctx.notifier
        assert isinstance(notifier, OwnerWebNotifier)
        return notifier

    def leaked_canaries(self) -> list[str]:
        everything = "\n".join(self.outputs).casefold()
        return [c for c in self.canaries if c.casefold() in everything]


def shop_settings(tmp_path: Path, port: int) -> Settings:
    return Settings(
        env="test",
        storage="memory",
        profile_dirs=[tmp_path / "no-profiles"],
        fallback_profile=None,
        profile_schema_path=SCHEMA_PATH,
        request_state_keys=[TEST_KEY],
        owner_link_keys=[TEST_KEY + "-links"],
        id_key=TEST_KEY + "-ids",
        public_base_url=f"http://127.0.0.1:{port}",  # the notifier's links point at this very server
        allowed_hosts=["127.0.0.1:*", "localhost:*"],
        task_poll_interval_ms=20,
        sweep_interval_s=0,  # the clock is fixed; nothing expires during a test
        admin_token=ADMIN_TOKEN,
        strict_invariants=False,
    )


def injection_canaries(raw_profiles: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(canary["quote"])
            for doc in raw_profiles.values()
            for source in (doc.get("sources") or {}).values()
            for canary in source.get("injection_canaries") or ()
            if canary.get("quote")
        }
    )


@contextmanager
def running_shop(tmp_path: Path, profiles: list[Profile], canaries: list[str]) -> Iterator[Shop]:
    sock = bind_localhost()
    settings = shop_settings(tmp_path, sock.getsockname()[1])
    deps, signer = build_deps(settings, clock=FixedClock(FIXED_NOW), profiles=profiles)
    with serve(create_app(settings, deps=deps, signer=signer), sock) as url:
        shop = Shop(url=url, deps=deps, settings=settings, signer=signer, canaries=canaries)
        yield shop
    leaked = shop.leaked_canaries()
    assert not leaked, f"injection canaries reached tool output: {leaked}"


def loaded(doc: dict[str, Any]) -> Profile:
    return load_profile(doc, schema_path=SCHEMA_PATH, strict=False).profile


def draft_copy(raw: dict[str, Any]) -> dict[str, Any]:
    doc = copy.deepcopy(raw)
    doc["profile_id"] = DRAFT
    doc["certification"] = {**doc.get("certification", {}), "status": "draft"}
    doc["certification"].pop("certified_run", None)
    return doc
