"""Rendering (`tools.render`): commitment results for every status, problem results and the wire form."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from tools_support import BAKERY, CANARY, HOME, SALON, Stack

from shopfront_server.domain.models import Commitment, OwnerDecision
from shopfront_server.domain.planning import CommitPlan
from shopfront_server.tools import render
from shopfront_server.tools.schemas import CommitmentResult, check_spoken

pytestmark = pytest.mark.anyio

DECIDED = datetime(2026, 10, 14, 17, 0, tzinfo=UTC)
CAKE_CHOICES = {"size": "ten inch round", "flavor": "vanilla bean", "filling": "lemon curd", "frosting": "vanilla buttercream"}
VALIDATOR = Draft202012Validator(CommitmentResult.model_json_schema(mode="serialization"))


async def cake(stack: Stack, *, servings: int = 30, size: str = "ten inch round") -> tuple[Any, Commitment]:
    profile = await stack.deps.profiles.get_live(BAKERY)
    plan = await stack.deps.orders.plan(
        profile,
        item="custom cake",
        choices={**CAKE_CHOICES, "size": size},
        day="2026-10-18",
        intake={"servings": servings, "customer_name": "Sam", "customer_phone": "+12175550100"},
        principal="anonymous",
    )
    outcome = await stack.deps.orders.commit(profile, plan, task_id=None)
    assert outcome.commitment is not None
    return profile, outcome.commitment


def decided(commitment: Commitment, status: str, decision: str, **fields: Any) -> Commitment:
    return commitment.model_copy(
        update={"status": status, "decision": OwnerDecision(decision=decision, decided_at=DECIDED, **fields)}  # type: ignore[arg-type]
    )


def check(result: CommitmentResult) -> dict[str, Any]:
    check_spoken(result.speak)
    wire = render.to_call_tool_result(result)
    assert json.loads(wire.content[0].text) == wire.structured_content
    errors = [e.message for e in VALIDATOR.iter_errors(wire.structured_content)]
    assert errors == []
    CommitmentResult.model_validate(wire.structured_content)
    return dict(wire.structured_content or {})


async def test_pending_and_approved_cake(stack: Stack) -> None:
    profile, pending = await cake(stack)
    data = check(render.commitment_result(profile, pending))
    assert data["status"] == "pending_owner" and data["reference"] == pending.commitment_id
    assert data["speak"] == (
        "I've sent your request to Rosa, who usually replies within 24 hours. Nothing is confirmed until Rosa approves it."
    )
    view = data["commitment"]
    assert view["item_name"] == "custom cake" and view["date"] == "2026-10-18"
    assert view["when_spoken"] == "Sunday, October 18"
    assert view["choices"] == {"size": "ten inch round", "flavor": "vanilla bean", "filling": "lemon curd", "frosting": "vanilla buttercream"}
    assert view["price"]["amount_cents"] == 12000

    approved = decided(pending, "approved", "approve", note="See you Sunday!")
    data = check(render.commitment_result(profile, approved))
    assert data["speak"] == "Good news: Rosa confirmed your custom cake for Sunday, October 18. The total is $120. Rosa says: See you Sunday!"
    owner_fact = next(f for f in data["facts"] if f["fact_ref"].endswith("#owner_note"))
    assert owner_fact["evidence_url"] == f"urn:shopfront:owner-answer:{pending.commitment_id}"
    assert owner_fact["owner_confirmed"] is True and owner_fact["value"] == "See you Sunday!"
    assert "menu/custom-cake/size/10in-round#price" in [f["fact_ref"] for f in data["facts"]]


@pytest.mark.parametrize(
    ("status", "decision", "fields", "expected"),
    [
        ("declined", "decline", {"note": "We're fully booked"}, "Sorry, Rosa can't take your custom cake for Sunday, October 18. Rosa says: We're fully booked."),
        ("counter_offer", "counter_offer", {"note": "an eight inch cake instead"}, "Rosa suggested a change: an eight inch cake instead. Want to go ahead that way?"),
        ("suggested_time", "suggest_time", {"values": {"suggested_date": "2026-10-20", "suggested_time": "11:00"}}, "Rosa can't do that time, but suggests Tuesday, October 20 at 11 am."),
        ("info_requested", "request_info", {"question": "Is the 18th a firm date?"}, "Rosa needs a bit more information: Is the 18th a firm date?"),
        ("expired", "decline", {}, "Rosa hasn't answered yet, so your request isn't confirmed."),
        ("cancelled", "decline", {}, "Your custom cake for Sunday, October 18 is cancelled."),
    ],
)
async def test_owner_outcomes(stack: Stack, status: str, decision: str, fields: dict[str, Any], expected: str) -> None:
    profile, pending = await cake(stack)
    data = check(render.commitment_result(profile, decided(pending, status, decision, **fields)))
    assert data["status"] == status and data["speak"] == expected


async def test_confirmed_order_and_its_card(stack: Stack) -> None:
    profile, confirmed = await cake(stack, servings=10, size="six inch round")
    data = check(render.commitment_result(profile, confirmed, replayed=True))
    assert data["status"] == "confirmed" and data["replayed"] is True
    assert data["speak"] == "Your custom cake for Sunday, October 18 is confirmed. The total is $56."
    lines = {line["label"]: line["value"] for line in data["card"]["lines"]}
    assert lines["Size"] == "six inch round" and lines["Reference"] == confirmed.commitment_id
    assert data["card"]["badges"] == ["Confirmed"]


async def test_quotes(stack: Stack) -> None:
    profile = await stack.deps.profiles.get_live(HOME)
    plan = await stack.deps.quotes.plan(
        profile,
        item="leak repair",
        intake={"description": "Drip", "zip": "80211", "time_window": "weekday-morning", "name": "Sam", "contact_by": "text", "phone": "+13035550100"},
        principal="anonymous",
    )
    assert plan.ok, plan.problems
    pending = (await stack.deps.quotes.commit(profile, plan, task_id=None)).commitment
    assert pending is not None
    data = check(render.commitment_result(profile, pending))
    assert data["speak"] == "I've sent your quote request to Jo, who usually replies within 24 hours. I'll let you know the quote."
    priced = decided(pending, "quoted", "quote", values={"reply_type": "fixed_price", "quoted_price": 24000})
    data = check(render.commitment_result(profile, priced))
    assert data["speak"] == "Kettlewick Home Repair sent a quote: $240."
    assert data["commitment"]["quote"]["amount_cents"] == 24000
    estimate = decided(pending, "quoted", "quote", values={"reply_type": "onsite_estimate"}, note="Jo can come Thursday")
    data = check(render.commitment_result(profile, estimate))
    assert data["speak"].startswith("Kettlewick Home Repair replied: Free on-site estimate")
    assert data["speak"].endswith("Jo says: Jo can come Thursday.")


async def test_owner_question_answer(stack: Stack) -> None:
    profile = await stack.deps.profiles.get_live(BAKERY)
    plan = await stack.deps.owner.plan_question(profile, question="Do you sell gift cards?", principal="anonymous")
    pending = (await stack.deps.owner.commit(profile, plan, task_id=None)).commitment
    assert pending is not None
    data = check(render.commitment_result(profile, pending))
    assert data["speak"].startswith("I've sent your question to Rosa") and data["card"]["kind"] == "task"
    answered = decided(pending, "answered", "answer", values={"answer": "Yes, in any amount."})
    data = check(render.commitment_result(profile, answered))
    assert data["speak"] == "Rosa says: Yes, in any amount."
    assert data["commitment"]["answer"] == "Yes, in any amount."
    assert any(f["fact_ref"].endswith("#answer") for f in data["facts"])


async def test_owner_free_text_is_sanitised_and_canaries_are_blocked(stack: Stack) -> None:
    profile, pending = await cake(stack)
    noisy = decided(pending, "approved", "approve", note="Details at https://evil.example\n**today**")
    data = check(render.commitment_result(profile, noisy))
    assert "http" not in data["speak"] and "*" not in data["speak"]
    hostile = decided(pending, "approved", "approve", note=f"Use code {CANARY}")
    wire = render.CommitmentRenderer().render(profile, hostile)
    assert CANARY not in json.dumps(wire.model_dump())
    assert wire.structured_content["status"] == "approved"
    assert not list(VALIDATOR.iter_errors(wire.structured_content))


async def test_problem_results(stack: Stack) -> None:
    bakery = await stack.deps.profiles.get_live(BAKERY)
    missing = await stack.deps.orders.plan(
        bakery, item="custom cake", choices={"size": "ten inch round"}, day="2026-10-18", intake={}, principal="anonymous"
    )
    data = check(render.problem_result(bakery, missing))
    assert data["status"] == "needs_info"
    assert data["missing_fields"][:3] == ["flavor", "filling", "frosting"]
    assert data["speak"].startswith("I still need the cake flavor, filling and frosting. Which flavor:")

    unknown = await stack.deps.orders.plan(bakery, item="wedding limousine", choices={}, day=None, intake={}, principal="anonymous")
    data = check(render.problem_result(bakery, unknown, asked="wedding limousine"))
    assert data["status"] == "not_offered"
    assert data["handoff"]["reason"] == "not_offered" and data["handoff"]["suggested_question"] == "Do you offer wedding limousine?"
    assert data["speak"].endswith("Want me to ask Rosa?")

    salon = await stack.deps.profiles.get_live(SALON)
    closed = await stack.deps.bookings.plan(
        salon, item="men's cut", start="2026-10-19T10:00", party_size=None, intake={}, principal="anonymous"
    )
    today = datetime(2026, 10, 14).date()
    data = check(render.problem_result(salon, closed, today=today))
    assert data["status"] == "unavailable" and 1 <= len(data["alternatives"]) <= render.MAX_ALTERNATIVES
    assert data["speak"].startswith("Quillfern Hair Studio is closed then. The nearest open times are Tuesday at 10 am")


async def test_not_confirmed_result(stack: Stack) -> None:
    profile, _ = await cake(stack)
    plan = CommitPlan(
        business_id=BAKERY, module_id="custom-cakes", kind="custom_order", operation="custom_order",
        principal="anonymous", profile_revision=profile.revision, idempotency_key="idem_x",
    )
    data = check(render.not_confirmed_result(profile, plan))
    assert data["status"] == "not_confirmed" and data["speak"] == "Okay, I haven't placed the order."
    assert data["reference"] is None and data["commitment"] is None


async def test_spoken_slots_groups_by_day_and_resource(stack: Stack) -> None:
    salon = await stack.deps.profiles.get_live(SALON)
    slots = await stack.deps.bookings.open_slots(salon, item="services/mens-cut", limit=3)
    names = render.resource_names(salon, "appointments")
    phrase = render.spoken_slots(salon, slots, today=datetime(2026, 10, 14).date(), resource_names=names)
    assert phrase == "today at 11 am, 11:15 am and 11:30 am with June"
    days = await stack.deps.orders.open_days(await stack.deps.profiles.get_live(BAKERY), item="menu/custom-cake", limit=3)
    assert render.spoken_slots(salon, days, today=datetime(2026, 10, 14).date()) == "Saturday, Sunday and Tuesday"


def test_waiting_message_has_no_pronouns(live_raw_profiles: dict[str, Any]) -> None:
    from tools_support import loaded

    profile = loaded(live_raw_profiles[SALON])
    assert render.waiting_message(profile, "appointments") == "Waiting for Inés, who usually replies within 2 hours."


# ---------------------------------------------------------------------------- round 2: request_info


ANSWERED = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)


async def test_round2_request_info_an_answered_question_that_expires_gets_the_owner_wording(stack: Stack) -> None:
    """R2: the customer answered Rosa's question, then the request expired before Rosa looked. It must
    not say the question went unanswered (that wording is kept for a question nobody answered)."""
    profile, pending = await cake(stack)
    asked = {"question": "Is the 18th a firm date?"}
    answered = decided(pending, "expired", "request_info", answered_at=ANSWERED, **asked)
    data = check(render.commitment_result(profile, answered))
    assert data["speak"] == "Rosa hasn't answered yet, so your request isn't confirmed."
    unanswered = decided(pending, "expired", "request_info", **asked)
    data = check(render.commitment_result(profile, unanswered))
    assert data["speak"] == "Your request lapsed because Rosa's question wasn't answered in time."


async def test_round2_request_info_a_request_back_with_the_owner_says_the_answer_was_passed_on(stack: Stack) -> None:
    """R1: once the customer's answer is folded in (pending_owner after request_info), the result says
    it is with the owner and never repeats the question."""
    profile, pending = await cake(stack)
    back = decided(pending, "pending_owner", "request_info", question="Is the 18th a firm date?", answered_at=ANSWERED)
    data = check(render.commitment_result(profile, back))
    assert data["speak"] == (
        "I've passed your answer on to Rosa, who usually replies within 24 hours. Nothing is confirmed until Rosa approves it."
    )
    assert "firm date" not in json.dumps(data)
    assert data["card"]["badges"] == ["Waiting for the owner"]

    home = await stack.deps.profiles.get_live(HOME)
    plan = await stack.deps.quotes.plan(
        home,
        item="leak repair",
        intake={"description": "Drip", "zip": "80211", "time_window": "weekday-morning", "name": "Sam", "contact_by": "text", "phone": "+13035550100"},
        principal="anonymous",
    )
    quote = (await stack.deps.quotes.commit(home, plan, task_id=None)).commitment
    assert quote is not None
    data = check(render.commitment_result(home, decided(quote, "pending_owner", "request_info", question="Which floor?", answered_at=ANSWERED)))
    assert data["speak"] == "I've passed your answer on to Jo, who usually replies within 24 hours. I'll let you know the quote."


async def test_round2_misc_low_booking_code_survives_a_long_owner_note(stack: Stack) -> None:
    """VUX-11: the code is the tail of the speech, so the 240-character cap drops anything but it."""
    from shopfront_server.ids import spoken_reference

    salon = await stack.deps.profiles.get_live(SALON)
    plan = await stack.deps.bookings.plan(
        salon,
        item="men's cut",
        start="2026-10-16T10:00",
        party_size=None,
        intake={"stylist": "wren", "customer_name": "Sam", "customer_phone": "+15125550100", "customer_email": "s@example.com"},
        principal="anonymous",
    )
    outcome = await stack.deps.bookings.commit(salon, plan, task_id=None)
    assert outcome.commitment is not None and outcome.commitment.status == "confirmed"
    booking = outcome.commitment
    code = spoken_reference(booking.commitment_id)
    assert code is not None
    spoken_code = f"Your booking code is {code[0]} {code[1]} {code[2]} {code[3]}, {code[4]} {code[5]} {code[6]} {code[7]}."

    data = check(render.commitment_result(salon, booking))
    assert data["speak"].endswith(spoken_code)
    approved = decided(booking, "approved", "approve", note="Please come ten minutes early so we can talk about the look you want. " * 2)
    data = check(render.commitment_result(salon, approved))
    assert data["speak"].endswith(spoken_code) and len(data["speak"]) <= 240

    hidden = check(render.commitment_result(salon, approved, with_code=False))
    assert "booking code" not in hidden["speak"].lower() and code not in str({k: v for k, v in hidden.items() if k != "reference"})
    assert "Booking code" not in {line["label"] for line in hidden["card"]["lines"]}
    for status in ("cancelled", "pending_owner", "declined"):
        other = check(render.commitment_result(salon, booking.model_copy(update={"status": status})))
        assert "booking code" not in other["speak"].lower(), status
