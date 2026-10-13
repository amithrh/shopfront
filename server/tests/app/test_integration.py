"""The app over the REAL collaborators (profile, domain, tools): skips until those packages land.

The demo's owner round trip: a customer's big-cake order becomes a pending request, the notifier's
signed decision link reaches the owner, the owner approves through the REST API, and the customer's
task completes with the approved result.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from askonce_server.app.main import create_app

pytestmark = pytest.mark.anyio

BAKERY = "thimbleberry-bakehouse"
BIG_CAKE = {
    "business_id": BAKERY,
    "item": "custom cake",
    "choices": {"size": "10in-round", "flavor": "vanilla-bean", "filling": "lemon-curd", "frosting": "cream-cheese"},
    "date": "2026-10-18",
    "details": {"servings": 30, "customer_name": "Sam Lee", "customer_phone": "+12175550100"},
}


def test_real_app_health_and_tools(http_app: TestClient) -> None:
    assert http_app.get("/health").json()["status"] == "ok"
    response = http_app.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                 "io.modelcontextprotocol/clientCapabilities": {}}},
        },
        headers={"accept": "application/json, text/event-stream", "mcp-protocol-version": "2026-07-28",
                 "mcp-method": "tools/list"},
    )
    assert response.status_code == 200 and len(response.json()["result"]["tools"]) == 16


async def test_owner_approves_through_the_api_and_the_task_completes(
    plain_client: Any, deps: Any, signer: Any, settings: Any
) -> None:
    result = await plain_client.call_tool("place_custom_order", BIG_CAKE)
    data = result.structured_content
    assert data["status"] == "pending_owner", data
    task_id = data["task_id"]

    notifier = deps.bookings.ctx.notifier  # the owner_web notifier recorded a signed decision link
    (message,) = notifier.messages_for(BAKERY)
    commitment_id = message.commitment_id
    assert (await deps.commitments.get(BAKERY, commitment_id)).task_id == task_id
    token = parse_qs(urlsplit(message.link).query)["token"][0]

    app = create_app(settings, deps=deps, signer=signer)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        inbox_token = signer.sign(business_id=BAKERY, scope="inbox")
        inbox = await api.get(f"/owner/{BAKERY}/inbox", headers={"authorization": f"Bearer {inbox_token}"})
        assert inbox.status_code == 200
        (item,) = inbox.json()["items"]
        assert item["commitment"]["commitment_id"] == commitment_id
        assert "approve" in item["allowed_decisions"]

        decided = await api.post(
            f"/owner/{BAKERY}/requests/{commitment_id}/decision",
            json={"decision": "approve", "note": "See you Sunday"},
            headers={"authorization": f"Bearer {token}"},  # the notification's decide link
        )
        assert decided.status_code == 200, decided.text
        assert decided.json()["commitment"]["status"] == "approved"

        again = await api.post(
            f"/owner/{BAKERY}/requests/{commitment_id}/decision",
            json={"decision": "decline"},
            headers={"authorization": f"Bearer {token}"},
        )
        assert again.status_code == 409

    record = await deps.task_manager.get(task_id)
    assert record.status == "completed"
    assert record.result["structuredContent"]["status"] == "approved"
