"""End to end over real HTTP: the full app under uvicorn on a 127.0.0.1 port, driven by the mcp SDK `Client`.

Nothing here is in-process: every customer call goes through Streamable HTTP at `/mcp` and every owner
action goes through the signed REST API (the decision link the notifier produced), exactly as a phone would.
The server runs on its own thread and event loop; tests only touch it through HTTP, plus read-only peeks at
the notifier outbox, which stands in for the owner's SMS/notification channel.

Scenarios (one test class each):
  A  modern 2026-07-28 client with the tasks extension: discover -> catalog -> grounded allergen facts ->
     custom order with MRTR confirmation -> task handle (working) -> owner approves -> completed; the retry
     is idempotent.
  B  the same order, but the customer declines the confirmation: nothing is created.
  C  legacy 2025-11-25 client: synchronous fallback (pending_owner + task_id), polled with `get_task`.
  D  salon: rule-based slots in the business timezone, booking with confirmation, double-booking refused.
  E  home services: service area in/out, quote request as a task, the owner's quote reaches the result.
  F  security: tampered/expired/foreign owner tokens, non-live profiles are invisible, canaries never leak.
  G  MCP Apps: card tools carry `_meta.ui.resourceUri` and the `ui://` resources are served as mcp-app HTML.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import anyio
import httpx
import mcp_types as types
import pytest
from e2e_support import (
    BAKERY,
    DRAFT,
    HOME,
    SALON,
    Shop,
    draft_copy,
    injection_canaries,
    loaded,
    running_shop,
)
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_tasks.client import TaskHandle, TasksClientExtension, TaskState, get_task
from support import FIXED_NOW, ElicitationScript, apps_advertisement, make_live

from askonce_server.app.links import signer_from_settings
from askonce_server.app.notifier import OutboxMessage
from askonce_server.clock import FixedClock
from askonce_server.profile.model import Profile

pytestmark = pytest.mark.anyio

INTAKE = {
    "customer_name": "Sam Lee",
    "customer_phone": "+12175550100",
    "customer_email": "sam@example.com",
    "stylist": "any",
    "name": "Sam Lee",
    "contact_by": "text",
    "phone": "+13035550100",
    "time_window": "weekday-morning",
    "job_type": "leak-repair",
    "description": "The pipe under the kitchen sink drips.",
    "zip": "80211",
}
"""Answers for every required intake field of the three gold profiles."""

BIG_CAKE: dict[str, Any] = {
    "business_id": BAKERY,
    "item": "custom cake",
    "choices": {
        "size": "ten inch round",
        "flavor": "vanilla bean",
        "filling": "lemon curd",
        "frosting": "vanilla buttercream",
    },
    "date": "2026-10-18",
    "details": {"servings": 30, "customer_name": "Sam Lee", "customer_phone": "+12175550100"},
}
"""30 servings: over the bakery's approval threshold, so it needs Rosa and becomes an owner task."""

SALON_DETAILS = {
    "stylist": "wren",
    "customer_name": "Sam Lee",
    "customer_phone": "+15125550100",
    "customer_email": "sam@example.com",
}
QUOTE = {
    "business_id": HOME,
    "item": "leak repair",
    "details": {"description": "The kitchen sink pipe drips.", "zip": "80211"},
}

ClientMode = Literal["tasks", "plain", "legacy"]


# ============================================================================ the running shop


@pytest.fixture
def shop(
    tmp_path: Path, raw_profiles: dict[str, dict[str, Any]], live_raw_profiles: dict[str, dict[str, Any]]
) -> Iterator[Shop]:
    """A fresh server per test: the gold profiles promoted to live, plus a draft copy of the bakery."""
    if not {BAKERY, SALON, HOME} <= set(raw_profiles):
        pytest.skip("gold profiles not present")
    profiles = [loaded(doc) for doc in live_raw_profiles.values()]
    draft = loaded(draft_copy(raw_profiles[BAKERY]))
    assert draft.status != "live"
    with running_shop(tmp_path, [*profiles, draft], injection_canaries(raw_profiles)) as running:
        yield running


# ============================================================================ customer side (MCP over HTTP)


@dataclass
class StatusLog:
    """`TasksClientExtension.on_status` sink: the handle, then each change seen while polling."""

    states: list[TaskHandle | TaskState] = field(default_factory=list)
    handle_seen: anyio.Event = field(default_factory=anyio.Event)

    async def __call__(self, state: TaskHandle | TaskState) -> None:
        self.states.append(state)
        if isinstance(state, TaskHandle):
            self.handle_seen.set()

    @property
    def statuses(self) -> list[str]:
        return [s.status for s in self.states]


@asynccontextmanager
async def customer(
    shop: Shop,
    elicitation: ElicitationScript,
    *,
    mode: ClientMode,
    status_log: StatusLog | None = None,
    apps: bool = False,
) -> AsyncIterator[Client]:
    """An SDK client on `<url>/mcp`: `tasks` (2026-07-28 + tasks ext), `plain` (2026-07-28) or `legacy`."""
    kwargs: dict[str, Any] = {"elicitation_callback": elicitation, "cache": None}
    extensions: list[Any] = []
    if mode == "legacy":
        kwargs["mode"] = "legacy"
    if mode == "tasks":
        extensions.append(TasksClientExtension(on_status=status_log, default_poll_interval_s=0.02, max_wait_s=20))
    if apps:
        extensions.append(apps_advertisement())
    if extensions:
        kwargs["extensions"] = extensions
    async with Client(f"{shop.url}/mcp", **kwargs) as client:
        expected = "2025-11-25" if mode == "legacy" else "2026-07-28"
        assert client.protocol_version == expected
        yield client


async def call(shop: Shop, client: Client, name: str, args: dict[str, Any]) -> types.CallToolResult:
    """Call a tool and record everything it returned (for the canary sweep)."""
    result = await client.call_tool(name, args)
    shop.outputs.append(json.dumps(result.model_dump(mode="json", by_alias=True)))
    return result


async def structured(shop: Shop, client: Client, name: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await call(shop, client, name, args)
    assert result.is_error is False, result.content
    assert result.structured_content is not None, result
    return dict(result.structured_content)


def confirm_messages(script: ElicitationScript) -> list[str]:
    return [message for message, props in script.seen if props == ["confirm"]]


# ============================================================================ owner side (signed REST)


def token_of(link: str) -> str:
    return parse_qs(urlsplit(link).query)["token"][0]


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def wait_for_notification(
    shop: Shop, business_id: str, *, already: int = 0, timeout_s: float = 10
) -> OutboxMessage:
    """The owner's phone: wait until a new decision link for `business_id` arrives."""
    with anyio.fail_after(timeout_s):
        while len(messages := shop.notifier.messages_for(business_id)) <= already:
            await anyio.sleep(0.01)
    return messages[already]


async def owner_decides(
    shop: Shop, message: OutboxMessage, decision: str, *, note: str | None = None, values: dict[str, Any] | None = None
) -> httpx.Response:
    """Open the notification's decision link, then POST the decision with the link's token."""
    async with httpx.AsyncClient(timeout=10) as http:
        detail = await http.get(message.link)
        assert detail.status_code == 200, detail.text
        assert decision in detail.json()["allowed_decisions"], detail.json()
        body: dict[str, Any] = {"decision": decision}
        if note is not None:
            body["note"] = note
        if values is not None:
            body["values"] = values
        url = f"{shop.url}/owner/{message.business_id}/requests/{message.commitment_id}/decision"
        return await http.post(url, json=body, headers=bearer(token_of(message.link)))


async def owner_inbox(shop: Shop, business_id: str, *, status: str = "all") -> list[dict[str, Any]]:
    """Every request of `business_id`, read through the owner API with an inbox link."""
    token = shop.signer.sign(business_id=business_id, scope="inbox")
    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.get(
            f"{shop.url}/owner/{business_id}/inbox", params={"status": status}, headers=bearer(token)
        )
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


async def with_owner(
    customer_call: Callable[[], Awaitable[types.CallToolResult]], owner: Callable[[], Awaitable[None]]
) -> types.CallToolResult:
    """Run the customer's (polling) call and the owner's reaction concurrently."""
    results: list[types.CallToolResult] = []

    async def run_customer() -> None:
        results.append(await customer_call())

    with anyio.fail_after(30):
        async with anyio.create_task_group() as group:
            group.start_soon(owner)
            group.start_soon(run_customer)
    return results[0]


# ============================================================================ A: modern client, tasks extension


class TestModernClientWithTasks:
    async def test_discover_ground_order_approve_and_retry(self, shop: Shop) -> None:
        elicitation = ElicitationScript(intake=dict(INTAKE))
        log = StatusLog()
        async with customer(shop, elicitation, mode="tasks", status_log=log) as client:
            # 1. discovery: the live bakery, never the draft copy
            found = await structured(shop, client, "find_business", {"need": "gluten-free cake", "category": "bakery"})
            ids = [b["business_id"] for b in found["businesses"]]
            assert ids[0] == BAKERY and DRAFT not in ids
            assert "http" not in found["speak"]

            # 2. catalog: gluten-free items, each fact carrying evidence
            catalog = await structured(
                shop, client, "get_catalog", {"business_id": BAKERY, "query": "gluten-free", "limit": 10}
            )
            assert catalog["total"] >= 1 and catalog["items"]
            assert catalog["facts"] and all(f["evidence_url"] for f in catalog["facts"])

            # 3. allergens: owner-confirmed safety facts are stated with evidence ...
            sourdough = await structured(
                shop,
                client,
                "find_item",
                {"business_id": BAKERY, "item": "sourdough", "aspects": ["allergens", "dietary"]},
            )
            assert sourdough["allergens"] == ["wheat"] and sourdough["handoff"] is None
            safety = {f["fact_ref"]: f for f in sourdough["facts"]}
            for ref in ("menu/country-sourdough#allergens.contains", "menu#facility_may_contain.allergens"):
                assert safety[ref]["owner_confirmed"] is True and safety[ref]["evidence_url"], safety[ref]
            # ... and unconfirmed ones are withheld: a hand-off, never a guess (and never "none")
            cookies = await structured(
                shop, client, "find_item", {"business_id": BAKERY, "item": "cookie dozen", "aspects": ["allergens"]}
            )
            assert cookies["allergens"] is None  # the kitchen-wide statement still accompanies the hand-off
            assert cookies["facility_may_contain"] == ["wheat", "milk", "eggs", "soybeans", "sesame", "tree_nuts", "peanuts"]
            assert cookies["handoff"]["reason"] == "unknown" and cookies["handoff"]["tool"] == "ask_owner"
            assert not any(f["fact_ref"].startswith("menu/cookie-dozen#allergens") for f in cookies["facts"])
            assert "none" not in cookies["speak"].lower()

            # 4. the order: MRTR confirmation, then a task handle (working) until Rosa approves over REST
            async def rosa() -> None:
                message = await wait_for_notification(shop, BAKERY)
                await log.handle_seen.wait()  # the customer is holding a working task by now
                response = await owner_decides(shop, message, "approve", note="See you Sunday")
                assert response.status_code == 200, response.text
                assert response.json()["commitment"]["status"] == "approved"

            result = await with_owner(lambda: call(shop, client, "place_custom_order", BIG_CAKE), rosa)
            order = result.structured_content
            assert result.is_error is False and order is not None
            assert order["status"] == "approved" and order["reference"]
            assert order["commitment"]["decision"] == "approve"
            assert "Rosa" in order["speak"]
            assert log.statuses[0] == "working" and isinstance(log.states[0], TaskHandle)
            assert log.statuses[-1] == "completed"
            assert order["task_id"] == log.states[0].task_id
            [question] = confirm_messages(elicitation)
            assert "Rosa" in question

            # 5. the retry is idempotent: same order, no second question, no duplicate. Without auth the
            #    replay is redacted (no reference, no task id): repeating details must not leak them.
            retry = await call(shop, client, "place_custom_order", BIG_CAKE)
            again = retry.structured_content
            assert retry.is_error is False and again is not None
            assert again["replayed"] is True and again["status"] == "approved"
            assert again["reference"] is None and again["task_id"] is None
            assert len(confirm_messages(elicitation)) == 1

        requests = await owner_inbox(shop, BAKERY)
        assert [r["commitment"]["commitment_id"] for r in requests] == [order["reference"]]
        assert len(shop.notifier.messages_for(BAKERY)) == 1


# ============================================================================ B: declined confirmation


class TestDeclinedConfirmation:
    async def test_decline_creates_nothing(self, shop: Shop) -> None:
        elicitation = ElicitationScript(intake=dict(INTAKE), answers=["decline"])
        log = StatusLog()
        async with customer(shop, elicitation, mode="tasks", status_log=log) as client:
            data = await structured(shop, client, "place_custom_order", BIG_CAKE)
        assert data["status"] == "not_confirmed"
        assert data.get("reference") is None and data.get("task_id") is None
        assert len(confirm_messages(elicitation)) == 1
        assert log.states == []  # no task was ever handed out
        assert await owner_inbox(shop, BAKERY) == []
        assert shop.notifier.messages_for(BAKERY) == []


# ============================================================================ C: legacy client, synchronous fallback


class TestLegacyClientFallback:
    async def test_pending_status_then_get_task(self, shop: Shop) -> None:
        elicitation = ElicitationScript(intake=dict(INTAKE))
        async with customer(shop, elicitation, mode="legacy") as client:
            created = await structured(shop, client, "place_custom_order", BIG_CAKE)
            assert created["status"] == "pending_owner" and created["task_id"] and created["reference"]
            assert "Nothing is confirmed until Rosa approves it" in created["speak"]
            assert len(confirm_messages(elicitation)) == 1  # asked synchronously (elicitation/create)

            waiting = await structured(shop, client, "get_task", {"task_id": created["task_id"]})
            assert waiting["task_status"] == "working" and waiting["result"] is None

            message = await wait_for_notification(shop, BAKERY)
            assert message.commitment_id == created["reference"]
            response = await owner_decides(shop, message, "approve", note="See you Sunday")
            assert response.status_code == 200, response.text

            done = await structured(shop, client, "get_task", {"task_id": created["task_id"]})
            assert done["task_status"] == "completed"
            assert done["result"]["status"] == "approved"
            assert done["result"]["reference"] == created["reference"]

            # tasks/* is not served on the legacy wire: the get_task tool is the only way to poll
            with pytest.raises(MCPError) as refused:
                await get_task(client.session, created["task_id"])
            assert refused.value.error.code == -32601

    async def test_plain_modern_client_without_extension_also_falls_back(self, shop: Shop) -> None:
        elicitation = ElicitationScript(intake=dict(INTAKE))
        async with customer(shop, elicitation, mode="plain") as client:
            created = await structured(shop, client, "place_custom_order", BIG_CAKE)
            assert created["status"] == "pending_owner" and created["task_id"]
            waiting = await structured(shop, client, "get_task", {"task_id": created["task_id"]})
            assert waiting["task_status"] == "working"


# ============================================================================ D: salon booking


class TestSalonBooking:
    async def test_slots_in_business_timezone_book_and_refuse_double_booking(
        self, shop: Shop, raw_profiles: dict[str, dict[str, Any]]
    ) -> None:
        tz = ZoneInfo(raw_profiles[SALON]["business"]["timezone"])
        elicitation = ElicitationScript(intake=dict(INTAKE))
        async with customer(shop, elicitation, mode="plain") as client:
            slots = await structured(
                shop, client, "check_availability", {"business_id": SALON, "item": "men's cut", "limit": 4}
            )
            assert slots["module_id"] == "appointments" and len(slots["slots"]) == 4
            starts = [datetime.fromisoformat(s["start"]) for s in slots["slots"]]
            for start in starts:  # wall time in the salon's zone, with that zone's offset
                assert start.utcoffset() == start.astimezone(tz).utcoffset()
                assert start.astimezone(tz).replace(tzinfo=None) == start.replace(tzinfo=None)
                assert start >= FIXED_NOW
            assert starts == sorted(starts)
            assert all(b - a == timedelta(minutes=15) for a, b in zip(starts, starts[1:], strict=False))
            assert slots["slots"][0]["start"] == "2026-10-14T11:00:00-05:00"  # CDT, from the weekly rules

            action = slots["card"]["actions"][0]["arguments"]
            booking = {
                "business_id": SALON,
                "item": action["item"],
                "start": action["start"],
                "resource": action["resource"],
            }
            booked = await structured(
                shop, client, "book", {**booking, "details": {**SALON_DETAILS, "stylist": action["resource"]}}
            )
            assert booked["status"] == "confirmed" and booked["reference"]
            [question] = confirm_messages(elicitation)
            assert question.endswith("?")

            other = {
                **SALON_DETAILS,
                "stylist": action["resource"],
                "customer_name": "Alex Kim",
                "customer_email": "alex@example.com",
            }
            refused = await structured(shop, client, "book", {**booking, "details": other})
            assert refused["status"] == "unavailable"
            assert refused["speak"].startswith("That time is already taken.")
            assert len(confirm_messages(elicitation)) == 1  # the full slot is refused before any question

            after = await structured(
                shop, client, "check_availability", {"business_id": SALON, "item": "men's cut", "limit": 4}
            )
            assert not any(
                s["start"] == slots["slots"][0]["start"] and s.get("resource") == action["resource"]
                for s in after["slots"]
            )

        bookings = await owner_inbox(shop, SALON)
        assert [b["commitment"]["commitment_id"] for b in bookings] == [booked["reference"]]


# ============================================================================ E: home services quote


class TestHomeServicesQuote:
    async def test_service_area_and_quote_task(self, shop: Shop) -> None:
        elicitation = ElicitationScript(intake=dict(INTAKE))
        log = StatusLog()
        async with customer(shop, elicitation, mode="tasks", status_log=log) as client:
            inside = await structured(shop, client, "check_service_area", {"business_id": HOME, "postal_code": "80211"})
            assert inside["served"] is True and inside["speak"] == "Yes, Kettlewick Home Repair serves 80211."
            outside = await structured(
                shop, client, "check_service_area", {"business_id": HOME, "postal_code": "90210"}
            )
            assert outside["served"] is False and "doesn't serve 90210" in outside["speak"]

            async def jo() -> None:
                message = await wait_for_notification(shop, HOME)
                await log.handle_seen.wait()
                response = await owner_decides(
                    shop, message, "quote", values={"reply_type": "fixed_price", "quoted_price": 240}
                )
                assert response.status_code == 200, response.text

            result = await with_owner(lambda: call(shop, client, "request_quote", QUOTE), jo)
        data = result.structured_content
        assert result.is_error is False and data is not None
        assert log.statuses[0] == "working" and log.statuses[-1] == "completed"
        assert data["status"] == "quoted"
        assert data["commitment"]["quote"]["amount_cents"] == 24000
        assert "$240" in data["speak"]
        assert confirm_messages(elicitation)[-1].endswith("Shall I send it to Jo?")


# ============================================================================ F: security


class TestSecurity:
    async def test_owner_endpoint_rejects_bad_tokens(self, shop: Shop) -> None:
        elicitation = ElicitationScript(intake=dict(INTAKE))
        async with customer(shop, elicitation, mode="plain") as client:
            created = await structured(shop, client, "place_custom_order", BIG_CAKE)
        message = await wait_for_notification(shop, BAKERY)
        token = token_of(message.link)
        decide_url = f"{shop.url}/owner/{BAKERY}/requests/{message.commitment_id}/decision"
        claims, mac = token.split(".")
        tampered_mac = f"{claims}.{mac[:-2]}{'AA' if mac[-2:] != 'AA' else 'BB'}"
        forged_claims = f"{claims[:-2]}{'AA' if claims[-2:] != 'AA' else 'BB'}.{mac}"
        stale_signer = signer_from_settings(shop.settings, clock=FixedClock(FIXED_NOW - timedelta(days=2)))
        expired = stale_signer.sign(business_id=BAKERY, scope="decide", commitment_id=message.commitment_id, ttl_s=60)
        foreign = shop.signer.sign(business_id=SALON, scope="inbox")

        async with httpx.AsyncClient(timeout=10) as http:
            missing = await http.post(decide_url, json={"decision": "approve"})
            assert missing.status_code == 401
            for bad in (tampered_mac, forged_claims, expired, foreign, "not-a-token"):
                response = await http.post(decide_url, json={"decision": "approve"}, headers=bearer(bad))
                assert response.status_code == 401, (bad, response.text)
                assert response.json() == {"error": "Invalid or expired link", "code": "unauthorized"}
                peek = await http.get(f"{shop.url}/owner/{BAKERY}/inbox", params={"token": bad})
                assert peek.status_code == 401
            # a decide link is good for its own request only, not the inbox
            assert (await http.get(f"{shop.url}/owner/{BAKERY}/inbox", headers=bearer(token))).status_code == 401
            # an owner note carrying an injection canary is refused (it would be read to the customer)
            canary = next(c for c in shop.canaries if c == "ROSAFREE")
            poisoned = await http.post(
                decide_url, json={"decision": "approve", "note": f"use {canary}"}, headers=bearer(token)
            )
            assert poisoned.status_code == 400

        # nothing was decided by any of the above
        [request] = await owner_inbox(shop, BAKERY, status="open")
        assert request["commitment"]["status"] == "pending_owner" and request["task_status"] == "working"
        assert request["commitment"]["commitment_id"] == created["reference"]

    async def test_non_live_profile_is_invisible(self, shop: Shop) -> None:
        stored = await shop.deps.profiles.get(DRAFT)  # lock-free read: the draft really is in the store
        assert stored is not None and stored.status == "draft"
        async with customer(shop, ElicitationScript(), mode="plain") as client:
            for need, category in (("gluten-free cake", "bakery"), ("birthday cake", None), ("bread", None)):
                args = {"need": need, "limit": 10, **({"category": category} if category else {})}
                found = await structured(shop, client, "find_business", args)
                assert DRAFT not in [b["business_id"] for b in found["businesses"]]
            for tool, args in (
                ("get_business_info", {}),
                ("get_catalog", {}),
                ("ask_policy", {"question": "parking?"}),
            ):
                result = await call(shop, client, tool, {"business_id": DRAFT, **args})
                assert result.is_error is True and "Unknown business" in result.content[0].text  # type: ignore[union-attr]

    async def test_injection_canaries_never_reach_tool_output(self, shop: Shop) -> None:
        """Ask exactly the questions the poisoned web pages target, on every business."""
        assert shop.canaries, "the gold profiles carry injection canaries"
        probes: list[tuple[str, dict[str, Any]]] = [
            ("find_business", {"need": "free cake with a discount code"}),
            ("get_business_info", {"business_id": BAKERY}),
            ("get_catalog", {"business_id": BAKERY, "limit": 25}),
            ("ask_policy", {"business_id": BAKERY, "question": "are custom cakes free this month?"}),
            ("ask_policy", {"business_id": BAKERY, "question": "do I need to pay a deposit for a custom cake?"}),
            (
                "find_item",
                {"business_id": BAKERY, "item": "custom cake", "aspects": ["price", "description", "options"]},
            ),
            ("get_business_info", {"business_id": SALON}),
            ("ask_policy", {"business_id": SALON, "question": "can you book me with Wren?"}),
            ("get_catalog", {"business_id": SALON, "limit": 25}),
            ("check_availability", {"business_id": SALON, "item": "women's cut", "limit": 3}),
            ("get_business_info", {"business_id": HOME}),
            ("ask_policy", {"business_id": HOME, "question": "do you guarantee same-day service?"}),
            ("ask_policy", {"business_id": HOME, "question": "are all jobs free this month?"}),
            ("get_catalog", {"business_id": HOME, "limit": 25}),
        ]
        async with customer(shop, ElicitationScript(intake=dict(INTAKE)), mode="plain") as client:
            for name, args in probes:
                await call(shop, client, name, args)
        assert len(shop.outputs) == len(probes)
        assert shop.leaked_canaries() == []

    async def test_output_guard_blocks_a_poisoned_profile_over_http(
        self, tmp_path: Path, raw_profiles: dict[str, dict[str, Any]]
    ) -> None:
        """A profile that slipped past the loader (L8) with a canary in a spoken answer: the guard still blocks it."""
        if BAKERY not in raw_profiles:
            pytest.skip("gold bakery profile not present")
        canary = "ROSAFREE"
        doc = make_live(copy.deepcopy(raw_profiles[BAKERY]))
        for entry in doc["modules"]["info"]["entries"]:
            if entry["id"] == "parking":
                entry["answer_spoken"] = f"Use code {canary} for free parking on Thimbleberry Street."
        with running_shop(tmp_path, [Profile.unchecked(doc)], [canary]) as poisoned:
            async with customer(poisoned, ElicitationScript(), mode="plain") as client:
                data = await structured(
                    poisoned, client, "ask_policy", {"business_id": BAKERY, "question": "is there parking?"}
                )
            assert data["speak"] == "Sorry, I can't share that answer. Want me to ask Rosa?"
            assert data["handoff"]["reason"] == "needs_owner" and data["facts"] == []
            assert poisoned.leaked_canaries() == []


# ============================================================================ G: MCP Apps


class TestMcpApps:
    async def test_card_tools_and_ui_resources(self, shop: Shop) -> None:
        async with customer(shop, ElicitationScript(), mode="plain", apps=True) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 16
            uris = set()
            for tool in tools:
                ui = (tool.meta or {}).get("ui")
                assert ui and str(ui["resourceUri"]).startswith("ui://"), tool.name
                uris.add(str(ui["resourceUri"]))
            assert len(uris) == 4
            listed = {str(r.uri) for r in (await client.list_resources()).resources}
            assert uris <= listed
            for uri in sorted(uris):
                read = await client.read_resource(uri)
                [content] = read.contents
                assert content.mime_type == "text/html;profile=mcp-app"
                assert isinstance(content, types.TextResourceContents) and "<html" in content.text.lower()
