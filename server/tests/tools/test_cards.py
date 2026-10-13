"""MCP Apps cards (`tools.cards`) and how the server publishes them."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from support import ElicitationScript, apps_advertisement
from tools_support import BAKERY, BIG_CAKE, Stack, structured

from mcp import Client
from mcp.server.apps import Apps
from shopfront_server.domain.services.owner import OwnerDecisionInput
from shopfront_server.tools import cards
from shopfront_server.tools.server import TOOLS

pytestmark = pytest.mark.anyio

EXPECTED_CARDS = {
    "find_business": cards.BUSINESS_CARD,
    "get_business_info": cards.BUSINESS_CARD,
    "get_catalog": cards.ITEMS_CARD,
    "find_item": cards.ITEMS_CARD,
    "ask_policy": cards.BUSINESS_CARD,
    "check_availability": cards.COMMITMENT_CARD,
    "book": cards.COMMITMENT_CARD,
    "cancel_booking": cards.COMMITMENT_CARD,
    "place_custom_order": cards.COMMITMENT_CARD,
    "check_service_area": cards.BUSINESS_CARD,
    "request_quote": cards.COMMITMENT_CARD,
    "ask_owner": cards.TASK_CARD,
    "get_task": cards.TASK_CARD,
    "reply_to_owner": cards.TASK_CARD,
}


@pytest.mark.parametrize("variant", list(cards.CARD_URIS))
def test_card_html_is_self_contained_and_safe(variant: Any) -> None:
    html = cards.card_html(variant)
    assert html.startswith("<!doctype html>") and f'data-variant="{variant}"' in html
    assert f"<title>{cards.TITLES[variant]}</title>" in html
    for forbidden in ("https://", "http://", "<link", "@import", "url(", "src=", "fetch(", "XMLHttpRequest", "innerHTML", "eval("):
        assert forbidden not in html, forbidden
    assert "textContent" in html
    for method in ("ui/initialize", "ui/notifications/initialized", "ui/notifications/tool-result", "ui/notifications/size-changed"):
        assert method in html
    assert re.search(r"\{[a-z_]+\}", html.split("<script>")[0]) is None  # no unformatted placeholders


def test_every_tool_is_bound_to_its_card() -> None:
    assert {spec.fn.__name__: spec.card for spec in TOOLS} == EXPECTED_CARDS


def test_register_cards_registers_four_app_resources() -> None:
    apps = Apps()
    cards.register_cards(apps)
    resources = {str(binding.resource.uri): binding.resource for binding in apps.resources()}
    assert set(resources) == set(cards.CARD_URIS.values())
    assert {r.mime_type for r in resources.values()} == {"text/html;profile=mcp-app"}


async def test_server_serves_the_cards(mcp_server: Any) -> None:
    async with Client(mcp_server, extensions=[apps_advertisement()], cache=None) as client:
        for uri in cards.CARD_URIS.values():
            read = await client.read_resource(uri)
            content = read.contents[0]
            assert content.mime_type == "text/html;profile=mcp-app"
            assert content.text == cards.card_html(next(v for v, u in cards.CARD_URIS.items() if u == uri))
            assert content.meta == {"ui": {"prefersBorder": True}}
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name, uri in EXPECTED_CARDS.items():
            assert tools[name].meta["ui"] == {"resourceUri": uri, "visibility": ["model", "app"]}


# ---------------------------------------------------------------------------- round 2: request_info


def test_round2_request_info_the_card_script_handles_action_inputs() -> None:
    """R3 (static half): actions that declare an `input` get a textarea (built with createElement, not
    markup) and the text is merged into the tool arguments under `input.key`."""
    script = cards.card_html("task").split("<script>")[1]
    assert 'el("textarea")' in script and "field.maxLength = a.input.max_length" in script
    assert "args[a.input.key] = text" in script and "send.disabled = !field.value.trim()" in script


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the card script")
async def test_round2_request_info_the_task_card_collects_the_answer_before_calling_the_tool(
    stack: Stack, elicit: ElicitationScript, tmp_path: Path
) -> None:
    """R3: the task card's Answer button used to call reply_to_owner with only the task id, so it always
    failed ('That action isn't available here'). Runs the real card script (Node, minimal DOM stub) on
    a real get_task result and sends what it posts back to the server."""
    async with stack.client(elicit) as client:
        created = structured(await client.call_tool("place_custom_order", BIG_CAKE))
        await stack.deps.owner.decide(
            BAKERY, created["reference"], OwnerDecisionInput(decision="request_info", question="Is Sunday a firm date?")
        )
        status = await client.call_tool("get_task", {"task_id": created["task_id"]})
    task_id = created["task_id"]
    page = tmp_path / "task-card.html"
    page.write_text(cards.card_html("task"))
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"structuredContent": status.structured_content}))
    run = subprocess.run(
        ["node", str(Path(__file__).with_name("card_harness.js")), str(page), str(result)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert run.returncode == 0 and run.stdout, run.stderr or "the card script never finished"
    report = json.loads(run.stdout)
    assert report["textareas"] == [{"id": "reply-1", "maxLength": 1000}]
    assert report["labels"] == [{"text": "Your answer", "htmlFor": "reply-1"}]
    assert [b["text"] for b in report["buttons"]] == ["Answer", "Check again"]
    steps = report["steps"]
    assert steps["blankClickSent"] == [] and steps["disabledWhenBlank"] is True and steps["disabledWithText"] is False
    assert steps["sent"] == [{"name": "reply_to_owner", "arguments": {"task_id": task_id, "answer": "Yes, Sunday is firm."}}]
    assert steps["disabledWhileSending"] is True
    assert steps["plainSent"] == [{"name": "get_task", "arguments": {"task_id": task_id}}]

    async with stack.client(elicit) as client:  # the host proxies the card's tools/call to the server
        sent = steps["sent"][0]
        replied = structured(await client.call_tool(sent["name"], sent["arguments"]))
    assert replied["task_status"] == "working"
    assert replied["speak"] == "Thanks, I've passed that on to Rosa. I'll let you know what they decide."
