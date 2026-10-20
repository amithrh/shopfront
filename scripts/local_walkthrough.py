"""Play a full customer + owner conversation against a running local AskOnce server.

Prints what a voice assistant would say at each step (each tool result's `speak`), the confirmation
questions the server asks (MRTR elicitation), and the owner's side (inbox + approve) through the
owner REST API. Uses a plain 2026-07-28 MCP client without the tasks extension, so it exercises the
same fallback a host like Alexa+ would most likely use: `pending_owner` + polling `get_task`.

Prereqs: the server running locally (see docs/local-testing.md) with the certified live profiles (profiles/live/).
Usage:   server/.venv/bin/python scripts/local_walkthrough.py [--url http://localhost:8000]
"""

from __future__ import annotations

import argparse
import asyncio
import textwrap
from typing import Any

import httpx
from urllib.parse import parse_qs, urlsplit
import mcp_types as types
from mcp import Client

ADMIN_TOKEN = "DEV-ONLY-admin-token"  # the server's dev default; prod refuses to start with it

INTAKE = {
    "customer_name": "Sam Lee",
    "customer_phone": "+12175550100",
    "customer_email": "sam@example.com",
    "name": "Sam Lee",
    "contact_by": "text",
    "phone": "+13035550100",
    "stylist": "any",
    "time_window": "weekday-morning",
    "job_type": "leak-repair",
    "description": "The pipe under the kitchen sink drips.",
    "zip": "80211",
}


def say(who: str, text: str) -> None:
    print(textwrap.fill(text, width=100, initial_indent=f"{who:>10}: ", subsequent_indent=" " * 12))


async def on_elicit(_context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
    schema = getattr(params, "requested_schema", None) or {}
    props = sorted(schema.get("properties", {}))
    say("assistant", f"[asks] {params.message}")
    if props == ["confirm"]:
        say("customer", "Yes, go ahead.")
        return types.ElicitResult(action="accept", content={"confirm": True})
    answer = {k: v for k, v in INTAKE.items() if k in props}
    say("customer", f"(fills in {', '.join(answer) or 'nothing'})")
    return types.ElicitResult(action="accept", content=answer)


async def tool(client: Client, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, args)
    data = dict(result.structured_content or {})
    if result.is_error:
        text = " ".join(getattr(c, "text", "") for c in result.content)
        say("ERROR", f"{name}: {text}")
        return data
    if data.get("speak"):
        say("assistant", data["speak"])
    return data


def _sample(field: dict[str, Any]) -> Any:
    """A plausible owner answer for one response field of the quote form."""
    kind = field.get("type")
    if kind == "money":
        return 185
    if kind in ("integer", "number"):
        return 2
    if kind == "choice":
        return field["options"][0]["value"]
    if kind == "boolean":
        return True
    return "Fixed price, includes parts."


async def owner_approve(url: str, business_id: str, note: str) -> None:
    async with httpx.AsyncClient(timeout=10) as http:
        minted = await http.post(
            f"{url}/owner/{business_id}/links", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
        )
        minted.raise_for_status()
        token = parse_qs(urlsplit(minted.json()["inbox_url"]).query)["token"][0]
        auth = {"Authorization": f"Bearer {token}"}
        inbox = (await http.get(f"{url}/owner/{business_id}/inbox", params={"status": "open"}, headers=auth)).json()
        for item in inbox["items"]:
            c = item["commitment"]
            print(f"{'owner':>10}: inbox -> {c['kind']} {c['commitment_id']} {c.get('item_ref') or ''} "
                  f"{c.get('choices') or ''} {c.get('day') or ''} allowed={item['allowed_decisions']}")
            decision = "quote" if "quote" in item["allowed_decisions"] else "approve"
            body: dict[str, Any] = {"decision": decision, "note": note}
            if decision == "quote":
                body["values"] = {f["key"]: _sample(f) for f in item["response_fields"]}
                print(f"{'owner':>10}: fills quote form {body['values']}")
            r = await http.post(
                f"{url}/owner/{business_id}/requests/{c['commitment_id']}/decision", json=body, headers=auth
            )
            print(f"{'owner':>10}: taps {decision.upper()} -> HTTP {r.status_code}")
            if r.status_code >= 400:
                print(f"{'':>12}{r.text[:300]}")


async def main(url: str) -> None:
    async with Client(f"{url}/mcp", elicitation_callback=on_elicit, cache=None) as client:
        print(f"Connected: protocol {client.protocol_version}\n")

        print("=== 1. Bakery: gluten-free cake for 30 (needs the owner's okay) ===")
        say("customer", "Can a bakery in Portland do a gluten-free birthday cake for 30?")
        found = await tool(client, "find_business", {"need": "gluten-free birthday cake for 30", "locality": "Portland"})
        biz = found["businesses"][0]["business_id"]
        await tool(client, "find_item", {"business_id": biz, "item": "gluten-free cake", "aspects": ["allergens", "dietary", "price"]})
        order = await tool(client, "place_custom_order", {
            "business_id": biz, "item": "custom cake",
            "choices": {"size": "ten inch round", "flavor": "vanilla bean", "filling": "lemon curd",
                        "frosting": "vanilla buttercream"},
            "date": "2026-10-18",
            "details": {"servings": 30, "customer_name": "Sam Lee", "customer_phone": "+12175550100"},
        })
        task_id = order.get("task_id")
        if task_id:
            await tool(client, "get_task", {"task_id": task_id})
            print("\n    ... Rosa's phone buzzes ...")
            await owner_approve(url, biz, "Happy to! See you Sunday.")
            say("customer", "Any news on my cake?")
            await tool(client, "get_task", {"task_id": task_id})

        print("\n=== 2. Salon: book a cut ===")
        found = await tool(client, "find_business", {"need": "women's haircut", "locality": "Austin"})
        salon = found["businesses"][0]["business_id"]
        slots = await tool(client, "check_availability", {"business_id": salon, "item": "women's cut",
                                                          "date_from": "2026-10-16", "date_to": "2026-10-17"})
        first = (slots.get("slots") or [{}])[0]
        if first.get("start"):
            await tool(client, "book", {"business_id": salon, "item": "women's cut", "start": first["start"],
                                        "details": {"stylist": "any", "customer_name": "Sam Lee",
                                                    "customer_phone": "+15125550100"}})

        print("\n=== 3. Home repair: leaky pipe, quote from the owner ===")
        found = await tool(client, "find_business", {"need": "leaking pipe under the sink", "locality": "80211"})
        home = found["businesses"][0]["business_id"]
        await tool(client, "check_service_area", {"business_id": home, "postal_code": "80211"})
        quote = await tool(client, "request_quote", {"business_id": home, "item": "leak repair",
                                                     "details": {"description": "The kitchen sink pipe drips.",
                                                                 "zip": "80211"}})
        if quote.get("task_id"):
            print("\n    ... Jo's phone buzzes ...")
            await owner_approve(url, home, "Can come Thursday morning.")
            await tool(client, "get_task", {"task_id": quote["task_id"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    asyncio.run(main(parser.parse_args().url))
