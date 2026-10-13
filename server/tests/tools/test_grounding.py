"""Grounding (`tools.grounding`): the only path from a profile value to a result."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from support import make_live
from tools_support import BAKERY, SALON, Stack, loaded

from shopfront_server.profile.model import Profile
from shopfront_server.tools import grounding
from shopfront_server.tools.schemas import Fact, PolicyResult, check_spoken

pytestmark = pytest.mark.anyio


@pytest.fixture
def bakery(live_raw_profiles: dict[str, dict[str, Any]]) -> Profile:
    return loaded(live_raw_profiles[BAKERY])


def test_state_of_a_statable_fact(bakery: Profile) -> None:
    got = grounding.state(bakery, "business#name")
    assert got.ok and got.value == "Thimbleberry Bakehouse" and got.text == "business#name"
    assert got.fact is not None
    assert got.fact.fact_ref == "business#name" and got.fact.value == "Thimbleberry Bakehouse"
    assert got.fact.evidence_url and got.fact.evidence_url.startswith("https://")


def test_state_turns_read_only_views_into_plain_json(bakery: Profile) -> None:
    got = grounding.state(bakery, "locations/main#hours.weekly")
    assert got.ok and isinstance(got.value, list) and isinstance(got.value[0], dict)
    json.dumps(got.fact.model_dump(mode="json"))  # type: ignore[union-attr]


def test_absent_fact_is_not_statable(bakery: Profile) -> None:
    got = grounding.state(bakery, "menu/custom-cake#allergens.contains")
    assert not got.ok and got.absent and got.fact is None and got.value is None


async def test_disputed_fact_is_withheld(disputed_stack: Stack) -> None:
    profile = await disputed_stack.deps.profiles.get_live(BAKERY)
    got = grounding.state(profile, "locations/main#hours.weekly")
    assert not got.ok and got.disputed and got.fact is None
    assert got.statability.question_id == "q-saturday-close"


def test_facts_for_skips_unstatable_and_malformed_refs(bakery: Profile) -> None:
    facts = grounding.facts_for(
        bakery, ["business#name", "business#name", "menu/custom-cake#allergens.contains", "NOT A REF", "info/deposit#facts.deposit_percent"]
    )
    assert [f.fact_ref for f in facts] == ["business#name", "info/deposit#facts.deposit_percent"]
    assert facts[1].value == 50


def test_citations_carry_source_title_and_quote(bakery: Profile) -> None:
    facts = grounding.facts_for(bakery, ["locations/main#hours.weekly", "menu/country-sourdough#price"])
    citations = grounding.citations_for(bakery, facts)
    assert len(citations) == len({f.evidence_url for f in facts})
    first = citations[0]
    assert first.url == facts[0].evidence_url
    assert first.title and first.quote and len(first.quote) <= grounding.QUOTE_MAX


def test_owner_answer_citations_name_the_approver(bakery: Profile) -> None:
    fact = Fact(fact_ref="info/x#answer", value="y", evidence_url="urn:shopfront:owner-answer:q-1", owner_confirmed=True)
    [citation] = grounding.citations_for(bakery, [fact, fact])
    assert citation.title == "Confirmed by Rosa" and citation.quote is None


def test_handoff_names_the_approver(bakery: Profile) -> None:
    hand = grounding.handoff(bakery, reason="unknown", question="Do you  sell\ngift cards?" + " x" * 200, fact_ref="info/x#y")
    assert hand.approver_spoken_name == "Rosa" and hand.tool == "ask_owner" and hand.fact_ref == "info/x#y"
    assert hand.suggested_question.startswith("Do you sell gift cards?") and len(hand.suggested_question) <= 300


def test_unknown_answer_offers_to_ask(bakery: Profile) -> None:
    result = grounding.unknown_answer(bakery, question="Gift cards?", result_type=PolicyResult, topic="gifts")
    assert isinstance(result, PolicyResult) and result.topic == "gifts"
    assert result.speak == "I don't know that. Want me to ask Rosa?"
    assert result.facts == [] and result.handoff is not None and result.handoff.reason == "unknown"
    assert result.business_id == BAKERY and result.profile_revision == bakery.revision
    assert result.card is not None and result.card.actions[0].tool == "ask_owner"


def test_unknown_answer_without_an_owner_who_answers(live_raw_profiles: dict[str, dict[str, Any]]) -> None:
    doc = copy.deepcopy(live_raw_profiles[BAKERY])
    doc["modules"]["rosa"]["answers_unknown_questions"] = False
    profile = Profile.unchecked(doc)
    assert not grounding.can_ask_owner(profile)
    result = grounding.unknown_answer(profile, question="Gift cards?")
    assert result.handoff is None and result.speak == "I don't know that. Thimbleberry Bakehouse can tell you directly."


async def test_withheld_answer_never_mentions_the_candidates(disputed_stack: Stack, raw_profiles: dict[str, Any]) -> None:
    profile = await disputed_stack.deps.profiles.get_live(BAKERY)
    result = grounding.withheld_answer(
        profile, question="When do you close on Saturday?", fact_ref="locations/main#hours.weekly", question_id="q-saturday-close"
    )
    assert result.speak == "Rosa is double-checking that, so I can't say for sure yet. Want me to ask Rosa?"
    assert result.handoff is not None and result.handoff.reason == "disputed"
    dumped = json.dumps(result.model_dump()).lower()
    question = next(q for q in raw_profiles[BAKERY]["open_questions"] if q["id"] == "q-saturday-close")
    for candidate in question["candidates"]:
        assert str(candidate["value"]).lower() not in dumped
        assert candidate["label"].lower() not in dumped
    assert "q-saturday-close" not in dumped
    check_spoken(result.speak)


async def test_knowledge_answer_is_disputed_when_one_of_its_facts_is(disputed_stack: Stack, stack: Stack) -> None:
    disputed = await disputed_stack.deps.profiles.get_live(SALON)
    entry = disputed.knowledge_entry("info/cancellation")
    answer, facts = grounding.knowledge_answer(disputed, "info/cancellation", entry)
    assert not answer.ok and answer.disputed and answer.text == "info/cancellation#facts.notice_minutes"
    assert "info/cancellation#facts.notice_minutes" not in [f.text for f in facts]
    clean = await stack.deps.profiles.get_live(SALON)
    answer, facts = grounding.knowledge_answer(clean, "info/cancellation", clean.knowledge_entry("info/cancellation"))
    assert answer.ok and answer.value == entry["answer_spoken"]
    assert {f.text for f in facts} >= {"info/cancellation#facts.notice_minutes"}


def test_notice_card() -> None:
    card = grounding.notice_card("Heads up", [("When", "Today")])
    assert card.kind == "notice" and card.lines[0].label == "When" and card.lines[0].value == "Today"


def test_make_live_keeps_reopened_questions_open(raw_profiles: dict[str, Any]) -> None:
    from tools_support import reopen_question

    doc = make_live(reopen_question(raw_profiles[BAKERY], "q-saturday-close"))
    question = next(q for q in doc["open_questions"] if q["id"] == "q-saturday-close")
    assert question["status"] == "open" and question["blocking"] is False and "answer" not in question
