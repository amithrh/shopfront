"""The composition root's output: every collaborator the MCP tools and the owner API need.

Built once per process by `app.wiring.build_deps` (or per test by the conftest fixtures) and handed to
`tools.server.build_mcp_server`, whose MCPServer lifespan yields it to tools as
`ctx.request_context.lifespan_context` (see `tools.context.get_deps`).
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp_tasks import TaskManager, TasksExtension
from shopfront_server.clock import Clock
from shopfront_server.config import Settings
from shopfront_server.domain.ports import CapacityStore, CommitmentStore, IdempotencyStore
from shopfront_server.domain.services import BookingService, CustomOrderService, OwnerService, QuoteService
from shopfront_server.ids import IdFactory
from shopfront_server.profile.store import ProfileStore


@dataclass(frozen=True)
class ShopfrontDeps:
    settings: Settings
    clock: Clock
    ids: IdFactory
    profiles: ProfileStore
    commitments: CommitmentStore
    capacity: CapacityStore
    idempotency: IdempotencyStore
    task_manager: TaskManager
    tasks: TasksExtension
    bookings: BookingService
    orders: CustomOrderService
    quotes: QuoteService
    owner: OwnerService
