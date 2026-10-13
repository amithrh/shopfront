"""Access to process dependencies and per-request identity from inside tools and resolvers.

Also the small amount of plumbing every tool shares:

  * `live_profile` is the tenant gate: unknown and non-live businesses both become `ToolError`
    "Unknown business" (indistinguishable on purpose);
  * `tool_span` opens the `askonce.tool.<name>` span, and `finish` stamps `business_id`,
    `profile_revision` and `trace_id` on the result and records the PII-free span attributes.
    Both namespace extra attributes as `askonce.<key>` and record commitment and task ids only as
    `observability.id_digest` (`_span_attributes`).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TypeVar

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp_tasks import ANONYMOUS_PRINCIPAL, principal_of
from opentelemetry.trace import Span
from askonce_server import observability
from askonce_server.deps import AskOnceDeps
from askonce_server.errors import UnknownBusiness
from askonce_server.profile.model import Profile
from askonce_server.tools.schemas import Grounded

G = TypeVar("G", bound=Grounded)

UNKNOWN_BUSINESS = "Unknown business. Use find_business to get a valid business_id."

_CAPABILITY_IDS = frozenset({"commitment_id", "task_id"})
"""Span attributes recorded as `observability.id_digest` (see `observability`)."""


def _span_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Extra tool-span attributes as `askonce.<key>`, with `None` values dropped and commitment and
    task ids replaced by their digest: without auth those ids are bearer capabilities (`get_task`,
    `tasks/cancel`), so a trace reader must not be able to recover them."""
    return {
        f"askonce.{key}": observability.id_digest(value) if key in _CAPABILITY_IDS else value
        for key, value in attributes.items()
        if value is not None
    }


def get_deps(ctx: Context[Any, Any]) -> AskOnceDeps:
    """The `AskOnceDeps` yielded by the MCPServer lifespan (`tools.server.build_mcp_server`)."""
    deps = ctx.request_context.lifespan_context
    if not isinstance(deps, AskOnceDeps):
        raise RuntimeError("MCPServer lifespan did not yield AskOnceDeps; build it with build_mcp_server()")
    return deps


def principal(ctx: Context[Any, Any]) -> str:
    """Who is calling: the authenticated principal, or 'anonymous' (task ids are then the capability)."""
    return principal_of(ctx)


def anonymous_nonce(ctx: Context[Any, Any]) -> str | None:
    """A fresh random nonce for an unauthenticated caller, else None.

    Without auth every caller is 'anonymous', so a request whose details cannot tell two strangers
    apart (an owner question; a module that collects no contact field) mixes this into its
    idempotency key. A retry then counts as a new request: the price of never merging strangers."""
    return secrets.token_urlsafe(12) if principal(ctx) == ANONYMOUS_PRINCIPAL else None


async def live_profile(deps: AskOnceDeps, business_id: str) -> Profile:
    """The live profile for `business_id`, or `ToolError` for unknown and non-live tenants alike."""
    try:
        profile = await deps.profiles.get_live(business_id)
    except UnknownBusiness:
        raise ToolError(UNKNOWN_BUSINESS) from None
    if not profile.is_live:  # defence in depth: the store already filters non-live profiles
        raise ToolError(UNKNOWN_BUSINESS)
    return profile


@contextmanager
def tool_span(tool: str, business_id: str | None = None, **attributes: Any) -> Iterator[Span]:
    """The INTERNAL span around one tool body (child of the SDK's `tools/call <tool>` SERVER span).

    `attributes` follow the `finish` rule (`_span_attributes`): `get_task` passes its raw `task_id` and
    the span records only its digest."""
    attrs: dict[str, Any] = {observability.ATTR_TOOL: tool, **_span_attributes(attributes)}
    if business_id:
        attrs[observability.ATTR_BUSINESS] = business_id
    with observability.span(f"askonce.tool.{tool}", **attrs) as current:
        yield current


def finish(result: G, profile: Profile | None, *, outcome: str, **attributes: Any) -> G:
    """Stamp tenant, revision and trace id on `result` and record the outcome on the active span.

    `attributes` are extra span attributes (low cardinality, never PII), e.g. `module_id`.
    """
    update: dict[str, Any] = {"trace_id": observability.current_trace_id()}
    if profile is not None:
        update["business_id"] = profile.profile_id
        update["profile_revision"] = profile.revision
    stamped = result.model_copy(update=update)
    span_attrs: dict[str, Any] = {
        observability.ATTR_OUTCOME: outcome,
        observability.ATTR_FACT_COUNT: len(stamped.facts),
    }
    if profile is not None:
        span_attrs[observability.ATTR_BUSINESS] = profile.profile_id
    # Commitment and task ids are bearer capabilities without auth: spans get a digest.
    span_attrs.update(_span_attributes(attributes))
    observability.set_attributes(None, **span_attrs)
    return stamped
