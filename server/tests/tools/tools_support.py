"""Helpers for the tools work package tests (imported by the tests and by `conftest.py`).

Most tests use the shared fixtures from `tests/conftest.py` (`deps`, `client`, `plain_client`,
`legacy_client`, `elicitation`). The helpers here add what those cannot express:

  * `Stack`: an MCP server + deps over a *modified* copy of the gold profiles (an open contradiction
    so a fact is withheld, an injected canary so the output guard must fire), built with the real
    profile loader / `Profile.unchecked` and the real `app.wiring.build_deps`;
  * `RoundCounter`: server middleware counting `tools/call` requests (MRTR rounds);
  * `assert_grounded`: every `speak` passes `check_spoken`, every fact ref parses and (for profile
    facts) is statable in the profile that served it.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from support import FIXED_NOW, SCHEMA_PATH, TEST_KEY, ElicitationScript, apps_advertisement

from mcp import Client
from askonce_server.app.wiring import build_deps
from askonce_server.clock import FixedClock
from askonce_server.config import Settings
from askonce_server.deps import AskOnceDeps
from askonce_server.profile.factref import FactRef
from askonce_server.profile.loader import load_profile
from askonce_server.profile.model import Profile
from askonce_server.tools import grounding
from askonce_server.tools.render import COMMITMENT_URN, OWNER_ANSWER_URN
from askonce_server.tools.schemas import check_spoken
from askonce_server.tools.server import build_mcp_server

BAKERY = "thimbleberry-bakehouse"
SALON = "quillfern-salon"
HOME = "kettlewick-home"

INTAKE = {
    "customer_name": "Sam Lee",
    "customer_phone": "+12175550100",
    "customer_email": "sam@example.com",
    "stylist": "any",
    "name": "Sam Lee",
    "contact_by": "text",
    "phone": "+13035550100",
    "time_window": "weekday-morning",
    "job_type": "leak-repair",
    "description": "The pipe under the kitchen sink drips.",
    "zip": "80211",
}
"""Answers for every required intake field across the three gold profiles."""

SMALL_CAKE = {
    "business_id": BAKERY,
    "item": "custom cake",
    "choices": {"size": "six inch round", "flavor": "vanilla bean", "filling": "lemon curd", "frosting": "vanilla buttercream"},
    "date": "2026-10-18",
    "details": {"servings": 10, "customer_name": "Sam Lee", "customer_phone": "+12175550100"},
}
"""A custom cake under every approval rule: confirmed without the owner."""

BIG_CAKE = {
    **SMALL_CAKE,
    "choices": {**SMALL_CAKE["choices"], "size": "ten inch round"},
    "details": {**SMALL_CAKE["details"], "servings": 30},
}
"""30 servings and a 10-inch round: matches both approval rules, so it becomes an owner task."""


def settings_for(tmp_path: Any) -> Settings:
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
        task_poll_interval_ms=20,
        strict_invariants=False,
    )


@dataclass
class RoundCounter:
    """Server middleware counting `tools/call` requests per tool name."""

    calls: dict[str, int] = field(default_factory=dict)

    async def __call__(self, ctx: Any, call_next: Callable[[Any], Any]) -> Any:
        if ctx.method == "tools/call":
            name = str((ctx.params or {}).get("name"))
            self.calls[name] = self.calls.get(name, 0) + 1
        return await call_next(ctx)

    def reset(self) -> None:
        self.calls.clear()


@dataclass
class Stack:
    """A wired server over a given set of profiles, plus helpers to open clients on it."""

    deps: AskOnceDeps
    server: Any
    counter: RoundCounter

    @asynccontextmanager
    async def client(
        self, elicitation: ElicitationScript, *, mode: str = "plain"
    ) -> AsyncIterator[Client]:
        """`plain` (2026-07-28, no tasks extension), `tasks` (with it) or `legacy` (2025-11-25)."""
        kwargs: dict[str, Any] = {"elicitation_callback": elicitation, "cache": None}
        if mode == "legacy":
            kwargs["mode"] = "legacy"
        elif mode == "tasks":
            from mcp_tasks.client import TasksClientExtension

            kwargs["extensions"] = [
                TasksClientExtension(default_poll_interval_s=0.02, max_wait_s=10),
                apps_advertisement(),
            ]
        async with Client(self.server, **kwargs) as client:
            yield client


def build_stack(tmp_path: Any, profiles: list[Profile]) -> Stack:
    deps, _signer = build_deps(settings_for(tmp_path), clock=FixedClock(FIXED_NOW), profiles=profiles)
    server = build_mcp_server(deps)
    counter = RoundCounter()
    server.middleware.append(counter)
    return Stack(deps=deps, server=server, counter=counter)


def loaded(doc: dict[str, Any]) -> Profile:
    return load_profile(doc, schema_path=SCHEMA_PATH, strict=False).profile


def reopen_question(raw: dict[str, Any], question_id: str) -> dict[str, Any]:
    """A copy of `raw` (not yet live) where `question_id` is open again (its fact becomes disputed)."""
    doc = copy.deepcopy(raw)
    for question in doc["open_questions"]:
        if question["id"] == question_id:
            question["status"] = "open"
            question.pop("answer", None)
    return doc


CANARY = "ROSAFREE"


# ---------------------------------------------------------------------------- assertions


def structured(result: Any) -> dict[str, Any]:
    assert result.is_error is False, result.content
    assert result.structured_content is not None, result
    return dict(result.structured_content)


def strings_of(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings_of(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in strings_of(v)]
    return []


async def assert_grounded(deps: AskOnceDeps, data: dict[str, Any]) -> None:
    """`speak` is spoken-safe; every fact parses; every profile fact is statable where it came from."""
    check_spoken(data["speak"])
    business_ids = [data["business_id"]] if data.get("business_id") else []
    business_ids += [b["business_id"] for b in data.get("businesses") or ()]
    profiles = [p for p in [await deps.profiles.get(b) for b in business_ids] if p is not None]
    for fact in data.get("facts") or ():
        FactRef.parse(fact["fact_ref"])
        if str(fact.get("evidence_url") or "").startswith((OWNER_ANSWER_URN, COMMITMENT_URN)):
            continue  # the owner's own words, or a value fixed in the commitment: not profile facts
        assert profiles, f"fact {fact['fact_ref']} without a business"
        assert any(grounding.state(p, fact["fact_ref"]).ok for p in profiles), fact["fact_ref"]
