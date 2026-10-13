"""Transport security (Host/Origin allow-lists) and CORS for the MCP endpoint and owner API.

Two independent gates (both must allow a browser origin):
  * `TransportSecuritySettings` inside the MCP transport: Host must be in `allowed_hosts` (else 421),
    Origin (if sent) in `allowed_origins` (else 403). Literal match; `name:*` = any port. Never rely on
    the SDK default (`host="127.0.0.1"` = localhost-only, the classic "421 on AWS" failure).
  * Starlette `CORSMiddleware` on the outer app, answering preflights before they reach the transport.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from mcp.server.transport_security import TransportSecuritySettings
from askonce_server.config import Settings, origin_allowed

__all__ = [
    "CORS_MAX_AGE_S",
    "CORS_METHODS",
    "MCP_EXPOSED_HEADERS",
    "MCP_REQUEST_HEADERS",
    "BodySizeLimitMiddleware",
    "cors_options",
    "cors_origins_rejected_by_transport",
    "origin_allowed",  # lives in config (the prod guard needs it); re-exported for callers here
    "transport_security",
]

MCP_REQUEST_HEADERS: tuple[str, ...] = (
    "content-type",
    "accept",
    "authorization",
    "mcp-session-id",
    "mcp-protocol-version",
    "mcp-method",
    "mcp-name",
    "last-event-id",
)
"""Headers browser MCP clients send (2026-07-28 stamps mcp-method / mcp-name on every request)."""

MCP_EXPOSED_HEADERS: tuple[str, ...] = ("mcp-session-id",)

CORS_METHODS: tuple[str, ...] = ("GET", "POST", "DELETE", "OPTIONS")
CORS_MAX_AGE_S = 600


def transport_security(settings: Settings) -> TransportSecuritySettings:
    """Explicit DNS-rebinding protection from settings (never the SDK's implicit default)."""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=list(settings.allowed_origins),
    )


def cors_options(settings: Settings) -> dict[str, Any]:
    """Keyword arguments for `app.add_middleware(CORSMiddleware, **cors_options(settings))`.

    allow_origins=settings.cors_origins, allow_methods GET/POST/DELETE/OPTIONS, allow_headers =
    MCP_REQUEST_HEADERS (no tool declares `x-mcp-header` params, so no `Mcp-Param-*` headers are
    needed), expose_headers=MCP_EXPOSED_HEADERS, allow_credentials=False (owner links and MCP use
    bearer tokens, never cookies).
    """
    return {
        "allow_origins": list(settings.cors_origins),
        "allow_methods": list(CORS_METHODS),
        "allow_headers": list(MCP_REQUEST_HEADERS),
        "expose_headers": list(MCP_EXPOSED_HEADERS),
        "allow_credentials": False,
        "max_age": CORS_MAX_AGE_S,
    }


def cors_origins_rejected_by_transport(settings: Settings) -> list[str]:
    """CORS origins the transport's Origin check would still answer with 403 (a misconfiguration:
    refused at startup in prod, logged elsewhere)."""
    return [o for o in settings.cors_origins if o != "*" and not origin_allowed(o, settings.allowed_origins)]


Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]


class BodySizeLimitMiddleware:
    """Answer 413 before any route runs when a request under `path_prefix` declares a body larger
    than `max_bytes` (`Content-Length`). Routes that read a body still cap it while streaming
    (`owner_api.read_capped_body`); the MCP transport has its own 4 MiB cap."""

    def __init__(self, app: Callable[[Scope, Receive, Send], Awaitable[None]], *, max_bytes: int, path_prefix: str) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.path_prefix = path_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and str(scope.get("path", "")).startswith(self.path_prefix):
            declared = dict(scope.get("headers") or []).get(b"content-length")
            if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
                body = json.dumps({"error": "Request body too large.", "code": "too_large"}).encode()
                headers = [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                    (b"connection", b"close"),
                ]
                await send({"type": "http.response.start", "status": 413, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
