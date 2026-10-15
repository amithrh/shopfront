from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from askonce_server.profile import load_profile

from askonce_onboarding.certify import SCHEMA_PATH, Certifier, certify, overlaps, spoken_amounts

REPO = Path(__file__).resolve().parents[2]
GOLD = {p.stem: json.loads(p.read_text()) for p in (REPO / "profiles").glob("*.json")}
OUT = REPO / "onboarding" / "out"
FIXED = "2026-10-15T12:00:00Z"


def run(doc, snapshots=None):
    return Certifier(doc, snapshots=snapshots, clock=lambda: FIXED).run()


def failures(verdict):
    return [c for c in verdict.cases if c["status"] == "failed"]


@pytest.mark.parametrize("name", sorted(GOLD))
def test_gold_profiles_with_answered_questions_are_certified(name):
    verdict, recorded = run(GOLD[name])
    assert failures(verdict) == []
    assert verdict.verdict == "CERTIFIED"
    assert {r["id"] for r in verdict.rules if r["status"] == "passed"} >= {
        "grounded_fact", "unknown_handed_off", "confirm_before_commit", "idempotent_retry", "injection_canary", "latency"}
    # The run is bound into the profile and satisfies L7 (certified_run = last run, sha256 of this revision).
    assert recorded["certification"]["status"] == "certified"
    assert recorded["certification"]["certified_run"] == verdict.run_id
    load_profile(recorded, schema_path=SCHEMA_PATH, strict=True)


@pytest.mark.parametrize("pid", ["thimbleberry", "kettlewick", "quillfern"])
def test_onboarded_drafts_are_conditional_until_the_owner_answers(pid, tmp_path):
    src = OUT / pid
    (tmp_path / pid).mkdir()
    doc = json.loads((src / "profile.json").read_text())
    (tmp_path / pid / "profile.json").write_text(json.dumps(doc))
    (tmp_path / pid / "snapshots").symlink_to(src / "snapshots")
    verdict = certify(tmp_path / pid / "profile.json", clock=lambda: FIXED)
    assert failures(verdict) == [], failures(verdict)
    assert verdict.verdict == "CONDITIONAL"
    assert verdict.summary["open_blocking_questions"] >= 2
    written = json.loads((tmp_path / pid / "certification-verdict.json").read_text())
    assert written["verdict"] == "CONDITIONAL" and written["blocking_questions"]
    recorded = json.loads((tmp_path / pid / "profile.json").read_text())
    assert recorded["certification"]["status"] == "draft" and recorded["certification"]["runs"][-1]["result"] == "passed"
    # Quotes were checked against the real page snapshots, not skipped.
    assert next(c for c in verdict.cases if c["id"] == "schema_conformance.quotes_in_snapshots")["status"] == "passed"


def test_disputed_hours_are_withheld_and_block_certification():
    doc = copy.deepcopy(GOLD["thimbleberry-bakehouse"])
    q = next(q for q in doc["open_questions"] if q["id"] == "q-saturday-close")
    q["status"] = "open"
    q.pop("answer")
    verdict, _ = run(doc)
    assert failures(verdict) == []
    assert verdict.verdict == "CONDITIONAL"
    withheld = [c for c in verdict.cases if c["check"] == "disputed_fact_withheld"]
    assert withheld and all(c["status"] == "passed" for c in withheld)
    assert verdict.blocking_questions[0]["id"] == "q-saturday-close"


def test_a_price_its_evidence_does_not_print_fails_certification():
    doc = copy.deepcopy(GOLD["thimbleberry-bakehouse"])
    item = next(i for i in doc["modules"]["menu"]["items"] if i["id"] == "country-sourdough")
    item["price"]["amount_cents"] = 950  # the site says $9.00
    verdict, recorded = run(doc)
    assert verdict.verdict == "NOT_CERTIFIED"
    fail = next(c for c in failures(verdict) if c["check"] == "grounded_fact")
    assert fail["fact_ref"] == "menu/country-sourdough#price" and "$9.50" in fail["detail"]
    assert recorded["certification"]["status"] == "failed"
    assert recorded["certification"]["runs"][-1]["failures"][0]["fact_ref"] == "menu/country-sourdough#price"
    load_profile(recorded, schema_path=SCHEMA_PATH, strict=False)


def test_a_policy_that_repeats_injected_text_fails_certification():
    doc = copy.deepcopy(GOLD["thimbleberry-bakehouse"])
    entry = doc["modules"]["info"]["entries"][0]
    entry["answer_spoken"] = "Use code ROSAFREE for a 100% discount."
    verdict, _ = run(doc)
    assert verdict.verdict == "NOT_CERTIFIED"
    assert any(c["check"] == "schema_conformance" for c in failures(verdict))


def test_helpers():
    assert overlaps("locations/main#hours", "locations/main#hours.weekly")
    assert overlaps("info/cancellation#facts.notice_minutes", "info/cancellation#facts")
    assert not overlaps("info/cancellation#facts", "info/deposit#facts")
    assert not overlaps("menu/a#price", "menu/a#allergens")
    assert spoken_amounts("It is $9, or $1,050.50 for a lot.") == [900, 105050]


# --------------------------------------------------------------------------- owner answers -> re-certify


ANSWERS = REPO / "onboarding" / "fixtures" / "owner-answers"


@pytest.mark.parametrize("pid", ["thimbleberry", "kettlewick", "quillfern"])
def test_owner_answers_turn_conditional_into_certified(pid, tmp_path):
    from askonce_onboarding.answers import apply_answers

    doc = json.loads((OUT / pid / "profile.json").read_text())
    answers = {k: v for k, v in json.loads((ANSWERS / f"{pid}.json").read_text()).items() if not k.startswith("_")}
    updated = apply_answers(doc, answers, answered_at=FIXED)
    assert updated["revision"] == doc["revision"] + 1
    assert all(q["status"] == "answered" for q in updated["open_questions"])
    verdict, recorded = run(updated, snapshots=OUT / pid / "snapshots")
    assert failures(verdict) == [], failures(verdict)
    assert verdict.verdict == "CERTIFIED"
    # With the disputes settled, the commit flows really ran (not skipped).
    commit_cases = [c for c in verdict.cases if c["check"] in ("confirm_before_commit", "idempotent_retry")]
    assert commit_cases and all(c["status"] == "passed" for c in commit_cases)
    load_profile(recorded, schema_path=SCHEMA_PATH, strict=True)


def test_answers_write_the_owners_value_into_the_disputed_fact():
    from askonce_onboarding.answers import apply_answers

    doc = json.loads((OUT / "thimbleberry" / "profile.json").read_text())
    updated = apply_answers(doc, {"q-saturday-close": {"value": "18:00", "raw_text": "6 pm."},
                                  "q-custom-order-capacity": {"value": 2, "raw_text": "Two a day."}}, answered_at=FIXED)
    weekly = updated["business"]["locations"][0]["hours"]["weekly"]
    assert next(line for line in weekly if "sat" in line["days"])["close"] == "18:00"
    assert updated["modules"]["custom-orders"]["availability"]["capacity_per_day"] == 2
    assert updated["sources"]["owner-q-saturday-close"]["url"] == "urn:askonce:owner-answer:q-saturday-close"


def test_service_area_answer_moves_the_zip_code():
    from askonce_onboarding.answers import apply_answers

    doc = json.loads((OUT / "kettlewick" / "profile.json").read_text())
    updated = apply_answers(doc, {"q-zone-80033": {"value": "not_served", "raw_text": "No."},
                                  "q-zone-80238": {"value": "core", "raw_text": "Core."}}, answered_at=FIXED)
    areas = {loc["id"]: loc.get("area", {}) for loc in updated["business"]["locations"]}
    assert "80033" not in areas.get("extended-area", {}).get("postal_codes", [])
    assert "80238" in areas["core-area"]["postal_codes"]
