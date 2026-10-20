"""Policy matching (`tools.knowledge`): stemming, coverage, thresholds, and the gold FAQ variants."""

from __future__ import annotations

from typing import Any

import pytest
from tools_support import loaded

from askonce_server.tools import knowledge


@pytest.mark.parametrize(
    ("word", "stem"),
    [("cancellation", "cancel"), ("cancelling", "cancel"), ("cancel", "cancel"), ("deposits", "deposit"), ("parking", "park"), ("bus", "bus")],
)
def test_stem(word: str, stem: str) -> None:
    assert knowledge.stem(word) == stem


def test_tokens_drop_stop_words_and_fold_forms() -> None:
    assert knowledge.tokens("Can I get my deposit back?") == frozenset({"deposit", "back"})
    assert knowledge.tokens("Hi, what's your cancellation policy?") == frozenset({"cancel"})
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


def test_place_names_ages_and_synonyms_fold() -> None:
    units = (("central", "park"),)
    assert knowledge.tokens("Is Central Park in your service area?", units) == frozenset({"central park", "service", "area"})
    assert knowledge.tokens("is there parking", units) == frozenset({"park"})  # never the neighbourhood
    assert knowledge.tokens("can my 16 year old let you in") == frozenset({"teen", "let"})
    assert knowledge.tokens("Can my teenager let you in?") == frozenset({"teen", "let"})
    assert knowledge.tokens("do you accept checks") == frozenset({"take", "check"})


def test_variants_and_answer_together_can_explain_a_question() -> None:
    entry = {
        "id": "turnaround",
        "topic": "response_time",
        "question_variants": ["How fast will Jo get back to me?", "How long does a quote take?"],
        "answer_spoken": "Jo replies to every quote request within 24 hours.",
    }
    question = knowledge.tokens("how fast will i get a quote")
    assert knowledge.unexplained(question, entry) == 0
    assert knowledge.score_entry(question, entry, None) >= knowledge.MATCH_THRESHOLD
    # A word neither the variants nor the answer contain keeps the partial rule: no answer.
    assert knowledge.score_entry(knowledge.tokens("how fast will my pizza arrive"), entry, None) == 0.0


def test_stems_synonyms_and_idioms_fold() -> None:
    assert knowledge.stem("charged") == knowledge.stem("charge") == knowledge.stem("charging") == "charge"
    assert knowledge.stem("accessibility") == knowledge.stem("accessible") == "access"
    assert knowledge.tokens("what forms of payment do you accept") == frozenset({"payment", "take"})
    assert knowledge.tokens("do I need to put down a deposit") == knowledge.tokens("do I need to pay a deposit")
    assert knowledge.tokens("what if I hate my haircut") == knowledge.tokens("what if I don't like my haircut")
    assert knowledge.tokens("can I come in without an appointment") == frozenset({"come", "walk"})
    assert knowledge.tokens("hablan español") == knowledge.tokens("hablan espanol")


def test_words_for_the_business_count_only_alone() -> None:
    assert knowledge.tokens("who owns the bakery") == frozenset({"own"})
    assert knowledge.tokens("tell me about the bakery") == frozenset({"bakery"})


def test_a_lone_rare_word_needs_the_rest_of_the_question_explained() -> None:
    entry = {
        "id": "same-day",
        "topic": "lead_time",
        "question_variants": ["can I get a cake for tomorrow"],
        "answer_spoken": "Not a custom cake, but most days we have case cakes.",
    }
    frequency = {"cake": 3, "tomorrow": 1}
    assert not knowledge.decisive(knowledge.tokens("what's the weather like tomorrow"), entry, frequency)
    assert knowledge.decisive(knowledge.tokens("a case cake for tomorrow"), entry, frequency)


@pytest.mark.parametrize(
    "question",
    ["Do you deliver to Beaverton?", "Can you deliver to Lake Oswego?", "Do you deliver in 97225?", "Do you deliver?"],
)
def test_a_place_the_business_never_mentions_does_not_hide_the_policy(live_raw_profiles: dict[str, dict[str, Any]], question: str) -> None:
    profile = loaded(live_raw_profiles["thimbleberry-bakehouse"])
    match = knowledge.best_entry(profile, question, None)
    assert match is not None and match[1]["id"] == "delivery", question


def test_named_places_are_capitalised_names_or_postal_codes_after_a_place_word() -> None:
    assert knowledge.named_places("Do you deliver to Beaverton?") == {"beaverton"}
    assert knowledge.named_places("Do you come out to 80211?") == {"80211"}
    assert knowledge.named_places("Can I go to cancel it?") == frozenset()
