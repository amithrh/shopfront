"""Transport security and CORS options are built from settings, never from SDK defaults."""

from __future__ import annotations

import pytest
from support import TEST_KEY

from shopfront_server.app.security import (
    MCP_EXPOSED_HEADERS,
    MCP_REQUEST_HEADERS,
    cors_options,
    cors_origins_rejected_by_transport,
    origin_allowed,
    transport_security,
)
from shopfront_server.config import Settings

KEYS = {"request_state_keys": [TEST_KEY], "owner_link_keys": [TEST_KEY + "-links"], "id_key": TEST_KEY + "-ids"}
"""Non-DEV keys: a public host or origin makes the settings look deployed, which refuses DEV-ONLY keys."""


def test_transport_security_comes_from_settings() -> None:
    settings = Settings(
        allowed_hosts=["mcp.example.com", "localhost:*"], allowed_origins=["https://host.example"], **KEYS
    )
    security = transport_security(settings)
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["mcp.example.com", "localhost:*"]
    assert security.allowed_origins == ["https://host.example"]


def test_transport_security_lists_are_copies() -> None:
    settings = Settings()
    security = transport_security(settings)
    security.allowed_hosts.append("evil.example")
    assert "evil.example" not in settings.allowed_hosts


def test_cors_options() -> None:
    options = cors_options(Settings(cors_origins=["https://host.example"], **KEYS))
    assert options["allow_origins"] == ["https://host.example"]
    assert set(options["allow_methods"]) == {"GET", "POST", "DELETE", "OPTIONS"}
    assert set(options["allow_headers"]) == set(MCP_REQUEST_HEADERS)
    for header in ("mcp-protocol-version", "mcp-method", "mcp-name", "last-event-id", "authorization",
                   "content-type", "accept", "mcp-session-id"):
        assert header in options["allow_headers"]
    assert options["expose_headers"] == list(MCP_EXPOSED_HEADERS) == ["mcp-session-id"]
    assert options["allow_credentials"] is False


@pytest.mark.parametrize(
    ("origin", "patterns", "allowed"),
    [
        ("http://localhost:5173", ["http://localhost:*"], True),
        ("http://localhost", ["http://localhost:*"], False),
        ("http://localhost.evil.example:80", ["http://localhost:*"], False),
        ("https://host.example", ["https://host.example"], True),
        ("https://host.example:443", ["https://host.example"], False),
        ("https://evil.example", ["https://host.example", "http://localhost:*"], False),
    ],
)
def test_origin_matching_mirrors_the_transport(origin: str, patterns: list[str], allowed: bool) -> None:
    assert origin_allowed(origin, patterns) is allowed


def test_cors_origins_missing_from_the_transport_allow_list_are_reported() -> None:
    settings = Settings(
        cors_origins=["http://localhost:5173", "https://owner.example"],
        allowed_origins=["http://localhost:*"],
        **KEYS,
    )
    assert cors_origins_rejected_by_transport(settings) == ["https://owner.example"]
    assert cors_origins_rejected_by_transport(Settings()) == []


def test_round2_prod_safety_origin_allowed_is_shared_with_the_settings_guard() -> None:
    """The prod guard and the startup warning use the same transport rule (no drift)."""
    from shopfront_server import config

    assert origin_allowed is config.origin_allowed
