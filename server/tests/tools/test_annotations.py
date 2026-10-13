"""Tool annotations as clients see them on `tools/list` (mcp-spec F10).

`openWorldHint` says whether a tool reaches beyond the server. Every tool that puts something in front
of a real business owner (a booking, an order, a quote request, a question, a reply, a cancellation)
is open-world; tools that only read the published profile or the server's own records are not.
"""

from __future__ import annotations

from typing import Any

import pytest
from support import ElicitationScript
from tools_support import Stack

pytestmark = pytest.mark.anyio

REACHES_THE_OWNER = {"book", "cancel_booking", "place_custom_order", "request_quote", "ask_owner", "reply_to_owner"}


async def _annotations(stack: Stack, elicit: ElicitationScript) -> dict[str, Any]:
    async with stack.client(elicit) as client:
        tools = (await client.list_tools()).tools
    assert all(t.annotations is not None for t in tools)
    return {t.name: t.annotations for t in tools}


async def test_round2_misc_low_open_world_is_exactly_the_tools_that_reach_the_owner(
    stack: Stack, elicit: ElicitationScript
) -> None:
    annotations = await _annotations(stack, elicit)
    assert {name for name, a in annotations.items() if a.open_world_hint} == REACHES_THE_OWNER
    for name, a in annotations.items():
        assert a.open_world_hint is not None, name  # explicit: the spec default is open-world
        if a.read_only_hint:
            assert a.open_world_hint is False and a.destructive_hint is False and a.idempotent_hint is True, name
        else:
            assert name in REACHES_THE_OWNER, name


async def test_round2_misc_low_ask_owner_is_not_idempotent_and_cancel_is_destructive(
    stack: Stack, elicit: ElicitationScript
) -> None:
    annotations = await _annotations(stack, elicit)
    # Without auth every ask_owner call mixes in a fresh nonce, so a retry asks the owner again.
    assert annotations["ask_owner"].idempotent_hint is False and annotations["ask_owner"].destructive_hint is False
    assert annotations["cancel_booking"].destructive_hint is True and annotations["cancel_booking"].idempotent_hint is True
    for name in ("book", "place_custom_order", "request_quote", "reply_to_owner"):
        assert annotations[name].idempotent_hint is True and annotations[name].destructive_hint is False, name


async def test_round2_misc_low_an_identical_ask_owner_retry_asks_again(stack: Stack, elicit: ElicitationScript) -> None:
    """Pins why ask_owner is not idempotent: two identical anonymous calls are two questions."""
    from tools_support import BAKERY, structured

    args = {"business_id": BAKERY, "question": "Do you deliver to Urbana?"}
    async with stack.client(elicit) as client:
        first = structured(await client.call_tool("ask_owner", args))
        second = structured(await client.call_tool("ask_owner", args))
    assert first["task_id"] != second["task_id"]
    inbox, _ = await stack.deps.owner.inbox(BAKERY)
    assert sum(item.commitment.kind == "owner_question" for item in inbox) == 2
