"""Approval rules: the gold bakery's servings/size rules, the salon's colour rule, `always`, and every op."""

from __future__ import annotations

from typing import Any

import pytest

from askonce_server.domain.approval_rules import evaluate_approval, rule_matches
from askonce_server.profile.model import Profile

CAKE = "menu/custom-cake"


def facts(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"item": CAKE, "choices": [f"{CAKE}/size/6in-round"]}
    base.update(kwargs)
    return base


@pytest.mark.parametrize(("servings", "required"), [(12, False), (24, False), (25, True), (30, True)])
def test_bakery_servings_rule(bakery: Profile, servings: int, required: bool) -> None:
    check = evaluate_approval("custom-cakes", bakery.module("custom-cakes"), facts(servings=servings), owner_module_id="rosa")
    assert check.required is required
    assert check.owner_module_id == "rosa"
    assert check.fact_refs == ("custom-cakes#approval.rules",)
    if required:
        assert check.matched_rule_keys == ("servings",)
        assert "25 servings or more" in check.reasons_spoken[0]


@pytest.mark.parametrize(
    ("size", "required"),
    [("6in-round", False), ("8in-round", False), ("10in-round", True), ("half-sheet", True), ("three-tier", True)],
)
def test_bakery_size_rule_matches_full_choice_refs(bakery: Profile, size: str, required: bool) -> None:
    check = evaluate_approval(
        "custom-cakes",
        bakery.module("custom-cakes"),
        facts(servings=10, choices=[f"{CAKE}/size/{size}", f"{CAKE}/flavor/vanilla-bean"]),
        owner_module_id="rosa",
    )
    assert check.required is required
    assert check.matched_rule_keys == (("choices",) if required else ())


def test_both_bakery_rules_give_both_reasons(bakery: Profile) -> None:
    check = evaluate_approval(
        "custom-cakes",
        bakery.module("custom-cakes"),
        facts(servings=30, choices=[f"{CAKE}/size/10in-round"]),
        owner_module_id="rosa",
    )
    assert check.matched_rule_keys == ("servings", "choices")
    assert len(check.reasons_spoken) == 2


def test_unknown_values_never_match(bakery: Profile) -> None:
    check = evaluate_approval("custom-cakes", bakery.module("custom-cakes"), {"choices": []}, owner_module_id="rosa")
    assert check.required is False


def test_salon_colour_services_need_ines(salon: Profile) -> None:
    module = salon.module("appointments")
    colour = evaluate_approval("appointments", module, {"item": "services/balayage"}, owner_module_id="owner")
    cut = evaluate_approval("appointments", module, {"item": "services/womens-cut"}, owner_module_id="owner")
    assert colour.required and colour.owner_module_id == "owner" and "deposit" in colour.reasons_spoken[0]
    assert not cut.required


def test_quote_requests_always_need_the_owner(home: Profile) -> None:
    check = evaluate_approval("quotes", home.module("quotes"), {}, owner_module_id=None)
    assert check.required and check.owner_module_id == "owner"
    assert check.fact_refs == ("quotes#approval.mode",)
    assert check.reasons_spoken == ()


def test_module_without_approval_never_needs_it(bakery: Profile) -> None:
    assert evaluate_approval("menu", bakery.module("menu"), {"item": CAKE}, owner_module_id="rosa").required is False


@pytest.mark.parametrize(
    ("rule", "values", "expected"),
    [
        ({"when": "n", "op": ">", "value": 24}, {"n": 25}, True),
        ({"when": "n", "op": ">", "value": 24}, {"n": 24}, False),
        ({"when": "n", "op": ">=", "value": 24}, {"n": "24"}, True),
        ({"when": "n", "op": "<", "value": 2}, {"n": 1.5}, True),
        ({"when": "n", "op": "<=", "value": 2}, {"n": 3}, False),
        ({"when": "n", "op": "==", "value": 3}, {"n": 3.0}, True),
        ({"when": "s", "op": "==", "value": "Vegan"}, {"s": "vegan"}, True),
        ({"when": "b", "op": "==", "value": "true"}, {"b": True}, True),
        ({"when": "b", "op": "==", "value": "true"}, {"b": False}, False),
        ({"when": "s", "op": "in", "value": ["a", "b"]}, {"s": "B"}, True),
        ({"when": "s", "op": "in", "value": ["a", "b"]}, {"s": "c"}, False),
        ({"when": "m", "op": "in", "value": ["gluten_free"]}, {"m": ["vegan", "gluten_free"]}, True),
        ({"when": "m", "op": "in", "value": ["gluten_free"]}, {"m": ["vegan"]}, False),
        ({"when": "d", "op": "<", "value": "2026-11-01"}, {"d": "2026-10-18"}, True),
        ({"when": "t", "op": "present"}, {"t": "no nuts please"}, True),
        ({"when": "t", "op": "present"}, {"t": "   "}, False),
        ({"when": "t", "op": "present"}, {}, False),
        ({"when": "n", "op": ">", "value": 5}, {}, False),
        ({"when": "n", "op": ">", "value": 5}, {"n": "lots"}, False),
        ({"when": "n", "op": "~", "value": 5}, {"n": 6}, False),
        ({"when": "lead_minutes", "op": "<", "value": 1440}, {"lead_minutes": 600}, True),
    ],
)
def test_rule_ops(rule: dict[str, Any], values: dict[str, Any], expected: bool) -> None:
    assert rule_matches(rule, values) is expected
