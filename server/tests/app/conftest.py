"""Fixtures for the app work package's tests (HTTP app, owner API, links, wiring, sweeper).

The app layer is tested against small fakes of the other work packages' collaborators (profile store,
commitment store, owner service) wired into a real `AskOnceDeps` with a real `TaskManager`, so these
tests run before the profile/domain/tools packages land. Tests that need the real collaborators use the
root conftest's fixtures (`deps`, `http_app`, ...), which skip until those packages are ready.

This module deliberately avoids `from __future__ import annotations`: the probe tool and its resolver
are registered on a real MCPServer, whose resolver machinery evaluates their annotations.
"""

import contextlib
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, cast

import httpx
import mcp_types as types
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field

from mcp.server.mcpserver import AcceptedElicitation, Elicit, ElicitationResult, MCPServer, Resolve
from mcp_tasks import InMemoryTaskStore, TaskManager, TasksExtension
from askonce_server.app.links import OwnerLinkSigner
from askonce_server.app.main import create_app
from askonce_server.clock import Clock, FixedClock
from askonce_server.config import Settings
from askonce_server.deps import AskOnceDeps
from askonce_server.domain.models import (
    DECISION_TO_STATUS,
    OPEN_STATUSES,
    Commitment,
    CommitmentKind,
    CommitmentStatus,
    OwnerDecision,
)
from askonce_server.domain.services.owner import ANSWER_FIELD, InboxItem, OwnerDecisionInput
from askonce_server.errors import InvalidDecision, InvalidState, NotFound, UnknownBusiness, UnknownModule
from askonce_server.ids import IdFactory
from askonce_server.tools.server import build_mcp_server
from support import serve

BAKERY = "thimbleberry-bakehouse"
SALON = "quillfern-salon"

CUSTOM_ORDER_APPROVAL: dict[str, Any] = {
    "module": "owner",
    "mode": "always",
    "decisions": ["approve", "decline", "counter_offer", "request_info"],
    "response_fields": [{"key": "final_price", "type": "money", "label": "Final price", "required": False}],
}


# --------------------------------------------------------------------------- fake profiles


class FakeProfile:
    """The slice of `Profile` the app layer reads: id, name and module blocks."""

    def __init__(self, profile_id: str, name: str, modules: dict[str, dict[str, Any]]) -> None:
        self.profile_id = profile_id
        self.name = name
        self.revision = 1
        self.is_live = True
        self._modules = modules

    def module(self, module_id: str) -> dict[str, Any]:
        try:
            return self._modules[module_id]
        except KeyError:
            raise UnknownModule(f"unknown module {module_id}") from None

    def injection_canaries(self) -> list[str]:
        return []


class FakeProfileStore:
    def __init__(self, profiles: Sequence[FakeProfile]) -> None:
        self._profiles = {p.profile_id: p for p in profiles}

    async def get(self, business_id: str) -> FakeProfile | None:
        return self._profiles.get(business_id)

    async def get_live(self, business_id: str) -> FakeProfile:
        profile = self._profiles.get(business_id)
        if profile is None:
            raise UnknownBusiness("Unknown business")
        return profile

    async def put(self, profile: Any, *, expected_revision: int | None) -> None:
        self._profiles[profile.profile_id] = profile

    async def list_live(self) -> list[Any]:
        return []


# --------------------------------------------------------------------------- fake commitments + owner


class FakeCommitmentStore:
    """Tenant-scoped dict store (the slice of `CommitmentStore` the owner API reads)."""

    def __init__(self) -> None:
        self.rows: dict[str, Commitment] = {}

    def put(self, commitment: Commitment) -> None:
        self.rows[commitment.commitment_id] = commitment

    async def get(self, business_id: str, commitment_id: str) -> Commitment | None:
        row = self.rows.get(commitment_id)
        return row if row is not None and row.business_id == business_id else None

    async def get_by_task(self, task_id: str) -> Commitment | None:
        return next((c for c in self.rows.values() if c.task_id == task_id), None)

    async def find_by_reference_prefix(self, business_id: str, prefix: str, *, limit: int = 5) -> list[Commitment]:
        rows = [c for c in self.rows.values() if c.business_id == business_id and c.commitment_id.startswith(prefix)]
        return sorted(rows, key=lambda c: c.commitment_id)[:limit]


class FakeOwnerService:
    """Stands in for the domain's OwnerService: approve/decline only, completes the task on decision."""

    ALLOWED = ("approve", "decline", "answer")

    def __init__(
        self, commitments: FakeCommitmentStore, tasks: TaskManager, clock: Clock, profiles: FakeProfileStore
    ) -> None:
        self.commitments = commitments
        self.tasks = tasks
        self.clock = clock
        self.profiles = profiles
        self.inbox_calls = 0
        self.item_calls = 0
        self.expire_calls = 0
        self.expire_errors: list[Exception] = []
        self.decisions: list[tuple[str, str, OwnerDecisionInput]] = []

    async def inbox(
        self, business_id: str, *, statuses: Any = None, limit: int = 50, cursor: str | None = None
    ) -> tuple[list[InboxItem], str | None]:
        rows = [
            c
            for c in self.commitments.rows.values()
            if c.business_id == business_id and (statuses is None or c.status in statuses)
        ]
        rows.sort(key=lambda c: c.created_at, reverse=True)
        self.inbox_calls += 1
        profile = await self.profiles.get(business_id)
        return [await self._item(profile, c) for c in rows[:limit]], None

    async def item(self, business_id: str, commitment_id: str) -> InboxItem | None:
        self.item_calls += 1
        commitment = await self.commitments.get(business_id, commitment_id)
        if commitment is None:
            return None
        return await self._item(await self.profiles.get(business_id), commitment)

    async def _item(self, profile: FakeProfile | None, commitment: Commitment) -> InboxItem:
        """Like the domain: module decisions and response fields; owner questions get `answer`."""
        if commitment.kind == "owner_question":
            allowed: list[Any] = ["answer"]
            fields: list[Any] = [dict(ANSWER_FIELD)]
        else:
            approval = profile.module(commitment.module_id).get("approval", {}) if profile else {}
            allowed, fields = list(approval.get("decisions", [])), list(approval.get("response_fields", []))
        status = None
        if commitment.task_id:
            status = (await self.tasks.get(commitment.task_id)).status
        return InboxItem(commitment=commitment, task_status=status, allowed_decisions=allowed, response_fields=fields)

    async def decide(self, business_id: str, commitment_id: str, decision: OwnerDecisionInput) -> Commitment:
        self.decisions.append((business_id, commitment_id, decision))
        current = await self.commitments.get(business_id, commitment_id)
        if current is None:
            raise NotFound("Unknown request")
        if decision.decision not in self.ALLOWED:
            raise InvalidDecision(f"decision {decision.decision} is not allowed for this request")
        if current.status not in OPEN_STATUSES:
            raise InvalidState("request is not pending")
        now = self.clock.now()
        updated = current.model_copy(
            update={
                "status": DECISION_TO_STATUS[decision.decision],
                "decision": OwnerDecision(decision=decision.decision, note=decision.note, decided_at=now),
                "updated_at": now,
                "version": current.version + 1,
            }
        )
        self.commitments.put(updated)
        if current.task_id is not None:
            result = types.CallToolResult(
                content=[types.TextContent(type="text", text=f"Owner decided: {decision.decision}")],
                structured_content={"status": updated.status, "reference": commitment_id},
            )
            await self.tasks.complete(current.task_id, result)
        return updated

    async def expire_due(self, *, now: datetime | None = None, limit: int = 100) -> int:
        self.expire_calls += 1
        if self.expire_errors:
            raise self.expire_errors.pop(0)
        return 0


@dataclass
class Seeded:
    pending_order: Commitment
    question: Commitment
    approved: Commitment
    other_tenant: Commitment


class OwnerWorld:
    """Fakes for the other work packages, wired into a real AskOnceDeps with a real TaskManager."""

    def __init__(self, settings: Settings, clock: FixedClock) -> None:
        self.settings = settings
        self.clock = clock
        self.profiles = FakeProfileStore(
            [
                FakeProfile(
                    BAKERY,
                    "Thimbleberry Bakehouse",
                    {
                        "cakes": {"type": "custom_order", "approval": CUSTOM_ORDER_APPROVAL},
                        "owner": {"type": "owner_approval"},
                    },
                ),
                FakeProfile(SALON, "Quillfern Hair Studio", {"owner": {"type": "owner_approval"}}),
            ]
        )
        self.commitments = FakeCommitmentStore()
        self.task_manager = TaskManager(InMemoryTaskStore(), default_poll_interval_ms=20)
        self.owner = FakeOwnerService(self.commitments, self.task_manager, clock, self.profiles)
        unused = cast(Any, None)
        self.deps = AskOnceDeps(
            settings=settings,
            clock=clock,
            ids=IdFactory(settings.id_key_bytes()),
            profiles=cast(Any, self.profiles),
            commitments=cast(Any, self.commitments),
            capacity=unused,
            idempotency=unused,
            task_manager=self.task_manager,
            tasks=TasksExtension(self.task_manager),
            bookings=unused,
            orders=unused,
            quotes=unused,
            owner=cast(Any, self.owner),
        )

    def commitment(
        self,
        commitment_id: str,
        *,
        business_id: str = BAKERY,
        module_id: str = "cakes",
        kind: CommitmentKind = "custom_order",
        status: CommitmentStatus = "pending_owner",
        task_id: str | None = None,
        age_minutes: int = 0,
    ) -> Commitment:
        created = self.clock.now() - timedelta(minutes=age_minutes)
        record = Commitment(
            commitment_id=commitment_id,
            business_id=business_id,
            module_id=module_id,
            kind=kind,
            status=status,
            principal="anonymous",
            idempotency_key=f"idem_{commitment_id}",
            profile_revision=1,
            intake={"customer_name": "Sam Lee", "customer_phone": "+12175550100"},
            task_id=task_id,
            created_at=created,
            updated_at=created,
            expires_at=created + timedelta(hours=48),
        )
        self.commitments.put(record)
        return record

    async def task(self, task_id: str, tool_name: str = "place_custom_order") -> str:
        await self.task_manager.create(
            principal="anonymous",
            tool_name=tool_name,
            arguments={},
            status_message="Waiting for Rosa.",
            task_id=task_id,
        )
        return task_id

    async def seed(self) -> Seeded:
        return Seeded(
            pending_order=self.commitment("c_pending1", task_id=await self.task("t_pending1"), age_minutes=5),
            question=self.commitment(
                "c_question1",
                module_id="owner",
                kind="owner_question",
                task_id=await self.task("t_question1", "ask_owner"),
                age_minutes=1,
            ),
            approved=self.commitment("c_done1", status="approved", age_minutes=60),
            other_tenant=self.commitment(
                "c_salon1", business_id=SALON, module_id="owner", kind="owner_question",
                task_id=await self.task("t_salon1", "ask_owner"),
            ),
        )


@pytest.fixture
def world_factory(clock: FixedClock) -> Callable[[Settings], OwnerWorld]:
    """Build another fake world (e.g. a second replica with different settings) sharing the test clock."""
    return lambda settings: OwnerWorld(settings, clock)


@pytest.fixture
def world(settings: Settings, world_factory: Callable[[Settings], OwnerWorld]) -> OwnerWorld:
    return world_factory(settings)


@pytest.fixture
async def seeded(world: OwnerWorld) -> Seeded:
    return await world.seed()


@pytest.fixture
def app_signer(world: OwnerWorld) -> OwnerLinkSigner:
    """A signer equivalent to the app's (same keys, same clock)."""
    from askonce_server.app.links import signer_from_settings

    return signer_from_settings(world.settings, clock=world.clock)


@pytest.fixture
def owner_app(world: OwnerWorld) -> FastAPI:
    return create_app(world.settings, deps=world.deps)


@pytest.fixture
async def api(owner_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """In-loop HTTP client for the owner API (no lifespan needed: the owner routes don't use MCP)."""
    transport = httpx.ASGITransport(app=owner_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


# --------------------------------------------------------------------------- MCP probe tool


class ProbeConfirm(BaseModel):
    confirm: bool = Field(description="Go ahead?")


def probe_ask(item: str) -> Elicit[ProbeConfirm]:
    """Resolver: one deterministic confirmation question (MRTR on 2026-07-28, elicitation/create on legacy)."""
    return Elicit(f"Shall I probe {item}?", ProbeConfirm)


async def probe_commit(item: str, answer: Annotated[ElicitationResult[ProbeConfirm], Resolve(probe_ask)]) -> str:
    """Test-only tool exercising the transport's confirmation round trip."""
    if isinstance(answer, AcceptedElicitation) and answer.data.confirm:
        return f"committed {item}"
    return f"not committed {item} ({answer.action})"


def server_with_probe(deps: AskOnceDeps) -> MCPServer:
    """The real AskOnce MCPServer (name, request-state keys, extensions) plus the probe tool."""
    server = build_mcp_server(deps)
    server.add_tool(probe_commit)
    return server


# --------------------------------------------------------------------------- real HTTP


@pytest.fixture
def probe_factory() -> Callable[[AskOnceDeps], MCPServer]:
    return server_with_probe


@pytest.fixture
def serve_app() -> Callable[[FastAPI], contextlib.AbstractContextManager[str]]:
    return serve


@pytest.fixture
def served(world: OwnerWorld) -> Iterator[str]:
    """Base URL of the app (real AskOnce MCP server + probe tool) served over real HTTP."""
    with serve(create_app(world.settings, deps=world.deps, mcp_server_factory=server_with_probe)) as url:
        yield url

