"""The SDK client against the app served by uvicorn on a real 127.0.0.1 socket, in both protocol eras.

This pins the transport configuration that confirmations depend on: 2026-07-28 clients get MRTR
(`input_required` + retry, no session), and legacy 2025-11-25 clients get a synchronous
`elicitation/create` over stateful SSE (which needs `stateless_http=False, json_response=False`).
"""

from __future__ import annotations

from typing import Any

import httpx
import mcp_types as types
import pytest

from mcp import Client

pytestmark = pytest.mark.anyio


class Answers:
    """Elicitation callback answering every question with one fixed action; records the messages."""

    def __init__(self, action: str = "accept", confirm: bool = True) -> None:
        self.action = action
        self.confirm = confirm
        self.messages: list[str] = []

    async def __call__(self, context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        self.messages.append(params.message)
        if self.action != "accept":
            return types.ElicitResult(action=self.action)  # type: ignore[arg-type]
        return types.ElicitResult(action="accept", content={"confirm": self.confirm})


async def test_health_over_real_http(served: str) -> None:
    async with httpx.AsyncClient() as http:
        response = await http.get(f"{served}/health")
    assert response.status_code == 200 and response.json()["status"] == "ok"


async def test_modern_client_lists_tools_and_confirms(served: str) -> None:
    answers = Answers()
    async with Client(f"{served}/mcp", elicitation_callback=answers) as client:
        assert client.protocol_version == "2026-07-28"
        names = [tool.name for tool in (await client.list_tools()).tools]
        assert names[:2] == ["find_business", "get_business_info"] and "probe_commit" in names
        result = await client.call_tool("probe_commit", {"item": "cake"})
    assert result.is_error is False
    assert result.content[0].text == "committed cake"  # type: ignore[union-attr]
    assert answers.messages == ["Shall I probe cake?"]


async def test_modern_client_decline_commits_nothing(served: str) -> None:
    async with Client(f"{served}/mcp", elicitation_callback=Answers(action="decline")) as client:
        result = await client.call_tool("probe_commit", {"item": "cake"})
    assert result.is_error is False
    assert result.content[0].text == "not committed cake (decline)"  # type: ignore[union-attr]


async def test_legacy_client_uses_a_session_and_synchronous_elicitation(served: str) -> None:
    answers = Answers()
    async with Client(f"{served}/mcp", mode="legacy", elicitation_callback=answers) as client:
        assert client.protocol_version == "2025-11-25"
        names = [tool.name for tool in (await client.list_tools()).tools]
        assert len(names) == 17  # 16 AskOnce tools + the probe
        result = await client.call_tool("probe_commit", {"item": "bread"})
    assert result.content[0].text == "committed bread"  # type: ignore[union-attr]
    assert answers.messages == ["Shall I probe bread?"]


async def test_tasks_extension_is_advertised_over_http(served: str) -> None:
    async with Client(f"{served}/mcp") as client:
        extensions = client.server_capabilities.extensions or {}
    assert "io.modelcontextprotocol/tasks" in extensions
