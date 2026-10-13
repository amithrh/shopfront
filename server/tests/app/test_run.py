"""The container entrypoint (`python -m shopfront_server`) and the app's route table."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route
from support import TEST_KEY, pending

from shopfront_server.app import run
from shopfront_server.config import MCP_PATH, Settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in list(os.environ):
        if name.startswith("SHOPFRONT_") or name == "FORWARDED_ALLOW_IPS":
            monkeypatch.delenv(name)
    return monkeypatch


@pytest.fixture
def captured(clean_env: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch uvicorn.run and create_app in the run module; record what main() passes."""
    seen: dict[str, Any] = {}
    sentinel = object()

    def fake_create_app(settings: Settings) -> object:
        seen["settings"] = settings
        return sentinel

    def fake_run(app: object, **kwargs: Any) -> None:
        seen["app"] = app
        seen["kwargs"] = kwargs

    clean_env.setattr(run, "create_app", fake_create_app)
    clean_env.setattr(run.uvicorn, "run", fake_run)
    clean_env.setattr(run, "configure_logging", lambda level: seen.setdefault("log_level", level))
    seen["sentinel"] = sentinel
    return seen


def test_main_serves_the_agentcore_contract(captured: dict[str, Any], clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SHOPFRONT_HOST", "0.0.0.0")  # as the Dockerfile does (dev alone binds loopback)
    run.main()
    kwargs = captured["kwargs"]
    assert captured["app"] is captured["sentinel"]
    assert (kwargs["host"], kwargs["port"]) == ("0.0.0.0", 8000)
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "127.0.0.1"
    assert kwargs["timeout_keep_alive"] == 75 > 60  # above the ALB's default idle timeout
    assert kwargs["workers"] == 1
    assert captured["log_level"] == "INFO"


def test_main_reads_the_environment(captured: dict[str, Any], clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("SHOPFRONT_PORT", "9100")
    clean_env.setenv("SHOPFRONT_LOG_LEVEL", "DEBUG")
    clean_env.setenv("FORWARDED_ALLOW_IPS", "10.0.0.0/8")
    run.main()
    assert captured["kwargs"]["port"] == 9100
    assert captured["kwargs"]["forwarded_allow_ips"] == "10.0.0.0/8"
    assert captured["log_level"] == "DEBUG" and captured["settings"].log_level == "DEBUG"


PROD_ENV = {
    "SHOPFRONT_ENV": "prod",
    "SHOPFRONT_STORAGE": "dynamodb",
    "SHOPFRONT_ALLOWED_HOSTS": "mcp.example.com",
    "SHOPFRONT_PUBLIC_BASE_URL": "https://mcp.example.com",
    "SHOPFRONT_REQUEST_STATE_KEYS": "run-request-state-key-0123456789abcdef",
    "SHOPFRONT_OWNER_LINK_KEYS": "run-owner-link-key-0123456789abcdef0123",
    "SHOPFRONT_ID_KEY": "run-id-key-0123456789abcdef0123456789",
}


def test_round2_prod_safety_main_binds_loopback_in_dev_and_every_interface_in_prod(
    captured: dict[str, Any], clean_env: pytest.MonkeyPatch
) -> None:
    run.main()
    assert captured["kwargs"]["host"] == "127.0.0.1"  # plain `uv run python -m shopfront_server`
    for name, value in PROD_ENV.items():
        clean_env.setenv(name, value)
    run.main()
    assert captured["kwargs"]["host"] == "0.0.0.0"
    assert captured["settings"].env == "prod" and captured["settings"].invariants_strict


def test_round2_prod_safety_main_exits_2_without_traceback_or_secrets(
    captured: dict[str, Any], clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "ADM-secret-" + "D" * 40
    clean_env.setenv("SHOPFRONT_ENV", "prod")
    clean_env.setenv("SHOPFRONT_REQUEST_STATE_KEYS", "RSK-secret-" + "A" * 40)
    clean_env.setenv("SHOPFRONT_OWNER_LINK_KEYS", "OLK-secret-" + "B" * 40)
    clean_env.setenv("SHOPFRONT_ID_KEY", "IDK-secret-" + "C" * 40)
    clean_env.setenv("SHOPFRONT_ADMIN_TOKEN", secret)
    with pytest.raises(SystemExit) as exited:
        run.main()
    assert exited.value.code == 2
    assert "app" not in captured  # nothing was served
    err = capsys.readouterr().err
    assert err.startswith("Shopfront refuses to start:")
    for needle in ("SHOPFRONT_STORAGE=dynamodb", "SHOPFRONT_ALLOWED_HOSTS", "SHOPFRONT_PUBLIC_BASE_URL"):
        assert needle in err, needle  # every problem, in one start
    for marker in ("DDDDDDDD", "AAAAAAAA", "BBBBBBBB", "CCCCCCCC", "input_value", "Traceback"):
        assert marker not in err, marker


def test_round2_prod_safety_field_errors_name_the_variable(
    captured: dict[str, Any], clean_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_env.setenv("SHOPFRONT_OWNER_LINK_KEYS", "short-secret-value")
    with pytest.raises(SystemExit):
        run.main()
    err = capsys.readouterr().err
    assert "SHOPFRONT_OWNER_LINK_KEYS: keys must be at least 32 bytes" in err
    assert "short-secret-value" not in err


def test_round2_prod_safety_startup_warnings() -> None:
    assert run.startup_warnings(Settings()) == []  # loopback + DEV keys + lenient: nothing to say
    (exposed,) = run.startup_warnings(Settings(host="0.0.0.0"))
    assert "DEV-ONLY" in exposed and "127.0.0.1:8000:8000" in exposed
    strict = Settings(strict_invariants=True)
    assert any("SHOPFRONT_PROFILE_SNAPSHOT_DIR" in w for w in run.startup_warnings(strict))


def test_package_main_is_the_run_entrypoint() -> None:
    import shopfront_server.__main__ as package_main

    assert package_main.main is run.main


def test_route_table(owner_app: FastAPI) -> None:
    paths = {getattr(route, "path", None) for route in owner_app.routes}
    assert {MCP_PATH, "/health", "/ping"} <= paths
    assert "/mcp/" not in paths and "/docs" not in paths and "/openapi.json" not in paths
    assert not [r for r in owner_app.routes if isinstance(r, Mount)]
    (mcp_route,) = [r for r in owner_app.routes if getattr(r, "path", None) == MCP_PATH]
    assert type(mcp_route) is Route  # a Route (exact match), not a Mount (which would 307 to /mcp/)
    with TestClient(owner_app) as client:  # the owner router is mounted (it answers 401, not 404)
        assert client.get("/owner/thimbleberry-bakehouse/inbox").status_code == 401
        assert client.post("/mcp/", json={}, follow_redirects=False).status_code == 307  # slash -> /mcp


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_python_m_shopfront_server_serves_mcp(tmp_path: Any) -> None:
    """End to end through the real entrypoint (needs the real profile/domain/tools packages)."""
    from shopfront_server.app.wiring import build_deps

    pending("profile/domain/tools", lambda: build_deps(Settings(env="test")))
    port = free_port()
    env = {
        **os.environ,
        "SHOPFRONT_HOST": "127.0.0.1",
        "SHOPFRONT_PORT": str(port),
        "SHOPFRONT_ENV": "test",
        "SHOPFRONT_REQUEST_STATE_KEYS": TEST_KEY,
        "SHOPFRONT_LOG_LEVEL": "WARNING",
    }
    process = subprocess.Popen([sys.executable, "-m", "shopfront_server"], env=env)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except httpx.TransportError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        assert response.json()["status"] == "ok"
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
        mcp = httpx.post(
            f"http://127.0.0.1:{port}/mcp", json=init, timeout=10,
            headers={"accept": "application/json, text/event-stream"},
        )
        assert mcp.status_code == 200 and mcp.headers.get("mcp-session-id")
    finally:
        process.terminate()
        process.wait(timeout=30)
