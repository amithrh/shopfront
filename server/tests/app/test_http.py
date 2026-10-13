"""The HTTP application: health routes, MCP at exactly /mcp, Host/Origin gates, CORS, lifespan and
the cross-replica MRTR round trip. Uses Starlette's TestClient (which runs the lifespan)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.server.transport_security import TransportSecurityMiddleware, TransportSecuritySettings
from pydantic import SecretStr
from support import TEST_KEY

from askonce_server import __version__
from askonce_server.app.main import create_app
from askonce_server.config import Settings

MODERN = "2026-07-28"
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
}
LEGACY_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
JSON_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
BROWSER_ORIGIN = "http://localhost:5173"


def modern_post(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int = 1,
    headers: dict[str, str] | None = None,
) -> Any:
    """A raw 2026-07-28 request (headers and `_meta` envelope as the SDK client sends them)."""
    request_headers = {**JSON_HEADERS, "mcp-protocol-version": MODERN, "mcp-method": method}
    if method == "tools/call" and params:
        request_headers["mcp-name"] = params["name"]
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {**(params or {}), "_meta": MODERN_META}}
    return client.post("/mcp", json=body, headers={**request_headers, **(headers or {})}, follow_redirects=False)


def jsonrpc_message(response: Any) -> dict[str, Any]:
    """The JSON-RPC response from a plain JSON or an SSE body."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        messages = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
        return next(m for m in reversed(messages) if "id" in m)
    return dict(response.json())


@pytest.fixture
def client(owner_app: FastAPI) -> Any:
    with TestClient(owner_app, base_url="http://testserver") as test_client:
        yield test_client


# --------------------------------------------------------------------------- health and routes


@pytest.mark.parametrize("path", ["/health", "/ping"])
def test_health_routes(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__, "storage": "memory"}


def test_health_needs_no_allow_listed_host(client: TestClient) -> None:
    # Load balancers probe by IP; only the MCP transport enforces the Host allow-list.
    assert client.get("/health", headers={"host": "10.0.0.12:8000"}).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/", "/mcp/extra"])
def test_nothing_else_is_served(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_each_app_builds_a_fresh_mcp_server(world: Any) -> None:
    first, second = create_app(world.settings, deps=world.deps), create_app(world.settings, deps=world.deps)
    assert first.state.mcp_server is not second.state.mcp_server
    for app in (first, second):  # each lifespan runs its own session manager (run() is once per manager)
        with TestClient(app) as test_client:
            assert test_client.get("/health").status_code == 200


# --------------------------------------------------------------------------- /mcp


def test_post_mcp_is_served_without_redirect(client: TestClient) -> None:
    response = client.post("/mcp", json=LEGACY_INIT, headers=JSON_HEADERS, follow_redirects=False)
    assert response.status_code == 200, response.text
    assert response.headers.get("mcp-session-id")
    message = jsonrpc_message(response)
    assert message["result"]["serverInfo"]["name"] == "askonce"
    assert message["result"]["protocolVersion"] == "2025-11-25"


def test_modern_tools_list_is_sessionless(client: TestClient) -> None:
    response = modern_post(client, "tools/list")
    assert response.status_code == 200, response.text
    assert "mcp-session-id" not in response.headers
    names = [tool["name"] for tool in jsonrpc_message(response)["result"]["tools"]]
    assert names[0] == "find_business" and "get_task" in names and len(names) == 16


def test_unknown_host_gets_421(client: TestClient) -> None:
    response = client.post("/mcp", json=LEGACY_INIT, headers={**JSON_HEADERS, "host": "evil.example.com"})
    assert response.status_code == 421
    assert modern_post(client, "tools/list", headers={"host": "evil.example.com"}).status_code == 421


def test_unknown_origin_gets_403(client: TestClient) -> None:
    response = client.post("/mcp", json=LEGACY_INIT, headers={**JSON_HEADERS, "origin": "https://evil.example.com"})
    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


def test_allowed_browser_origin_reads_the_session_id(client: TestClient) -> None:
    response = client.post("/mcp", json=LEGACY_INIT, headers={**JSON_HEADERS, "origin": BROWSER_ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == BROWSER_ORIGIN
    assert "mcp-session-id" in response.headers["access-control-expose-headers"].lower()


def test_non_json_post_is_rejected(client: TestClient) -> None:
    response = client.post("/mcp", content=b"{}", headers={"content-type": "text/plain", "accept": "application/json"})
    assert response.status_code == 400


# --------------------------------------------------------------------------- CORS


def preflight(client: TestClient, path: str, origin: str, headers: str) -> Any:
    return client.options(
        path,
        headers={"origin": origin, "access-control-request-method": "POST", "access-control-request-headers": headers},
    )


def test_cors_preflight_for_mcp_is_allowed(client: TestClient) -> None:
    requested = "content-type, accept, mcp-protocol-version, mcp-method, mcp-name, mcp-session-id, last-event-id"
    response = preflight(client, "/mcp", BROWSER_ORIGIN, requested)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == BROWSER_ORIGIN
    allowed = {h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")}
    assert set(requested.replace(" ", "").split(",")) <= allowed
    assert {"GET", "POST", "DELETE", "OPTIONS"} <= set(response.headers["access-control-allow-methods"].split(", "))
    assert "access-control-allow-credentials" not in response.headers


def test_cors_preflight_for_owner_api_is_allowed(client: TestClient) -> None:
    response = preflight(client, "/owner/thimbleberry-bakehouse/inbox", BROWSER_ORIGIN, "authorization, content-type")
    assert response.status_code == 200


def test_cors_preflight_from_unknown_origin_is_denied(client: TestClient) -> None:
    response = preflight(client, "/mcp", "https://evil.example.com", "content-type")
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_cors_preflight_with_unlisted_header_is_denied(client: TestClient) -> None:
    assert preflight(client, "/mcp", BROWSER_ORIGIN, "content-type, x-surprise").status_code == 400


def test_cors_origin_missing_from_transport_allow_list_is_logged(
    world_factory: Callable[[Settings], Any], settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    misconfigured = settings.model_copy(update={"cors_origins": ["https://owner.example"]})
    with caplog.at_level(logging.WARNING, logger="askonce.app"):
        world = world_factory(misconfigured)
        create_app(misconfigured, deps=world.deps)
    assert "https://owner.example" in caplog.text


@pytest.mark.parametrize(
    "hosts",
    [["mcp.example.com"], ["mcp.example.com", "mcp.example.com:443"], ["mcp.example.com", "mcp.example.com:*"]],
)
def test_round2_prod_safety_prod_settings_that_start_answer_their_public_host(
    world_factory: Callable[[Settings], Any], hosts: list[str]
) -> None:
    """F9: whatever prod accepts must actually serve /mcp to a client on the default https port (no 421)
    and to the configured browser origin (no 403); entries that cannot are refused at startup instead."""
    origin = "https://host.example.com"
    prod = Settings(
        env="prod",
        storage="dynamodb",  # the fake world below keeps everything in memory
        allowed_hosts=hosts,
        public_base_url="https://mcp.example.com",
        allowed_origins=[origin],
        cors_origins=[origin],
        request_state_keys=[TEST_KEY],
        owner_link_keys=[TEST_KEY + "-links"],
        id_key=TEST_KEY + "-ids",
        sweep_interval_s=0,
    )
    app = create_app(prod, deps=world_factory(prod).deps)
    with TestClient(app, base_url="https://mcp.example.com") as client:  # Host: mcp.example.com (no :443)
        response = client.post("/mcp", json=LEGACY_INIT, headers={**JSON_HEADERS, "origin": origin})
        assert response.status_code == 200, response.text
        assert client.post("/mcp", json=LEGACY_INIT, headers={**JSON_HEADERS, "host": "evil.example.com"}).status_code == 421


@pytest.mark.parametrize(
    "hosts",
    [
        ["*"],
        ["https://mcp.example.com"],
        ["*.example.com"],
        ["mcp.example.com:443"],
        ["mcp.example.com/"],
        ["mcp.example.com:*"],
    ],
)
def test_round2_prod_safety_prod_refuses_hosts_the_transport_would_421(hosts: list[str]) -> None:
    # The SDK really does reject the portless public Host with each of these lists...
    middleware = TransportSecurityMiddleware(TransportSecuritySettings(allowed_hosts=hosts))
    assert not middleware._validate_host("mcp.example.com")
    # ...so prod refuses to start with them rather than answering every /mcp request with 421.
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        Settings(
            env="prod",
            storage="dynamodb",
            allowed_hosts=hosts,
            public_base_url="https://mcp.example.com",
            request_state_keys=[TEST_KEY],
            owner_link_keys=[TEST_KEY + "-links"],
            id_key=TEST_KEY + "-ids",
        )


# --------------------------------------------------------------------------- MRTR across replicas


def probe_round_one(client: TestClient) -> tuple[str, str]:
    response = modern_post(client, "tools/call", {"name": "probe_commit", "arguments": {"item": "cake"}})
    assert response.status_code == 200, response.text
    result = jsonrpc_message(response)["result"]
    assert result["resultType"] == "input_required"
    (key,) = result["inputRequests"]
    assert result["inputRequests"][key]["params"]["message"] == "Shall I probe cake?"
    return key, result["requestState"]


def probe_round_two(client: TestClient, key: str, state: str, *, confirm: bool = True) -> dict[str, Any]:
    params = {
        "name": "probe_commit",
        "arguments": {"item": "cake"},
        "inputResponses": {key: {"action": "accept", "content": {"confirm": confirm}}},
        "requestState": state,
    }
    return jsonrpc_message(modern_post(client, "tools/call", params, request_id=2))


def test_mrtr_round_completes_on_another_replica(
    world_factory: Callable[[Settings], Any], settings: Settings, probe_factory: Callable[..., Any]
) -> None:
    replica_a = create_app(settings, deps=world_factory(settings).deps, mcp_server_factory=probe_factory)
    replica_b = create_app(settings, deps=world_factory(settings).deps, mcp_server_factory=probe_factory)
    with TestClient(replica_a) as a, TestClient(replica_b) as b:
        key, state = probe_round_one(a)
        message = probe_round_two(b, key, state)
    result = message["result"]
    assert result["resultType"] == "complete" and result["isError"] is False
    assert result["content"][0]["text"] == "committed cake"


def test_mrtr_state_is_rejected_by_a_replica_with_other_keys(
    world_factory: Callable[[Settings], Any], settings: Settings, probe_factory: Callable[..., Any]
) -> None:
    other = settings.model_copy(update={"request_state_keys": [SecretStr(TEST_KEY + "-other-replica")]})
    replica_a = create_app(settings, deps=world_factory(settings).deps, mcp_server_factory=probe_factory)
    replica_c = create_app(other, deps=world_factory(other).deps, mcp_server_factory=probe_factory)
    with TestClient(replica_a) as a, TestClient(replica_c) as c:
        key, state = probe_round_one(a)
        message = probe_round_two(c, key, state)
    assert message["error"]["code"] == -32602


def test_mrtr_unconfirmed_answer_commits_nothing(world: Any, probe_factory: Callable[..., Any]) -> None:
    app = create_app(world.settings, deps=world.deps, mcp_server_factory=probe_factory)
    with TestClient(app) as test_client:
        key, state = probe_round_one(test_client)
        result = probe_round_two(test_client, key, state, confirm=False)["result"]
    assert result["content"][0]["text"] == "not committed cake (accept)"


# --------------------------------------------------------------------------- lifespan + sweeper


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.005)


def test_lifespan_runs_the_sweeper_and_stops_it_on_shutdown(
    world_factory: Callable[[Settings], Any], settings: Settings
) -> None:
    fast = settings.model_copy(update={"sweep_interval_s": 0.01})
    world = world_factory(fast)
    with TestClient(create_app(fast, deps=world.deps)):
        wait_until(lambda: world.owner.expire_calls >= 3)
    stopped_at = world.owner.expire_calls
    time.sleep(0.05)
    assert world.owner.expire_calls == stopped_at


def test_sweeper_survives_failures_inside_the_app(world_factory: Callable[[Settings], Any], settings: Settings) -> None:
    fast = settings.model_copy(update={"sweep_interval_s": 0.01})
    world = world_factory(fast)
    world.owner.expire_errors.extend([RuntimeError("dynamodb throttled"), RuntimeError("again")])
    with TestClient(create_app(fast, deps=world.deps)) as test_client:
        wait_until(lambda: world.owner.expire_calls >= 4)
        assert test_client.get("/health").status_code == 200


def test_sweeper_can_be_disabled(world_factory: Callable[[Settings], Any], settings: Settings) -> None:
    off = settings.model_copy(update={"sweep_interval_s": 0})
    world = world_factory(off)
    with TestClient(create_app(off, deps=world.deps)) as test_client:
        assert test_client.get("/health").status_code == 200
        time.sleep(0.03)
    assert world.owner.expire_calls == 0
