"""MRTR building blocks (`tools.confirm`): intake forms, read-backs and the confirmation question."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError
from tools_support import BAKERY, SALON, Stack

from mcp.server.elicitation import render_elicitation_schema
from mcp.server.mcpserver import AcceptedElicitation, CancelledElicitation, DeclinedElicitation, Elicit
from mcp.server.mcpserver.resolve import _accepted
from askonce_server.domain.planning import CommitPlan, IntakeFieldSpec, PlanProblem
from askonce_server.tools import confirm

pytestmark = pytest.mark.anyio

SPECS = (
    IntakeFieldSpec(key="customer_name", label="Name", type="text", required=True, max_length=80),
    IntakeFieldSpec(key="notes", label="Notes", type="long_text", max_length=300, spoken_prompt="Anything else?"),
    IntakeFieldSpec(key="servings", label="Number of servings", type="integer", required=True, min=1, max=200),
    IntakeFieldSpec(key="budget", label="Budget", type="money", min=0),
    IntakeFieldSpec(key="first_visit", label="First visit?", type="boolean"),
    IntakeFieldSpec(key="pickup_date", label="Pickup date", type="date", required=True),
    IntakeFieldSpec(key="arrival", label="Arrival time", type="time"),
    IntakeFieldSpec(key="email", label="Email", type="email", required=True, pii=True),
    IntakeFieldSpec(key="phone", label="Phone", type="phone", pii=True),
    IntakeFieldSpec(
        key="stylist", label="Stylist", type="choice", required=True, options=(("wren", "Wren Abernathy"), ("any", "No preference"))
    ),
    IntakeFieldSpec(key="extras", label="Extras", type="multi_choice", options=(("flowers", "Buttercream flowers"), ("fruit", "Fresh fruit"))),
)


# ---------------------------------------------------------------------------- intake_form_model


def test_form_model_renders_a_spec_valid_elicitation_schema() -> None:
    model = confirm.intake_form_model(SPECS, model_name="custom-cakes details")
    schema = render_elicitation_schema(model)
    props = schema["properties"]
    assert list(props) == [s.key for s in SPECS]  # wire names are the intake keys, in spec order
    assert sorted(schema["required"]) == ["customer_name", "email", "pickup_date", "servings", "stylist"]
    assert props["customer_name"] == {"title": "Name", "description": "Name", "type": "string", "maxLength": 80}
    assert props["notes"]["description"] == "Anything else?"
    assert props["servings"]["type"] == "integer" and props["servings"]["minimum"] == 1 and props["servings"]["maximum"] == 200
    assert props["budget"]["type"] == "number"
    assert props["first_visit"]["type"] == "boolean"
    assert props["pickup_date"]["format"] == "date"
    assert props["email"]["format"] == "email"
    assert props["stylist"]["oneOf"] == [{"const": "wren", "title": "Wren Abernathy"}, {"const": "any", "title": "No preference"}]
    assert props["extras"]["type"] == "array"
    assert props["extras"]["items"]["anyOf"][1] == {"const": "fruit", "title": "Fresh fruit"}
    assert schema["title"] == "CustomCakesDetails"


def test_form_model_is_deterministic_and_cached() -> None:
    first = confirm.intake_form_model(SPECS, model_name="x")
    second = confirm.intake_form_model(list(SPECS), model_name="x")
    assert first is second
    assert render_elicitation_schema(first) == render_elicitation_schema(second)


def test_form_model_validates_answers() -> None:
    model = confirm.intake_form_model(SPECS, model_name="x")
    data = model.model_validate(
        {
            "customer_name": "Sam",
            "servings": 30,
            "pickup_date": "2026-10-18",
            "email": "sam@example.com",
            "stylist": "wren",
            "extras": ["fruit"],
            "unexpected": "ignored",
        }
    )
    dumped = data.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert dumped == {
        "customer_name": "Sam",
        "servings": 30,
        "pickup_date": "2026-10-18",
        "email": "sam@example.com",
        "stylist": "wren",
        "extras": ["fruit"],
    }
    with pytest.raises(ValidationError):
        model.model_validate({**dumped, "stylist": "nobody"})
    with pytest.raises(ValidationError):
        model.model_validate({**dumped, "servings": 0})


def test_keys_that_shadow_model_attributes_are_safe() -> None:
    model = confirm.intake_form_model(
        (IntakeFieldSpec(key="schema", label="Schema", type="text", required=True),), model_name="odd"
    )
    assert model.model_validate({"schema": "ok"}).model_dump(by_alias=True) == {"schema": "ok"}


# ---------------------------------------------------------------------------- outcomes


def test_outcome_peels_the_plain_value_wrapper() -> None:
    assert confirm.outcome(_accepted(confirm.NO_INTAKE)) is confirm.NO_INTAKE
    assert isinstance(confirm.outcome(_accepted(confirm.SKIPPED_CONFIRMATION)), DeclinedElicitation)
    accepted = AcceptedElicitation(data=confirm.ConfirmCommit(confirm=True))
    assert confirm.outcome(accepted) is accepted


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (AcceptedElicitation(data=confirm.ConfirmCommit(confirm=True)), True),
        (AcceptedElicitation(data=confirm.ConfirmCommit(confirm=False)), False),
        (DeclinedElicitation(), False),
        (CancelledElicitation(), False),
        (_accepted(confirm.SKIPPED_CONFIRMATION), False),
    ],
)
def test_confirmed(answer: Any, expected: bool) -> None:
    assert confirm.confirmed(answer) is expected


def test_merge_intake_overlays_accepted_answers() -> None:
    model = confirm.intake_form_model(SPECS[:6], model_name="m")
    answer = AcceptedElicitation(
        data=model.model_validate({"customer_name": "Sam", "servings": 12, "pickup_date": date(2026, 10, 18)})
    )
    merged = confirm.merge_intake({"servings": 3, "message": "Hi"}, answer)
    assert merged == {"servings": 12, "message": "Hi", "customer_name": "Sam", "pickup_date": "2026-10-18"}


@pytest.mark.parametrize("answer", [_accepted(confirm.NO_INTAKE), confirm.NO_INTAKE, DeclinedElicitation(), CancelledElicitation()])
def test_merge_intake_keeps_details_without_answers(answer: Any) -> None:
    assert confirm.merge_intake({"servings": 3}, answer) == {"servings": 3}
    assert confirm.merge_intake(None, answer) == {}


# ---------------------------------------------------------------------------- questions on real plans


async def _cake_plan(stack: Stack, **overrides: Any) -> tuple[Any, CommitPlan]:
    profile = await stack.deps.profiles.get_live(BAKERY)
    args: dict[str, Any] = {
        "item": "custom cake",
        "choices": {"size": "ten inch round", "flavor": "vanilla bean", "filling": "lemon curd", "frosting": "vanilla buttercream"},
        "day": "2026-10-18",
        "intake": {"servings": 30, "customer_name": "Sam Lee", "customer_phone": "+12175550100"},
        "principal": "anonymous",
    }
    args.update(overrides)
    return profile, await stack.deps.orders.plan(profile, **args)


async def test_intake_question_asks_only_for_missing_fields(stack: Stack) -> None:
    profile, plan = await _cake_plan(stack, intake={"servings": 10})
    assert plan.missing and not plan.problems
    question = confirm.intake_question(profile, plan)
    assert isinstance(question, Elicit)
    props = render_elicitation_schema(question.schema)["properties"]
    assert sorted(props) == sorted(m.key for m in plan.missing) == ["customer_name", "customer_phone"]
    assert question.message == "To order your custom cake, I need a few details: name for the order and phone number."


async def test_single_missing_field_uses_its_spoken_prompt(stack: Stack) -> None:
    profile, plan = await _cake_plan(stack, intake={"customer_name": "Sam", "customer_phone": "+12175550100"})
    assert [m.key for m in plan.missing] == ["servings"]
    question = confirm.intake_question(profile, plan)
    assert isinstance(question, Elicit)
    assert question.message == "How many servings do you need?"


async def test_no_intake_when_nothing_is_missing_or_the_plan_has_problems(stack: Stack) -> None:
    profile, plan = await _cake_plan(stack)
    assert confirm.intake_question(profile, plan) is confirm.NO_INTAKE
    _, broken = await _cake_plan(stack, choices={"size": "ten inch round"}, intake={})
    assert broken.problems and broken.missing
    assert confirm.intake_question(profile, broken) is confirm.NO_INTAKE


async def test_read_back_for_an_order_that_needs_the_owner(stack: Stack) -> None:
    profile, plan = await _cake_plan(stack)
    message = confirm.read_back_message(profile, plan)
    assert message.startswith(
        "Custom cake, ten inch round, vanilla bean, lemon curd and vanilla buttercream, for Sunday, October 18, 30 servings, $120."
    )
    # Why it goes to Rosa, then the deposit rule that applies to a 30-serving order (VUX-8).
    reason = message.index("Orders of 25 servings or more need Rosa's approval")
    deposit = message.index("For 25 servings or more, a 50 percent deposit is due")
    assert reason < deposit < message.index("Shall I send it to Rosa?")
    assert "Orders under 25 servings" not in message  # rules for small orders do not apply
    assert len(message) <= confirm.READ_BACK_BUDGET
    assert message.endswith("Shall I send it to Rosa?")
    assert confirm.read_back_message(profile, plan) == message  # deterministic


async def test_read_back_without_approval_asks_to_place_the_order(stack: Stack) -> None:
    profile, plan = await _cake_plan(
        stack,
        choices={"size": "six inch round", "flavor": "vanilla bean", "filling": "lemon curd", "frosting": "vanilla buttercream"},
        intake={"servings": 10, "customer_name": "Sam", "customer_phone": "+12175550100", "message": "Happy birthday Sam"},
    )
    assert not plan.approval.required
    message = confirm.read_back_message(profile, plan)
    assert "message on the cake: Happy birthday Sam" in message and '"' not in message
    assert "Orders under 25 servings need no deposit" in message
    assert "For 25 servings or more" not in message and "For larger orders" not in message
    assert len(message) <= confirm.READ_BACK_BUDGET
    assert message.endswith("Shall I place the order?")


async def test_read_back_for_a_booking(stack: Stack) -> None:
    profile = await stack.deps.profiles.get_live(SALON)
    plan = await stack.deps.bookings.plan(
        profile,
        item="men's cut",
        start="2026-10-16T10:00",
        party_size=None,
        intake={"stylist": "wren", "customer_name": "Sam", "customer_phone": "+15125550100", "customer_email": "s@example.com"},
        principal="anonymous",
    )
    assert plan.ok, plan.problems
    message = confirm.read_back_message(profile, plan)
    assert message.startswith("Men's cut, stylist: Wren Abernathy, Friday, October 16 at 10 am, $42.")
    assert message.endswith("Shall I book it?")


async def test_disputed_policy_is_not_read_back(disputed_stack: Stack) -> None:
    profile = await disputed_stack.deps.profiles.get_live(SALON)
    plan = await disputed_stack.deps.bookings.plan(
        profile,
        item="men's cut",
        start="2026-10-16T10:00",
        party_size=None,
        intake={"stylist": "wren", "customer_name": "Sam", "customer_phone": "+15125550100", "customer_email": "s@example.com"},
        principal="anonymous",
    )
    assert "info/cancellation" in plan.say_before_confirm
    message = confirm.read_back_message(profile, plan)
    assert profile.knowledge_entry("info/cancellation")["answer_spoken"] not in message
    assert profile.knowledge_entry("info/email-confirmation")["answer_spoken"] in message


async def test_confirmation_question_is_skipped_for_plans_with_problems(stack: Stack) -> None:
    profile, plan = await _cake_plan(stack)
    question = confirm.confirmation_question(profile, plan)
    assert isinstance(question, Elicit) and question.schema is confirm.ConfirmCommit
    broken = plan.model_copy(update={"problems": (PlanProblem(code="closed", detail="closed"),)})
    assert confirm.confirmation_question(profile, broken) is confirm.SKIPPED_CONFIRMATION


def test_needs_owner() -> None:
    from askonce_server.domain.models import ApprovalCheck

    base: dict[str, Any] = dict(
        business_id="b", module_id="m", principal="p", profile_revision=1, idempotency_key="idem_x", operation="book"
    )
    assert not confirm.needs_owner(CommitPlan(kind="booking", **base))
    assert confirm.needs_owner(CommitPlan(kind="booking", approval=ApprovalCheck(required=True), **base))
    assert confirm.needs_owner(CommitPlan(kind="quote_request", **{**base, "operation": "quote_request"}))
    assert confirm.needs_owner(CommitPlan(kind="owner_question", **{**base, "operation": "ask_owner"}))
