"""Acceptance tests: the demo flows end to end over the in-process MCP client (protocol 2026-07-28).

They skip (via `support.pending`) until the profile, domain, tools and app work packages have landed,
then pin the cross-package contract: grounded results, MRTR confirmation, owner approval as a task,
idempotent retries, and the no-extension fallback. Owner: tools work package.
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from support import ElicitationScript

pytestmark = pytest.mark.anyio

BAKERY = "thimbleberry-bakehouse"
# The gold bakery profile makes size, flavor, filling and frosting required option groups; without
# them the plan reports `missing_choice` and the reply is `needs_info` (asking "Which flavor...?").
BIG_CAKE = {
    "business_id": BAKERY,
    "item": "custom cake",
    "choices": {"size": "ten inch round", "flavor": "vanilla bean", "filling": "lemon curd", "frosting": "vanilla buttercream"},
    "date": "2026-10-18",
    "details": {"servings": 30, "customer_name": "Sam Lee", "customer_phone": "+12175550100"},
}


def _speak(result: Any) -> str:
    assert result.structured_content is not None, result
    return str(result.structured_content["speak"])


async def test_find_business_returns_live_bakery(client: Any) -> None:
    result = await client.call_tool("find_business", {"need": "gluten-free birthday cake"})
    data = result.structured_content
    assert result.is_error is False
    assert data["businesses"] and data["businesses"][0]["business_id"] == BAKERY
    assert "http" not in data["speak"]


async def test_unknown_fact_offers_handoff_not_a_guess(client: Any) -> None:
    result = await client.call_tool(
        "ask_policy", {"business_id": BAKERY, "question": "do you sell gift cards for the moon?"}
    )
    data = result.structured_content
    assert data["handoff"] is not None and data["handoff"]["tool"] == "ask_owner"
    assert data["facts"] == []


async def test_declined_confirmation_commits_nothing(client: Any, deps: Any, elicitation: ElicitationScript) -> None:
    elicitation.answers.append({"confirm": False})
    result = await client.call_tool("place_custom_order", BIG_CAKE)
    assert result.structured_content["status"] == "not_confirmed"
    items, _ = await deps.commitments.list_for_business(BAKERY)
    assert items == []


async def test_big_cake_becomes_owner_task_and_completes_on_approval(
    client: Any, deps: Any, elicitation: ElicitationScript
) -> None:
    from shopfront_server.domain.services.owner import OwnerDecisionInput

    async def owner_approves() -> None:
        for _ in range(200):
            items, _ = await deps.commitments.list_for_business(BAKERY, statuses=["pending_owner"])
            if items:
                await deps.owner.decide(BAKERY, items[0].commitment_id, OwnerDecisionInput(decision="approve", note="See you Sunday"))
                return
            await anyio.sleep(0.01)
        raise AssertionError("no pending commitment appeared")

    async with anyio.create_task_group() as tg:
        tg.start_soon(owner_approves)
        result = await client.call_tool("place_custom_order", BIG_CAKE)

    data = result.structured_content
    assert data["status"] == "approved"
    assert data["commitment"]["reference"]
    confirm_messages = [message for message, props in elicitation.seen if props == ["confirm"]]
    assert len(confirm_messages) == 1 and "Rosa" in confirm_messages[0]


async def test_retry_is_idempotent(plain_client: Any, deps: Any) -> None:
    first = (await plain_client.call_tool("place_custom_order", BIG_CAKE)).structured_content
    second = (await plain_client.call_tool("place_custom_order", BIG_CAKE)).structured_content
    assert first["status"] == second["status"] == "pending_owner"
    assert first["reference"] and second["reference"] is None  # anonymous replays carry no capabilities
    assert second["replayed"] is True
    items, _ = await deps.commitments.list_for_business(BAKERY)
    assert len(items) == 1


async def test_no_tasks_extension_falls_back_to_get_task(plain_client: Any) -> None:
    created = (await plain_client.call_tool("place_custom_order", BIG_CAKE)).structured_content
    assert created["status"] == "pending_owner" and created["task_id"]
    status = (await plain_client.call_tool("get_task", {"task_id": created["task_id"]})).structured_content
    assert status["task_status"] == "working"
