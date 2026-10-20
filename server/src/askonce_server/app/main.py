"""FastAPI application: MCP at exactly `/mcp`, owner REST API, health, CORS, background sweeper.

    app = create_app()                       # settings from env
    app = create_app(settings, deps=deps)    # tests (fresh MCPServer per app: run() is once per manager)

Assembly (verified patterns, docs/research/mcp-http-transport.md):
  1. `server = build_mcp_server(deps)`; call `server.streamable_http_app(json_response=False,
     stateless_http=False, transport_security=transport_security(settings))` ONCE so
     `server.session_manager` exists (we use only its session manager, not the returned Starlette app).
  2. `app.router.routes.append(Route("/mcp", endpoint=StreamableHTTPASGIApp(server.session_manager)))`
     -> serves exactly /mcp (no 307 trailing-slash redirect, which breaks behind TLS-terminating LBs).
  3. FastAPI lifespan: `async with server.session_manager.run():` (mounted/route apps' lifespans are
     not run by FastAPI) + start `run_sweeper` in a task group; cancel it on shutdown.
  4. A browser opening an owner link gets the owner phone page (`owner_page.OwnerPageMiddleware`); the
     page calls the same URLs as JSON.
  5. Routes: GET /health and GET /ping -> {"status": "ok", "version", "storage"} (no MCP, no auth;
     AgentCore/ALB health checks), the owner router, and nothing else (no OpenAPI/docs routes).
  6. CORSMiddleware outermost (`cors_options(settings)`), then `BodySizeLimitMiddleware` (413 for an
     owner API request declaring a body over `owner_api.MAX_BODY_BYTES`).
`json_response=False` keeps SSE (progress + 15 s keep-alive pings behind proxies);
`stateless_http=False` keeps legacy (2025-11-25) clients' sessions working; 2026-07-28 is sessionless.

This module deliberately avoids `from __future__ import annotations` (FastAPI evaluates route
annotations at registration time).
"""

import functools
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Literal

import anyio
from fastapi import FastAPI
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import Route

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from askonce_server import __version__
from askonce_server.app.background import run_sweeper
from askonce_server.app.links import OwnerLinkSigner, signer_from_settings
from askonce_server.app.owner_api import MAX_BODY_BYTES, build_owner_router, install_error_handlers
from askonce_server.app.owner_page import OwnerPageMiddleware
from askonce_server.app.security import (
    BodySizeLimitMiddleware,
    cors_options,
    cors_origins_rejected_by_transport,
    transport_security,
)
from askonce_server.app.wiring import build_deps
from askonce_server.config import MCP_PATH, Settings, get_settings
from askonce_server.deps import AskOnceDeps
from askonce_server.tools.server import build_mcp_server

logger = logging.getLogger("askonce.app")

McpServerFactory = Callable[[AskOnceDeps], MCPServer]
"""Builds a fresh MCPServer for one app (default: `tools.server.build_mcp_server`)."""


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    storage: str


def create_app(
    settings: Settings | None = None,
    *,
    deps: AskOnceDeps | None = None,
    signer: OwnerLinkSigner | None = None,
    mcp_server_factory: McpServerFactory | None = None,
) -> FastAPI:
    """Build the ASGI app (see module docstring). With `deps=None`, wiring follows `settings.storage`.

    `signer` defaults to the one `build_deps` returns (or one built from `settings` when `deps` is
    given; tokens are deterministic, so both verify the same links). `mcp_server_factory` lets tests
    add probe tools to the real server; it must return a NEW server on every call.
    """
    if settings is None:
        settings = deps.settings if deps is not None else get_settings()
    if deps is None:
        deps, built_signer = build_deps(settings)
        signer = signer or built_signer
    if signer is None:
        signer = signer_from_settings(settings, clock=deps.clock)

    server = (mcp_server_factory or build_mcp_server)(deps)
    server.streamable_http_app(
        json_response=False, stateless_http=False, transport_security=transport_security(settings)
    )
    session_manager = server.session_manager

    for origin in cors_origins_rejected_by_transport(settings):  # prod settings refuse this outright
        logger.warning(
            "CORS origin %s is not in ASKONCE_ALLOWED_ORIGINS: the MCP transport will answer it with 403", origin
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        async with session_manager.run(), anyio.create_task_group() as task_group:
            if settings.sweep_interval_s > 0:
                task_group.start_soon(functools.partial(run_sweeper, deps.owner, interval_s=settings.sweep_interval_s))
            logger.info(
                "AskOnce %s ready: MCP at %s, storage=%s, env=%s",
                __version__,
                MCP_PATH,
                settings.storage,
                settings.env,
            )
            try:
                yield
            finally:
                task_group.cancel_scope.cancel()

    app = FastAPI(
        title="AskOnce",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.deps = deps
    app.state.signer = signer
    app.state.mcp_server = server

    health = HealthResponse(version=__version__, storage=settings.storage)

    @app.get("/health", response_model=HealthResponse, include_in_schema=False)
    async def get_health() -> HealthResponse:
        return health

    @app.get("/ping", response_model=HealthResponse, include_in_schema=False)
    async def get_ping() -> HealthResponse:
        return health

    app.include_router(build_owner_router(deps, signer))
    install_error_handlers(app)
    app.router.routes.append(Route(MCP_PATH, endpoint=StreamableHTTPASGIApp(session_manager)))
    app.add_middleware(OwnerPageMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES, path_prefix="/owner/")
    app.add_middleware(CORSMiddleware, **cors_options(settings))
    return app
