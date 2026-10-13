"""Policy matching (`tools.knowledge`): stemming, coverage, thresholds, and the gold FAQ variants."""

from __future__ import annotations

from typing import Any

import pytest
from tools_support import loaded

from shopfront_server.tools import knowledge


@pytest.mark.parametrize(
    ("word", "stem"),
    [("cancellation", "cancel"), ("cancelling", "cancel"), ("cancel", "cancel"), ("deposits", "deposit"), ("parking", "park"), ("bus", "bus")],
)
def test_stem(word: str, stem: str) -> None:
    assert knowledge.stem(word) == stem


def test_tokens_drop_stop_words_and_fold_forms() -> None:
    assert knowledge.tokens("Can I get my deposit back?") == frozenset({"deposit", "back"})
    assert knowledge.tokens("Hi, what's your cancellation policy?") == frozenset({"cancel", "policy"})
    assert knowledge.tokens("the of and") == frozenset()


def test_dice_and_coverage() -> None:
    a, b = frozenset({"park", "airport", "validate"}), frozenset({"park"})
    assert knowledge.dice(a, b) == 0.5 and knowledge.coverage(a, b) == pytest.approx(1 / 3)
    assert knowledge.dice(frozenset(), b) == 0.0 and knowledge.coverage(frozenset(), b) == 0.0


def test_partial_overlap_is_not_enough() -> None:
    entry = {"id": "parking", "topic": "parking", "question_variants": ["is there parking", "where do I park"]}
    assert knowledge.score_entry(knowledge.tokens("Do you validate parking at the airport?"), entry, None) == 0.0
    assert knowledge.score_entry(knowledge.tokens("is there parking?"), entry, None) >= knowledge.MATCH_THRESHOLD
    # An explicit topic from the host lifts the coverage rule and boosts the entry.
    with_topic = knowledge.score_entry(knowledge.tokens("Do you validate parking at the airport?"), entry, "parking")
    assert with_topic >= knowledge.MATCH_THRESHOLD


def test_verbatim_sanitises_only_invalid_text() -> None:
    assert knowledge.verbatim("We take cards.") == "We take cards."
    assert knowledge.verbatim("See https://x.example") == "See"


def test_every_gold_question_variant_finds_its_entry(live_raw_profiles: dict[str, dict[str, Any]]) -> None:
    """Regression guard for the matcher: each published variant answers with its own entry (or one that
    lists the identical variant)."""
    misses = []
    for doc in live_raw_profiles.values():
        profile = loaded(doc)
        entries = profile.knowledge_entries()
        for module_id, entry in entries:
            for variant in entry["question_variants"]:
                best = knowledge.best_entry(profile, variant, None)
                owners = {f"{m}/{e['id']}" for m, e in entries if variant in e["question_variants"]}
                if best is None or f"{best[0]}/{best[1]['id']}" not in owners:
                    misses.append((profile.profile_id, f"{module_id}/{entry['id']}", variant, best and best[1]["id"]))
    assert misses == []
