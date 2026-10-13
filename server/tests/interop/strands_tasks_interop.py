"""Interop: Strands' independent SEP-2663 client against a running AskOnce server.

Runs under the ONBOARDING venv (strands-agents 1.57.1 on mcp 2.1.1), not the server venv:

    onboarding/.venv/bin/python server/tests/interop/strands_tasks_interop.py http://127.0.0.1:<port> <admin-token>

The customer is a Strands `MCPClient` with `TasksConfig` (it advertises io.modelcontextprotocol/tasks on
2026-07-28) and an elicitation callback that accepts AskOnce's MRTR confirmation. The owner is plain HTTP:
the admin route mints an inbox link, the owner reads the inbox and approves through the signed REST API.

Two paths are exercised:
  1. transparent: `call_tool_sync` receives a task handle and polls `tasks/get` until the owner approves;
  2. protocol level: `submit_tool_sync` returns `MCPCreateTaskResult` (working), `get_task_sync` shows
     `working`, the owner approves, `get_task_sync` shows `completed` with the tool's structured result.

The last stdout line is a JSON summary that the pytest wrapper asserts on. Exit status 0 means every check passed.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import timedelta
from typing import Any

import httpx
import mcp_types as types
from strands.tools.mcp import MCPClient, TasksConfig

BAKERY = "thimbleberry-bakehouse"


def order(date: str, servings: int) -> dict[str, Any]:
    """A custom cake big enough to need the owner's approval."""
    return {
        "business_id": BAKERY,
        "item": "custom cake",
        "choices": {
            "size": "ten inch round",
            "flavor": "vanilla bean",
            "filling": "lemon curd",
            "frosting": "vanilla buttercream",
        },
        "date": date,
        "details": {"servings": servings, "customer_name": "Sam Lee", "customer_phone": "+12175550100"},
    }


class Owner:
    """The bakery owner's phone: an inbox link minted by the admin route, then signed REST decisions."""

    def __init__(self, base_url: str, admin_token: str) -> None:
        self.base_url = base_url
        minted = httpx.post(
            f"{base_url}/owner/{BAKERY}/links", headers={"authorization": f"Bearer {admin_token}"}, timeout=10
        )
        minted.raise_for_status()
        self.token = httpx.URL(minted.json()["inbox_url"]).params["token"]

    def pending(self) -> list[dict[str, Any]]:
        response = httpx.get(f"{self.base_url}/owner/{BAKERY}/inbox", params={"token": self.token}, timeout=10)
        response.raise_for_status()
        return list(response.json()["items"])

    def approve_next(self, *, already_decided: set[str], note: str, timeout_s: float = 20) -> str:
        """Wait for a new pending request, approve it, and return its commitment id."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            fresh = [i for i in self.pending() if i["commitment"]["commitment_id"] not in already_decided]
            if fresh:
                commitment_id = fresh[0]["commitment"]["commitment_id"]
                response = httpx.post(
                    f"{self.base_url}/owner/{BAKERY}/requests/{commitment_id}/decision",
                    json={"decision": "approve", "note": note},
                    headers={"authorization": f"Bearer {self.token}"},
                    timeout=10,
                )
                response.raise_for_status()
                already_decided.add(commitment_id)
                return commitment_id
            time.sleep(0.05)
        raise TimeoutError("no pending request reached the owner")


class Confirmations:
    """Strands elicitation callback: accept every confirmation (MRTR `input_required` rounds)."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def __call__(self, context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        self.messages.append(params.message)
        properties = sorted((getattr(params, "requested_schema", None) or {}).get("properties", {}))
        if properties == ["confirm"]:
            return types.ElicitResult(action="accept", content={"confirm": True})
        return types.ElicitResult(action="decline")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main(base_url: str, admin_token: str) -> dict[str, Any]:
    owner = Owner(base_url, admin_token)
    decided: set[str] = set()
    confirmations = Confirmations()
    client = MCPClient(
        url=f"{base_url}/mcp",
        elicitation_callback=confirmations,
        tasks_config=TasksConfig(
            poll_timeout=timedelta(seconds=30),
            request_timeout=timedelta(seconds=10),
            poll_interval=timedelta(milliseconds=50),
        ),
    )
    summary: dict[str, Any] = {}
    with client:
        tools = [t.tool_name for t in client.list_tools_sync()]
        check("place_custom_order" in tools and "get_task" in tools, f"tools: {tools}")
        summary["tools"] = len(tools)

        # 1. transparent: call_tool_sync polls the task until the owner approves
        approved: list[str] = []
        approver = threading.Thread(
            target=lambda: approved.append(owner.approve_next(already_decided=decided, note="Approved for Strands")),
            daemon=True,
        )
        approver.start()
        result = client.call_tool_sync("tu-1", "place_custom_order", order("2026-10-18", 30))
        approver.join(timeout=20)
        structured = result.get("structuredContent") or {}
        check(result["status"] == "success", f"call_tool_sync: {result}")
        check(structured.get("status") == "approved", f"call_tool_sync structured: {structured}")
        check(approved == [structured.get("reference")], f"approved {approved} vs {structured.get('reference')}")
        summary["call_tool_sync"] = {"status": structured["status"], "reference": structured["reference"]}

        # 2. protocol level: submit -> working handle -> get (working) -> owner approves -> get (completed)
        handle = client.submit_tool_sync("place_custom_order", order("2026-10-20", 40))
        check(type(handle).__name__ == "MCPCreateTaskResult", f"submit_tool_sync returned {type(handle).__name__}")
        check(handle.status == "working", f"handle status {handle.status}")
        state = client.get_task_sync(handle.task_id)
        check(state.status == "working", f"first get: {state.status}")
        reference = owner.approve_next(already_decided=decided, note="Approved too")
        deadline = time.monotonic() + 10
        while state.status == "working" and time.monotonic() < deadline:
            time.sleep(0.05)
            state = client.get_task_sync(handle.task_id)
        check(state.status == "completed" and state.result is not None, f"final get: {state.status}")
        final = state.result.structured_content or {}  # type: ignore[union-attr]
        check(final.get("status") == "approved" and final.get("reference") == reference, f"final result: {final}")
        summary["submit_tool_sync"] = {
            "handle": type(handle).__name__,
            "handle_status": handle.status,
            "ttl_ms": handle.ttl_ms,
            "poll_interval_ms": handle.poll_interval_ms,
            "final_status": state.status,
            "result_status": final["status"],
            "reference": final["reference"],
        }

    check(len(confirmations.messages) == 2, f"confirmations: {confirmations.messages}")
    check(all("Rosa" in m for m in confirmations.messages), f"confirmations: {confirmations.messages}")
    summary["confirmations"] = len(confirmations.messages)
    summary["ok"] = True
    return summary


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: strands_tasks_interop.py <base-url> <admin-token>")
    print(json.dumps(main(sys.argv[1], sys.argv[2])))
