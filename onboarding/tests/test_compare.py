"""The onboarded demo profiles against the hand-curated gold profiles (`askonce-compare`)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from askonce_onboarding.compare import ANSWERS_DIR, OUT_DIR, answered, bool_claim, compare, gold_for, text_atoms

PIDS = ("thimbleberry", "kettlewick", "quillfern")


def onboarded(pid: str) -> dict:
    doc = json.loads((OUT_DIR / pid / "profile.json").read_text(encoding="utf-8"))
    return answered(doc, ANSWERS_DIR / f"{pid}.json")


@pytest.mark.parametrize("pid", PIDS)
def test_onboarded_profile_matches_gold_in_substance(pid):
    doc = onboarded(pid)
    report = compare(doc, gold_for(doc))
    assert report.mismatches == [], "\n".join(f"{c.dimension} {c.what}: {c.detail}" for c in report.mismatches)
    dims = report.summary()["by_dimension"]
    for dim in ("business", "hours", "items", "knowledge", "flows", "owner"):
        assert dims[dim]["checks"] > 0, dim  # every dimension was really compared


def test_a_wrong_price_or_a_split_variant_is_a_mismatch():
    doc = copy.deepcopy(onboarded("quillfern"))
    items = doc["modules"]["services"]["items"]
    next(i for i in items if i["id"] == "mens-cut")["price"]["amount_cents"] = 4500
    balayage = next(i for i in items if i["id"] == "balayage")
    long_hair = balayage["options"][0]["choices"].pop()
    items.append({"id": "balayage-long", "name": long_hair["name"], "spoken_name": long_hair["spoken_name"],
                  "price": long_hair["price"], "provenance": balayage["provenance"]})
    found = {(c.dimension, c.what) for c in compare(doc, gold_for(doc)).mismatches}
    assert ("items", "Men's cut: price") in found
    assert ("items", "item Balayage, past the shoulder blades") in found


def test_gold_compared_with_itself_is_clean():
    gold = json.loads((Path(__file__).resolve().parents[2] / "profiles" / "thimbleberry-bakehouse.json").read_text())
    report = compare(copy.deepcopy(gold), gold)
    assert [c for c in report.mismatches if c.dimension != "knowledge"] == []


def test_numbers_and_yes_no_facts_are_read_in_any_unit():
    assert ("minutes", 2880.0) in text_atoms("at least 48 hours before pickup")
    assert ("minutes", 4320.0) in text_atoms("Order 3 days ahead")
    assert ("cents", 5000.0) in text_atoms("a $50 deposit")
    assert bool_claim("accepts_checks", "We accept cards and cash. We don't accept checks.") is False
    assert bool_claim("after_hours_emergency", "No travel fee, and we take after-hours emergencies there.") is True
    assert bool_claim("nut_free_kitchen", "No. We use tree nuts every day.", questions=["Is your kitchen nut-free?"]) is False
