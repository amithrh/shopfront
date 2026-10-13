"""Build the AskOnce MCPServer: one multi-tenant endpoint, every tool, cards, tasks and the guard.

    server = build_mcp_server(deps)
    app.router.routes.append(Route("/mcp", StreamableHTTPASGIApp(...)))   # see app.main

Construction order matters: cards and Apps-bound tools are registered on `Apps` before `MCPServer(...)`.
Extensions (first = outermost interceptor): caller-supplied extras, the tasks extension, Apps.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.apps import Apps
from mcp.server.extension import Extension
from mcp.server.mcpserver import MCPServer, RequestStateSecurity
from mcp.types import ToolAnnotations
from askonce_server import __version__
from askonce_server.config import SERVER_NAME
from askonce_server.deps import AskOnceDeps
from askonce_server.tools import booking, catalog, discovery, knowledge, orders, owner, quotes, requests
from askonce_server.tools.cards import BUSINESS_CARD, COMMITMENT_CARD, ITEMS_CARD, TASK_CARD, register_cards
from askonce_server.tools.guard import OutputGuardMiddleware

INSTRUCTIONS = (
    "AskOnce connects you to certified local businesses. Start with find_business to get a business_id, "
    "then use the business tools. Read only the `speak` field aloud; show `card` if you can. Never state "
    "a price, allergen, hour or policy that is not in a tool result. When a result has `handoff`, offer "
    "to ask the owner and call ask_owner if the customer agrees. Bookings, orders and quote requests "
    "always ask the customer to confirm first; owner approvals continue as tasks (poll get_task if your "
    "client does not support MCP tasks, and pass the customer's answer to reply_to_owner when the owner "
    "asks a question). To check or cancel an earlier request from any client, call check_request_status "
    "or cancel_request with its task_id, reference or spoken booking code."
)

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
"""Reads the business's published profile and this server's own records: nothing leaves the server."""
OWNER_COMMITS = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
"""Creates or answers a request at a real business, which puts it in front of the owner (open world);
an identical retry replays the first request instead of making a second one."""
ASKS_OWNER = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
"""ask_owner: messages the owner. Not idempotent: without auth every call mixes in a fresh nonce
(strangers' identical questions must never merge), so a retry asks the owner again."""
CANCELS = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)
"""Withdraws a real appointment (or a request still waiting for the owner) at a real business."""


@dataclass(frozen=True)
class ToolSpec:
    fn: Callable[..., Any]
    title: str
    card: str | None
    annotations: ToolAnnotations


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(discovery.find_business, "Find a business", BUSINESS_CARD, READ_ONLY),
    ToolSpec(discovery.get_business_info, "Business info", BUSINESS_CARD, READ_ONLY),
    ToolSpec(catalog.get_catalog, "Browse catalog", ITEMS_CARD, READ_ONLY),
    ToolSpec(catalog.find_item, "Item details", ITEMS_CARD, READ_ONLY),
    ToolSpec(knowledge.ask_policy, "Policies and FAQ", BUSINESS_CARD, READ_ONLY),
    ToolSpec(booking.check_availability, "Check availability", COMMITMENT_CARD, READ_ONLY),
    ToolSpec(booking.book, "Book", COMMITMENT_CARD, OWNER_COMMITS),
    ToolSpec(booking.cancel_booking, "Cancel booking", COMMITMENT_CARD, CANCELS),
    ToolSpec(orders.place_custom_order, "Place custom order", COMMITMENT_CARD, OWNER_COMMITS),
    ToolSpec(quotes.check_service_area, "Check service area", BUSINESS_CARD, READ_ONLY),
    ToolSpec(quotes.request_quote, "Request a quote", COMMITMENT_CARD, OWNER_COMMITS),
    ToolSpec(owner.ask_owner, "Ask the owner", TASK_CARD, ASKS_OWNER),
    ToolSpec(owner.get_task, "Request status", TASK_CARD, READ_ONLY),
    ToolSpec(owner.reply_to_owner, "Reply to the owner", TASK_CARD, OWNER_COMMITS),
    ToolSpec(requests.check_request_status, "Check a request", TASK_CARD, READ_ONLY),
    ToolSpec(requests.cancel_request, "Cancel a request", COMMITMENT_CARD, CANCELS),
)
"""Every tool, in the order tools/list returns them."""


def _lifespan(deps: AskOnceDeps) -> Callable[[MCPServer], Any]:
    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[AskOnceDeps]:
        yield deps

    return lifespan


def build_mcp_server(deps: AskOnceDeps, *, extensions: Sequence[Extension] = ()) -> MCPServer:
    """A fresh, fully wired MCPServer. Call once per process (or per test)."""
    apps = Apps()
    register_cards(apps)
    for spec in TOOLS:
        if spec.card is not None:
            apps.tool(resource_uri=spec.card, visibility=["model", "app"], title=spec.title, annotations=spec.annotations)(
                spec.fn
            )
    server = MCPServer(
        SERVER_NAME,
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=_lifespan(deps),
        extensions=[*extensions, deps.tasks, apps],
        request_state_security=RequestStateSecurity(
            keys=deps.settings.request_state_key_bytes(), ttl=deps.settings.request_state_ttl_s
        ),
        middleware=[OutputGuardMiddleware(deps.profiles)],
    )
    for spec in TOOLS:
        if spec.card is None:
            server.add_tool(spec.fn, title=spec.title, annotations=spec.annotations)
    return server
