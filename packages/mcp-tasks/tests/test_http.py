"""The extension over real Streamable HTTP: `MCPServer.streamable_http_app()` under uvicorn on loopback.

The server runs in a background thread on a free 127.0.0.1 port (no other network). The owner acts
through an HTTP webhook so every task transition happens on the server's own event loop.
Deselect with `-m "not http"`.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import mcp_types as types
import pytest
import uvicorn
from mcp import Client
from mcp.client.session import ClientRequestContext
from mcp.shared.exceptions import MCPError
from mcp_types import METHOD_NOT_FOUND

from mcp_tasks.client import TaskHandle, TasksClientExtension, TaskState, get_task
from support import Harness, check_get_body, check_handle_body

pytestmark = [pytest.mark.anyio, pytest.mark.http]

PROTOCOL = "2026-07-28"


@pytest.fixture
def base_url(harness: Harness) -> Iterator[str]:
    """Serve the harness under uvicorn in a daemon thread; yields `http://127.0.0.1:<port>`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(harness.server.streamable_http_app(), log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def envelope(method: str, params: dict[str, Any], rid: int, *, name: str | None = None) -> tuple[dict, dict]:
    """A hand-built 2026-07-28 request: per-request `_meta` envelope plus the mirrored routing headers."""
    body = {
        "jsonrpc": "2.0",
        "id": rid,
        "method": method,
        "params": {
            **params,
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL,
                "io.modelcontextprotocol/clientCapabilities": {"extensions": {"io.modelcontextprotocol/tasks": {}}},
            },
        },
    }
    headers = {
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": PROTOCOL,
        "mcp-method": method,
    }
    if name is not None:
        headers["mcp-name"] = name
    return body, headers


def result_of(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC `result` of a JSON or single-event SSE response."""
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        frames = [
            json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:") and line[5:].strip()
        ]
        message = frames[-1]
    else:
        message = response.json()
    assert "error" not in message, message
    return message["result"]


async def test_raw_wire_tools_call_returns_a_task_then_tasks_get_resolves_it(base_url: str, harness: Harness) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
        body, headers = envelope(
            "tools/call",
            {"name": "request_owner_approval", "arguments": {"summary": "Cake"}},
            1,
            name="request_owner_approval",
        )
        handle = result_of(await http.post("/mcp", json=body, headers=headers))
        assert handle["resultType"] == "task" and handle["status"] == "working"
        assert handle["statusMessage"] == "Waiting for the owner: Cake" and handle["pollIntervalMs"] == 20
        check_handle_body(handle)
        task_id = handle["taskId"]

        body, headers = envelope("tasks/get", {"taskId": task_id}, 2, name=task_id)
        working = result_of(await http.post("/mcp", json=body, headers=headers))
        assert working["status"] == "working" and working["createdAt"] == handle["createdAt"]
        check_get_body(working)

        decision = await http.post(
            f"/owner/tasks/{task_id}/decision", json={"action": "approve", "note": "Done by 5pm"}
        )
        assert decision.status_code == 200 and decision.json() == {"taskId": task_id, "status": "completed"}

        body, headers = envelope("tasks/get", {"taskId": task_id}, 3, name=task_id)
        completed = result_of(await http.post("/mcp", json=body, headers=headers))
        check_get_body(completed)
        assert completed["status"] == "completed" and completed["statusMessage"] == "Owner approved"
        assert completed["result"]["structuredContent"] == {
            "status": "approved",
            "note": "Done by 5pm",
            "reference": task_id,
        }
        assert completed["lastUpdatedAt"] >= completed["createdAt"]

        late = await http.post(f"/owner/tasks/{task_id}/decision", json={"action": "approve"})
        assert late.status_code == 409  # already terminal
        missing = await http.post("/owner/tasks/nope/decision", json={"action": "approve"})
        assert missing.status_code == 404
    assert [r.status for r in harness.records()] == ["completed"]


async def test_sdk_client_resolves_a_task_over_http(base_url: str) -> None:
    seen: list[TaskHandle | TaskState] = []

    async def owner_phone(state: TaskHandle | TaskState) -> None:
        seen.append(state)
        async with httpx.AsyncClient(base_url=base_url, timeout=10) as http:
            if isinstance(state, TaskHandle):
                await http.post(
                    f"/owner/tasks/{state.task_id}/decision", json={"action": "ask", "question": "Flavour?"}
                )
            elif state.status == "working":
                await http.post(
                    f"/owner/tasks/{state.task_id}/decision", json={"action": "approve", "note": "From phone"}
                )

    async def elicit(ctx: ClientRequestContext, params: types.ElicitRequestParams) -> types.ElicitResult:
        return types.ElicitResult(action="accept", content={"answer": "Lemon"})

    ext = TasksClientExtension(on_status=owner_phone, max_wait_s=10)
    async with Client(f"{base_url}/mcp", extensions=[ext], elicitation_callback=elicit) as client:
        assert client.protocol_version == PROTOCOL
        assert client.server_capabilities.extensions == {"io.modelcontextprotocol/tasks": {}}
        result = await client.call_tool("request_owner_approval", {"summary": "After-hours"})
        state = await get_task(client.session, seen[0].task_id)
    assert result.is_error is False
    assert result.structured_content == {"status": "approved", "note": "From phone", "reference": seen[0].task_id}
    assert [s.status for s in seen] == ["working", "input_required", "working", "completed"]
    assert state.status == "completed"


@pytest.mark.parametrize("mode", ["no-extension", "legacy"])
async def test_degradation_over_http(base_url: str, harness: Harness, mode: str) -> None:
    if mode == "legacy":
        client = Client(f"{base_url}/mcp", mode="legacy", extensions=[TasksClientExtension()])
    else:
        client = Client(f"{base_url}/mcp")
    async with client:
        result = await client.call_tool("request_owner_approval", {"summary": "x"})
        if mode == "legacy":
            with pytest.raises(MCPError) as missing:
                await get_task(client.session, "any")
            assert missing.value.code == METHOD_NOT_FOUND
    assert result.structured_content == {"status": "pending", "note": "sync fallback", "reference": "n/a"}
    assert harness.records() == []
