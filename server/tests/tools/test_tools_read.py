"""Read-only tools over MCP (in-process, protocol 2026-07-28) on the gold profiles."""

from __future__ import annotations

import json
from typing import Any

import pytest
from support import ElicitationScript
from tools_support import BAKERY, HOME, SALON, Stack, assert_grounded, structured

pytestmark = pytest.mark.anyio


async def call(client: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return structured(await client.call_tool(name, args))


# ---------------------------------------------------------------------------- discovery


@pytest.mark.parametrize(
    ("need", "winner"),
    [("gluten-free birthday cake", BAKERY), ("leaking water heater", HOME), ("haircut", SALON)],
)
async def test_find_business_ranking(client: Any, deps: Any, need: str, winner: str) -> None:
    data = await call(client, "find_business", {"need": need})
    assert data["businesses"][0]["business_id"] == winner
    assert data["businesses"][0]["matched_on"]
    assert data["speak"].startswith(data["businesses"][0]["spoken_name"])
    assert data["card"]["actions"][0] == {
        "label": f"About {data['businesses'][0]['name']}",
        "tool": "get_business_info",
        "arguments": {"business_id": winner},
    }
    await assert_grounded(deps, data)


async def test_find_business_without_match(client: Any) -> None:
    data = await call(client, "find_business", {"need": "used car dealership"})
    assert data["businesses"] == [] and data["facts"] == []
    assert data["speak"] == "I couldn't find a certified business for that yet. Try describing it another way."


async def test_business_info_hours_and_open_now(client: Any, deps: Any) -> None:
    data = await call(client, "get_business_info", {"business_id": BAKERY, "topics": ["hours"]})
    # Wednesday 14 Oct 2026 09:00 in Portland: open 7 am to 3 pm.
    assert data["open_now"] is True
    assert data["speak"] == (
        "Thimbleberry Bakehouse is open now, until 3 pm. Hours are Tuesday to Friday, 7 am to 3 pm; Saturday, 8 am to 6 pm; "
        "Sunday, 8 am to 2 pm."
    )
    assert data["hours"][0] == {"days": ["tue", "wed", "thu", "fri"], "open": "07:00", "close": "15:00"}
    assert "locations/main#hours.weekly" in [f["fact_ref"] for f in data["facts"]]
    assert data["card"]["badges"] == ["Open now"] and data["card"]["citations"]
    await assert_grounded(deps, data)


async def test_business_info_location_and_contact(client: Any, deps: Any) -> None:
    data = await call(client, "get_business_info", {"business_id": SALON, "topics": ["location", "contact"]})
    assert data["address"] == "1906 East Marigold Street, Suite B, Austin, TX 78702"
    assert data["phone"] == "+15125550147"
    assert data["speak"] == (
        "Quillfern Hair Studio is on East Marigold Street, East Austin. You can call Quillfern Hair Studio at 512-555-0147."
    )
    await assert_grounded(deps, data)


async def test_business_info_for_a_closure_day(client: Any) -> None:
    data = await call(client, "get_business_info", {"business_id": BAKERY, "topics": ["hours"], "on_date": "2026-11-26"})
    assert data["speak"] == "Thimbleberry Bakehouse is closed on Thursday, November 26 for Thanksgiving Day."
    assert data["open_now"] is None
    assert "locations/main#hours.closures" in [f["fact_ref"] for f in data["facts"]]


async def test_disputed_hours_are_withheld(disputed_stack: Stack) -> None:
    async with disputed_stack.client(ElicitationScript()) as client:
        data = await call(client, "get_business_info", {"business_id": BAKERY})
    assert data["hours"] is None and data["open_now"] is None
    assert data["handoff"]["reason"] == "disputed" and data["handoff"]["fact_ref"] == "locations/main#hours.weekly"
    assert data["speak"].startswith("Rosa is double-checking the opening hours, so I can't say for sure yet.")
    assert data["speak"].endswith("Want me to ask Rosa?")
    text = json.dumps(data)
    for leaked in ("6 pm", "4 pm", "18:00", "16:00", "3 pm"):
        assert leaked not in text
    await assert_grounded(disputed_stack.deps, data)


async def test_bad_on_date_is_a_tool_error(client: Any) -> None:
    result = await client.call_tool("get_business_info", {"business_id": BAKERY, "on_date": "next tuesday"})
    assert result.is_error and "on_date must be a date" in result.content[0].text


# ---------------------------------------------------------------------------- catalog


async def test_catalog_by_category(client: Any, deps: Any) -> None:
    data = await call(client, "get_catalog", {"business_id": BAKERY, "category": "breads", "limit": 3})
    assert data["total"] == 6 and len(data["items"]) == 3
    assert data["items"][0]["item_ref"] == "menu/country-sourdough"
    assert data["items"][0]["price"] == {"kind": "fixed", "currency": "USD", "amount_cents": 900, "max_cents": None, "per": "each"}
    assert data["speak"].startswith("Thimbleberry Bakehouse has 6 breads. They include country sourdough for $9")
    await assert_grounded(deps, data)


async def test_catalog_query_uses_owner_confirmed_dietary_tags(client: Any) -> None:
    data = await call(client, "get_catalog", {"business_id": BAKERY, "query": "vegan", "limit": 25})
    refs = {i["item_ref"] for i in data["items"]}
    assert {"menu/country-sourdough", "menu/vegan-blueberry-muffin", "menu/ginger-molasses"} <= refs
    assert "menu/butter-croissant" not in refs


async def test_catalog_miss_is_a_not_offered_handoff(client: Any) -> None:
    data = await call(client, "get_catalog", {"business_id": BAKERY, "query": "sushi"})
    assert data["total"] == 0 and data["items"] == []
    assert data["handoff"]["reason"] == "not_offered"
    # The customer's words are never spoken back as if the bakery said them (SEC-6).
    assert data["speak"] == "I couldn't find that at Thimbleberry Bakehouse. Want me to ask Rosa?"
    assert data["handoff"]["suggested_question"] == "Do you have sushi?"


async def test_allergens_always_carry_the_kitchen_statement(client: Any, deps: Any) -> None:
    data = await call(client, "find_item", {"business_id": BAKERY, "item": "sourdough", "aspects": ["allergens", "dietary"]})
    assert data["allergens"] == ["wheat"] and data["dietary"] == ["vegan"]
    assert data["facility_may_contain"] == ["wheat", "milk", "eggs", "soybeans", "sesame", "tree_nuts", "peanuts"]
    assert data["speak"] == (
        "The country sourdough contains wheat. The kitchen also handles wheat, milk, eggs, soy, sesame, tree nuts "
        "and peanuts. The country sourdough is vegan."
    )
    refs = [f["fact_ref"] for f in data["facts"]]
    assert "menu#facility_may_contain.allergens" in refs and "menu/country-sourdough#allergens.contains" in refs
    assert data["handoff"] is None
    await assert_grounded(deps, data)


async def test_unconfirmed_allergens_are_a_handoff_never_none(client: Any, deps: Any) -> None:
    data = await call(client, "find_item", {"business_id": BAKERY, "item": "cookie dozen", "aspects": ["allergens"]})
    assert data["allergens"] is None
    assert data["facility_may_contain"] == ["wheat", "milk", "eggs", "soybeans", "sesame", "tree_nuts", "peanuts"]
    assert data["handoff"]["reason"] == "unknown"
    assert data["handoff"]["fact_ref"] == "menu/cookie-dozen#allergens.contains"
    assert data["speak"] == (
        "I don't know the allergens in the cookie dozen. The kitchen also handles wheat, milk, eggs, soy, sesame, "
        "tree nuts and peanuts. Want me to ask Rosa?"
    )
    assert "none" not in data["speak"].lower()
    await assert_grounded(deps, data)


async def test_item_price_and_options(client: Any, deps: Any) -> None:
    data = await call(client, "find_item", {"business_id": BAKERY, "item": "birthday cake"})
    assert data["item"]["item_ref"] == "menu/custom-cake"
    assert data["speak"] == (
        "The custom cake starts at $56. You choose the size, cake flavor, filling, frosting and extras. The size can be "
        "six inch round for $56, eight inch round for $84, ten inch round for $120 or 4 other options."
    )
    size = data["options"][0]
    assert size["group_id"] == "size" and size["required"] is True
    assert size["choices"][2] == {
        "choice_id": "10in-round",
        "spoken_name": "ten inch round",
        "price": {"kind": "fixed", "currency": "USD", "amount_cents": 12000, "max_cents": None, "per": None},
        "price_delta": None,
    }
    assert data["offered_via"] == ["custom-cakes"]
    assert data["card"]["actions"][0]["tool"] == "place_custom_order"
    await assert_grounded(deps, data)


async def test_ambiguous_and_unknown_items(client: Any) -> None:
    data = await call(client, "find_item", {"business_id": BAKERY, "item": "cake"})
    assert data["item"] is None
    assert data["speak"] == "Did you mean slice of cake, six inch case cake or custom cake?"
    data = await call(client, "find_item", {"business_id": BAKERY, "item": "pizza"})
    assert data["handoff"]["reason"] == "not_offered"
    assert data["speak"] == "I don't see that at Thimbleberry Bakehouse. Want me to ask Rosa?"


async def test_service_duration(client: Any) -> None:
    data = await call(client, "find_item", {"business_id": SALON, "item": "men's cut", "aspects": ["duration", "price"]})
    assert data["duration_minutes"] == 30
    assert data["speak"] == "The men's cut takes about 30 minutes. The men's cut is $42."


# ---------------------------------------------------------------------------- knowledge


async def test_policy_answer_is_verbatim(client: Any, deps: Any) -> None:
    data = await call(client, "ask_policy", {"business_id": BAKERY, "question": "How much is the deposit?"})
    profile = await deps.profiles.get_live(BAKERY)
    entry = profile.knowledge_entry("info/deposit")
    assert data["entry_ref"] == "info/deposit" and data["topic"] == "deposit"
    assert data["speak"] == entry["answer_spoken"]
    refs = [f["fact_ref"] for f in data["facts"]]
    assert refs[0] == "info/deposit#answer_spoken"
    assert "info/deposit#facts.deposit_percent" in refs
    assert data["card"]["lines"][0]["value"] == entry["answer_card"]  # longer card text, never spoken
    await assert_grounded(deps, data)


@pytest.mark.parametrize(
    ("business", "question", "entry"),
    [
        (SALON, "Do you take walk-ins?", "info/walk-ins"),
        (SALON, "can i cancel my appointment", "info/cancellation"),
        (HOME, "are you licensed and insured", "policies/licensing"),
        (BAKERY, "can I bring my dog?", "info/dogs"),
    ],
)
async def test_policy_matching(client: Any, business: str, question: str, entry: str) -> None:
    data = await call(client, "ask_policy", {"business_id": business, "question": question})
    assert data["entry_ref"] == entry


async def test_unknown_policy_is_a_handoff(client: Any) -> None:
    data = await call(client, "ask_policy", {"business_id": SALON, "question": "Do you validate parking at the airport?"})
    assert data["handoff"] is not None and data["handoff"]["approver_spoken_name"] == "Inés"
    assert data["facts"] == [] and data["entry_ref"] is None


async def test_disputed_policy_is_withheld(disputed_stack: Stack) -> None:
    async with disputed_stack.client(ElicitationScript()) as client:
        data = await call(client, "ask_policy", {"business_id": SALON, "question": "What is your cancellation policy?"})
    assert data["handoff"]["reason"] == "disputed"
    assert data["entry_ref"] == "info/cancellation"
    assert "24" not in data["speak"] and "hour" not in data["speak"]
    assert data["facts"] == []


# ---------------------------------------------------------------------------- availability & service area


async def test_open_times_for_a_booking(client: Any, deps: Any) -> None:
    data = await call(client, "check_availability", {"business_id": SALON, "item": "men's cut", "limit": 4})
    assert data["module_id"] == "appointments" and data["item_ref"] == "services/mens-cut"
    assert [s["kind"] for s in data["slots"]] == ["time"] * 4
    assert data["slots"][0]["start"] == "2026-10-14T11:00:00-05:00"
    assert data["slots"][0]["resource_spoken_name"] == "June"
    assert data["speak"] == "The next open times for the men's cut are today at 11 am, 11:15 am and 11:30 am with June."
    assert data["card"]["actions"][0]["arguments"] == {
        "business_id": SALON, "item": "services/mens-cut", "module_id": "appointments", "start": "2026-10-14T11:00", "resource": "june"
    }
    await assert_grounded(deps, data)


async def test_open_days_for_custom_orders_and_quotes(client: Any) -> None:
    cake = await call(client, "check_availability", {"business_id": BAKERY, "item": "custom cake", "limit": 3})
    assert [s["date"] for s in cake["slots"]] == ["2026-10-17", "2026-10-18", "2026-10-20"]
    assert cake["speak"] == "The next open days for the custom cake are Saturday, Sunday and Tuesday."
    quote = await call(client, "check_availability", {"business_id": HOME, "item": "leak repair", "limit": 2})
    assert [s["kind"] for s in quote["slots"]] == ["day", "day"] and quote["module_id"] == "quotes"


async def test_availability_for_items_that_cannot_be_booked(client: Any) -> None:
    data = await call(client, "check_availability", {"business_id": BAKERY, "item": "baguette"})
    assert data["handoff"]["reason"] == "not_offered" and data["slots"] == []
    assert data["speak"] == "I can't book or order the baguette through Thimbleberry Bakehouse. Want me to ask Rosa?"
    bad = await client.call_tool("check_availability", {"business_id": BAKERY, "item": "custom cake", "date_from": "soon"})
    assert bad.is_error and "date_from must be a local date" in bad.content[0].text


@pytest.mark.parametrize(
    ("args", "served", "speak"),
    [
        ({"postal_code": "80211"}, True, "Yes, Kettlewick Home Repair serves 80211."),
        ({"postal_code": "90210"}, False, "Sorry, Kettlewick Home Repair doesn't serve 90210."),
        ({}, None, "What's the ZIP code or town where the work is?"),
    ],
)
async def test_service_area(client: Any, deps: Any, args: dict[str, Any], served: bool | None, speak: str) -> None:
    data = await call(client, "check_service_area", {"business_id": HOME, **args})
    assert data["served"] is served and data["speak"] == speak
    await assert_grounded(deps, data)


async def test_service_area_at_a_shop_answers_from_its_delivery_policy(client: Any, deps: Any) -> None:
    # "Do you deliver to Beaverton?" routed to check_service_area at a bakery: the website answers it
    # (pickup only), so the reply is that policy, cited, not a hand-off to the owner.
    data = await call(client, "check_service_area", {"business_id": BAKERY, "locality": "Beaverton"})
    policy = await call(client, "ask_policy", {"business_id": BAKERY, "question": "Do you deliver to Beaverton?"})
    assert data["handoff"] is None and data["served"] is None
    assert data["speak"] == policy["speak"] and data["facts"] == policy["facts"]
    assert data["card"]["citations"]
    await assert_grounded(deps, data)


async def test_service_area_at_a_salon_without_a_matching_policy_still_hands_off(client: Any, deps: Any) -> None:
    data = await call(client, "check_service_area", {"business_id": SALON, "locality": "Round Rock"})
    assert data["handoff"]["reason"] == "not_offered" and data["handoff"]["tool"] == "ask_owner"
    await assert_grounded(deps, data)


# ---------------------------------------------------------------------------- tenant gate & grounding sweep


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("get_business_info", {}),
        ("get_catalog", {}),
        ("find_item", {"item": "cake"}),
        ("ask_policy", {"question": "parking?"}),
        ("check_availability", {"item": "cake"}),
        ("check_service_area", {"postal_code": "80211"}),
        ("place_custom_order", {"item": "cake"}),
        ("book", {"item": "cut", "start": "2026-10-16T10:00"}),
        ("cancel_booking", {"reference": "c_x"}),
        ("request_quote", {"item": "leak"}),
        ("ask_owner", {"question": "hello?"}),
    ],
)
async def test_unknown_business_is_a_tool_error(client: Any, elicitation: ElicitationScript, tool: str, args: dict[str, Any]) -> None:
    result = await client.call_tool(tool, {"business_id": "no-such-shop", **args})
    assert result.is_error and "Unknown business" in result.content[0].text
    assert elicitation.seen == []  # resolvers fail before any question is asked


async def test_every_read_result_is_grounded(client: Any, deps: Any) -> None:
    calls: list[tuple[str, dict[str, Any]]] = [
        ("find_business", {"need": "vegan muffin"}),
        ("get_business_info", {"business_id": HOME, "topics": ["summary", "hours", "location", "contact"]}),
        ("get_catalog", {"business_id": SALON, "limit": 25}),
        ("get_catalog", {"business_id": HOME, "query": "drain"}),
        ("find_item", {"business_id": SALON, "item": "balayage", "aspects": ["price", "options", "duration", "description", "availability"]}),
        ("find_item", {"business_id": HOME, "item": "water heater", "aspects": ["price", "description"]}),
        ("find_item", {"business_id": BAKERY, "item": "challah", "aspects": ["availability", "allergens", "dietary"]}),
        ("ask_policy", {"business_id": HOME, "question": "do you charge a travel fee?"}),
        ("check_availability", {"business_id": SALON, "item": "balayage"}),
        ("check_service_area", {"business_id": HOME, "locality": "Lakewood"}),
    ]
    for name, args in calls:
        data = await call(client, name, args)
        await assert_grounded(deps, data)
        if data.get("handoff"):
            assert data["handoff"]["tool"] == "ask_owner"
