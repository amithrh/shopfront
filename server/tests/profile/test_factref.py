"""`profile.factref`: parsing, overlap and resolution of fact refs (stable ids, L5 provenance)."""

from __future__ import annotations

from typing import Any

import pytest

from askonce_server import errors
from askonce_server.profile.factref import (
    Evidence,
    FactRef,
    FactRefError,
    ResolvedFact,
    entity_ref,
    explain_unresolvable,
    resolve,
)
from askonce_server.profile.model import Profile

DOCSTRING_EXAMPLES = [
    "business#name",
    "locations/main#hours",
    "menu#facility_may_contain",
    "menu/celebration-cake#price",
    "menu/celebration-cake/size/10in#price",
    "info/deposit#facts.deposit_percent",
    "custom-cakes#availability.capacity_per_day",
    "appointments/stylist-ana#attributes.level",
]


@pytest.fixture
def example(example_raw: dict[str, Any]) -> Profile:
    return Profile.unchecked(example_raw)


# --------------------------------------------------------------------------- parse / str


@pytest.mark.parametrize("text", DOCSTRING_EXAMPLES)
def test_round_trip(text: str) -> None:
    ref = FactRef.parse(text)
    assert str(ref) == text
    assert FactRef.parse(str(ref)) == ref


def test_parts() -> None:
    ref = FactRef.parse("menu/celebration-cake/size/10in#price.amount_cents")
    assert ref.scope == "menu"
    assert ref.path == ("celebration-cake", "size", "10in")
    assert ref.field == "price" and ref.subfields == ("amount_cents",)
    assert ref.entity == "menu/celebration-cake/size/10in"
    assert ref.field_path == ("price", "amount_cents")
    assert FactRef.parse("business#name").entity == "business"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "business",
        "#name",
        "Business#name",
        "menu/Cake#price",
        "/business/locations/0/hours",
        "menu/cake#Price",
        "menu/cake#price-x",
        "menu/cake#price.",
        "menu/cake#9price",
        "a/b/c/d/e#price",
        "menu/cake#a.b.c.d.e",
        "business#name\n",
        "menu//cake#price",
        "-menu/cake#price",
    ],
)
def test_malformed_refs_are_rejected(text: str) -> None:
    with pytest.raises(FactRefError):
        FactRef.parse(text)


def test_non_string_is_rejected() -> None:
    with pytest.raises(FactRefError):
        FactRef.parse(42)  # type: ignore[arg-type]


def test_direct_construction_is_validated() -> None:
    with pytest.raises(FactRefError):
        FactRef("Menu", (), "price")
    with pytest.raises(FactRefError):
        FactRef("menu", ["cake"], "price")  # type: ignore[arg-type]


def test_child_and_ordering() -> None:
    hours = FactRef.parse("locations/main#hours")
    assert str(hours.child("weekly")) == "locations/main#hours.weekly"
    assert hours.child() == hours
    with pytest.raises(FactRefError):
        hours.child("a", "b", "c", "d")
    refs = sorted([FactRef.parse("menu#b"), FactRef.parse("business#name"), FactRef.parse("menu#a")])
    assert [str(r) for r in refs] == ["business#name", "menu#a", "menu#b"]
    assert len({FactRef.parse("menu#a"), FactRef.parse("menu#a")}) == 1


# --------------------------------------------------------------------------- overlaps


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("locations/main#hours", "locations/main#hours", True),
        ("locations/main#hours", "locations/main#hours.weekly", True),
        ("locations/main#hours.weekly", "locations/main#hours.closures", False),
        ("locations/main#hours", "locations/main#address", False),
        ("locations/main#hours", "locations/other#hours", False),
        ("menu/cake#allergens", "menu/cake/sponge/gf#allergens", True),
        ("menu/cake#price", "menu/cake/size/10in#price", True),
        ("menu/cake#price", "menu/cake/size/10in#price.amount_cents", True),
        ("menu/cake#price", "menu/cake/size/10in#allergens", False),
        ("menu/cake/size/8in#price", "menu/cake/size/10in#price", False),
        ("menu/cake#price", "menu/cookie#price", False),
        ("menu#facility_may_contain", "menu/cake#allergens", False),
        ("menu/cake#price", "other/cake#price", False),
        ("business#name", "locations/main#name", False),
    ],
)
def test_overlaps(a: str, b: str, expected: bool) -> None:
    first, second = FactRef.parse(a), FactRef.parse(b)
    assert first.overlaps(second) is expected
    assert second.overlaps(first) is expected


# --------------------------------------------------------------------------- resolve (example profile)


def test_resolve_business_fact(example: Profile) -> None:
    fact = resolve(example, "business#name")
    assert fact.present and fact.value == "Thimbleberry Bakehouse"
    assert fact.owner_confirmed is False
    assert fact.evidence_url == "https://thimbleberry.example/"
    assert fact.entity is not None and fact.entity["spoken_name"] == "Thimbleberry Bakehouse"


def test_resolve_location_hours_uses_their_own_provenance(example: Profile) -> None:
    fact = resolve(example, "locations/main#hours")
    assert fact.present and fact.value["weekly"][0]["close"] == "18:00"
    assert fact.owner_confirmed is True  # hours.provenance, not the location's (unconfirmed) one
    assert "urn:askonce:owner-answer:q-hours" in {e.url for e in fact.evidence}
    weekly = resolve(example, "locations/main#hours.weekly")
    assert weekly.provenance == fact.provenance


def test_resolve_location_field_falls_back_to_location_provenance(example: Profile) -> None:
    fact = resolve(example, "locations/main#address.locality")
    assert fact.value == "Springfield" and fact.owner_confirmed is False
    assert fact.evidence_url == "https://thimbleberry.example/contact"
    area = resolve(example, "locations/springfield-delivery#area.localities")
    assert area.value == ["Springfield", "Chatham"]


def test_resolve_catalog_facts(example: Profile) -> None:
    price = resolve(example, "menu/celebration-cake#price")
    assert price.value == {"kind": "from", "amount_cents": 4800, "currency": "USD", "per": "each"}
    assert price.owner_confirmed is False and price.evidence_url == "https://thimbleberry.example/menu"
    choice = resolve(example, "menu/celebration-cake/size/10in#price")
    assert choice.value["amount_cents"] == 7200
    assert choice.provenance == price.provenance  # a choice is covered by its item
    group = resolve(example, "menu/celebration-cake/size#spoken_question")
    assert group.present and group.value.startswith("Which size")
    facility = resolve(example, "menu#facility_may_contain")
    assert facility.owner_confirmed is True and facility.value["allergens"] == ["tree_nuts", "peanuts"]


def test_resolve_choice_allergens_are_owner_confirmed(example: Profile) -> None:
    fact = resolve(example, "menu/celebration-cake/sponge/gf-vanilla#allergens")
    assert fact.present and fact.value["contains"] == ["milk", "eggs"]
    assert fact.owner_confirmed is True
    assert fact.evidence_url == "https://thimbleberry.example/menu"  # web first, owner URN second


def test_resolve_knowledge_and_module_facts(example: Profile) -> None:
    deposit = resolve(example, "info/deposit#facts.deposit_percent")
    assert deposit.value == 50 and deposit.owner_confirmed
    assert deposit.evidence_url == "urn:askonce:owner-answer:q-deposit"  # only owner evidence
    capacity = resolve(example, "custom-cakes#availability.capacity_per_day")
    assert capacity.value == 2 and capacity.owner_confirmed
    approver = resolve(example, "rosa#expected_response_minutes")
    assert approver.value == 120 and approver.owner_confirmed


def test_resolve_session_and_visit_window_provenance(example: Profile) -> None:
    session = resolve(example, "tastings/sat-10#start")
    assert session.value == "10:00"
    assert session.provenance == example.module("tastings")["slotting"]["provenance"]
    window = resolve(example, "catering/morning#start")
    assert window.value == "08:00"
    assert window.provenance == example.module("catering")["provenance"]


def test_category_without_module_provenance_has_none(example: Profile) -> None:
    fact = resolve(example, "menu/cakes#spoken_name")
    assert fact.present and fact.value == "cakes" and fact.provenance is None


@pytest.mark.parametrize(
    ("ref", "note"),
    [
        ("menu/cinnamon-roll#allergens", "has no 'allergens'"),
        ("menu/celebration-cake#price.max_cents", "has no 'price.max_cents'"),
        ("nope/thing#price", "unknown module"),
        ("menu/nope#price", "unknown entity"),
        ("menu/celebration-cake/size/12in#price", "unknown choice"),
        ("menu/celebration-cake/colour/red#price", "unknown option group"),
        ("locations/nowhere#hours", "unknown location"),
        ("business/x#name", "no nested entities"),
        ("info/deposit/x#facts", "no nested entities"),
        ("menu/celebration-cake#price.amount_cents.x", "has no"),
    ],
)
def test_absent_facts_are_not_errors(example: Profile, ref: str, note: str) -> None:
    fact = resolve(example, ref)
    assert isinstance(fact, ResolvedFact)
    assert fact.present is False and fact.value is None and fact.provenance is None
    assert note in fact.notes[0]


def test_disabled_module_facts_are_absent(example_raw: dict[str, Any]) -> None:
    example_raw["modules"]["tastings"]["enabled"] = False
    fact = resolve(Profile.unchecked(example_raw), "tastings/sat-10#start")
    assert not fact.present and "disabled" in fact.notes[0]


def test_extension_module_facts_are_flagged(example_raw: dict[str, Any]) -> None:
    example_raw["modules"]["waitlist"] = {
        "type": "x-waitlist",
        "title": "Waitlist",
        "provenance": example_raw["modules"]["rosa"]["provenance"],
    }
    fact = resolve(Profile.unchecked(example_raw), "waitlist#title")
    assert fact.present and fact.in_extension and fact.provenance is not None


def test_malformed_text_raises(example: Profile) -> None:
    with pytest.raises(FactRefError):
        resolve(example, "not a ref")


def test_evidence_url_preference() -> None:
    ref = FactRef.parse("business#name")
    web, owner = Evidence("https://a.example/", "x"), Evidence("urn:askonce:owner-answer:q1", "y")
    assert ResolvedFact(ref, True, evidence=(owner, web)).evidence_url == "https://a.example/"
    assert ResolvedFact(ref, True, evidence=(owner,)).evidence_url == "urn:askonce:owner-answer:q1"
    assert ResolvedFact(ref, True).evidence_url is None
    assert owner.is_owner and not web.is_owner


# --------------------------------------------------------------------------- entity_ref / L1 helper


def test_entity_ref(example: Profile) -> None:
    assert entity_ref(example, "menu", "celebration-cake") == "menu/celebration-cake"
    assert entity_ref(example, "menu", "celebration-cake", "size", "10in") == "menu/celebration-cake/size/10in"
    assert entity_ref(example, "info", "deposit") == "info/deposit"
    assert entity_ref(example, "rosa") == "rosa"


@pytest.mark.parametrize(
    "parts",
    [("menu", "nope"), ("nope",), ("menu", "celebration-cake", "size", "12in"), ("menu", "Bad Id"), ("business",)],
)
def test_entity_ref_not_found(example: Profile, parts: tuple[str, ...]) -> None:
    with pytest.raises(errors.NotFound):
        entity_ref(example, *parts)


@pytest.mark.parametrize(
    ("ref", "problem"),
    [
        ("menu/cinnamon-roll#allergens", None),  # absent but a real field: a `missing` question may target it
        ("custom-cakes#availability.capacity_per_day", None),
        ("info/deposit#facts.notice_minutes", None),
        ("menu/celebration-cake#attributes.serves", None),  # declared vocabulary, absent here
        ("locations/main#hourz", "has no field"),
        ("menu/celebration-cake#allergen", "has no field"),
        ("menu/celebration-cake#attributes.colour", "has no field"),
        ("custom-cakes#availability.capacity", "has no field"),
        ("rosa#items", "has no field"),
        ("locations/main#hours.weekly.days", "cannot address"),
        ("menu/nope#price", "unknown entity"),
    ],
)
def test_explain_unresolvable(example_raw: dict[str, Any], ref: str, problem: str | None) -> None:
    result = explain_unresolvable(example_raw, FactRef.parse(ref))
    if problem is None:
        assert result is None
    else:
        assert result is not None and problem in result


def test_explain_unresolvable_includes_disabled_modules(example_raw: dict[str, Any]) -> None:
    example_raw["modules"]["tastings"]["enabled"] = False
    assert explain_unresolvable(example_raw, FactRef.parse("tastings/sat-10#start")) is None


def test_explain_unresolvable_allows_anything_in_extension_modules(example_raw: dict[str, Any]) -> None:
    example_raw["modules"]["waitlist"] = {"type": "x-waitlist"}
    assert explain_unresolvable(example_raw, FactRef.parse("waitlist#anything.goes")) is None
