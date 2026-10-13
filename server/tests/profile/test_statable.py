"""`profile.statable`: the statable rule (L6) at answer time."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from shopfront_server.profile.factref import FactRef, resolve
from shopfront_server.profile.loader import LoadedProfile
from shopfront_server.profile.model import Profile
from shopfront_server.profile.statable import disputed_by, is_safety_field, statability


def _reopen(doc: dict[str, Any], question_id: str) -> dict[str, Any]:
    """Turn an answered question back into an open, non-blocking one (schema-valid on a live profile)."""
    for question in doc["open_questions"]:
        if question["id"] == question_id:
            question["status"] = "open"
            question["blocking"] = False
            question.pop("answer", None)
            return doc
    raise AssertionError(question_id)


def _state(profile: Profile, ref: str) -> tuple[bool, str, str | None]:
    result = statability(profile, resolve(profile, ref))
    return result.statable, result.reason, result.question_id


@pytest.fixture
def example(example_raw: dict[str, Any], load_doc: Callable[..., LoadedProfile]) -> Profile:
    return load_doc(example_raw).profile


def test_present_provenanced_fact_is_statable(example: Profile) -> None:
    assert _state(example, "menu/celebration-cake#price") == (True, "ok", None)
    assert _state(example, "menu/celebration-cake/sponge/gf-vanilla#allergens") == (True, "ok", None)
    assert _state(example, "locations/main#hours.weekly") == (True, "ok", None)  # q-hours is answered


def test_absent_fact_is_unknown(example: Profile) -> None:
    assert _state(example, "menu/cinnamon-roll#allergens") == (False, "absent", None)
    assert _state(example, "nope/x#price") == (False, "absent", None)


def test_open_contradiction_withholds_the_disputed_fact(
    example_raw: dict[str, Any], load_doc: Callable[..., LoadedProfile]
) -> None:
    profile = load_doc(_reopen(example_raw, "q-hours")).profile
    assert _state(profile, "locations/main#hours") == (False, "disputed", "q-hours")
    assert _state(profile, "locations/main#hours.weekly") == (False, "disputed", "q-hours")
    assert _state(profile, "locations/main#hours.closures") == (False, "disputed", "q-hours")
    assert _state(profile, "locations/main#address") == (True, "ok", None)
    assert _state(profile, "locations/springfield-delivery#area") == (True, "ok", None)
    assert disputed_by(profile, FactRef.parse("locations/main#hours.weekly")) == "q-hours"


def test_open_confirm_withholds_nested_choice_facts(
    example_raw: dict[str, Any], load_doc: Callable[..., LoadedProfile]
) -> None:
    question = next(q for q in example_raw["open_questions"] if q["id"] == "q-allergens")
    question["targets"] = ["menu/celebration-cake#allergens"]
    profile = load_doc(_reopen(example_raw, "q-allergens")).profile
    assert _state(profile, "menu/celebration-cake#allergens") == (False, "disputed", "q-allergens")
    assert _state(profile, "menu/celebration-cake/sponge/gf-vanilla#allergens") == (False, "disputed", "q-allergens")
    assert _state(profile, "menu/celebration-cake/sponge/gf-vanilla#dietary") == (True, "ok", None)
    assert _state(profile, "menu/sourdough#allergens") == (True, "ok", None)


def test_missing_question_never_withholds_a_present_fact(
    example_raw: dict[str, Any], load_doc: Callable[..., LoadedProfile]
) -> None:
    profile = load_doc(_reopen(example_raw, "q-capacity")).profile
    assert profile.open_questions()[0]["kind"] == "missing"
    assert _state(profile, "custom-cakes#availability.capacity_per_day") == (True, "ok", None)
    assert disputed_by(profile, FactRef.parse("custom-cakes#availability.capacity_per_day")) is None


def test_answered_and_dismissed_questions_do_not_withhold(example: Profile) -> None:
    assert disputed_by(example, FactRef.parse("locations/main#hours")) is None


def test_unparseable_target_never_crashes_an_answer(example_raw: dict[str, Any]) -> None:
    question = _reopen(example_raw, "q-hours")["open_questions"][0]
    question["targets"] = ["/business/locations/0/hours", "locations/main#hours"]
    profile = Profile.unchecked(example_raw)
    assert _state(profile, "locations/main#hours") == (False, "disputed", "q-hours")


def test_no_provenance(example: Profile) -> None:
    assert _state(example, "menu/cakes#spoken_name") == (False, "no_provenance", None)


def test_extension_facts_are_never_statable(example_raw: dict[str, Any]) -> None:
    example_raw["modules"]["waitlist"] = {
        "type": "x-waitlist",
        "title": "Waitlist",
        "provenance": copy.deepcopy(example_raw["modules"]["rosa"]["provenance"]),
    }
    assert _state(Profile.unchecked(example_raw), "waitlist#title") == (False, "extension", None)


def test_unconfirmed_safety_fact_is_withheld(example_raw: dict[str, Any]) -> None:
    """The schema forbids this document; `Profile.unchecked` proves the defence in depth."""
    allergens = example_raw["modules"]["menu"]["items"][1]["allergens"]
    allergens["provenance"] = {"owner_confirmed": False, "evidence": [allergens["provenance"]["evidence"][0]]}
    profile = Profile.unchecked(example_raw)
    assert _state(profile, "menu/sourdough#allergens") == (False, "unconfirmed_safety", None)
    assert _state(profile, "menu/sourdough#allergens.contains") == (False, "unconfirmed_safety", None)
    assert _state(profile, "menu/sourdough#price") == (True, "ok", None)


def test_safety_vocabulary_attributes_need_owner_confirmation(example_raw: dict[str, Any]) -> None:
    example_raw["vocabulary"]["attributes"]["nut_free_kitchen"] = {
        "label": "Nut-free kitchen",
        "type": "boolean",
        "claim_class": "safety",
        "absent_means": "unknown",
    }
    parking = example_raw["modules"]["info"]["entries"][2]
    parking["facts"] = {"nut_free_kitchen": False}  # covered by an unconfirmed web provenance
    deposit = example_raw["modules"]["info"]["entries"][0]
    deposit["facts"]["nut_free_kitchen"] = False  # covered by an owner-confirmed provenance
    profile = Profile.unchecked(example_raw)
    assert _state(profile, "info/parking#facts.nut_free_kitchen") == (False, "unconfirmed_safety", None)
    assert _state(profile, "info/parking#facts") == (False, "unconfirmed_safety", None)
    assert _state(profile, "info/parking#answer_spoken") == (True, "ok", None)
    assert _state(profile, "info/deposit#facts.nut_free_kitchen") == (True, "ok", None)


def test_is_safety_field() -> None:
    assert is_safety_field(FactRef.parse("menu/x#allergens"))
    assert is_safety_field(FactRef.parse("menu/x/y/z#dietary.tags"))
    assert is_safety_field(FactRef.parse("menu#facility_may_contain"))
    assert not is_safety_field(FactRef.parse("menu/x#price"))


@pytest.mark.parametrize("name", ["thimbleberry-bakehouse", "quillfern-salon", "kettlewick-home"])
def test_every_gold_safety_fact_is_statable(
    name: str, gold_raw: Callable[[str], dict[str, Any]], load_doc: Callable[..., LoadedProfile]
) -> None:
    profile = load_doc(gold_raw(name)).profile
    refs: list[str] = []
    for module_id, item in profile.catalog_items():
        for field in ("allergens", "dietary"):
            if field in item:
                refs.append(f"{module_id}/{item['id']}#{field}")
        for group in item.get("options") or ():
            for choice in group["choices"]:
                refs += [
                    f"{module_id}/{item['id']}/{group['id']}/{choice['id']}#{f}"
                    for f in ("allergens", "dietary")
                    if f in choice
                ]
    for module_id, _ in profile.modules_of_type("catalog"):
        if profile.facility_may_contain(module_id):
            refs.append(f"{module_id}#facility_may_contain")
    for ref in refs:
        assert _state(profile, ref) == (True, "ok", None), ref
