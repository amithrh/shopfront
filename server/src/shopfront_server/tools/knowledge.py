"""Knowledge tool: ask_policy answers policy/FAQ questions verbatim from the profile.

Matching is deterministic and explainable: the question is normalised (`search.normalize`), stop
words are removed and a light suffix stemmer folds "cancel/cancelling/cancellation" together. Each
entry scores the best Dice coefficient between the question and one of its `question_variants`,
counting only variants that cover at least `MIN_COVERAGE` of the question's content words (so "do
you validate parking at the airport" does not match "is there parking"); a `topic` equal to the
entry's topic lifts the coverage rule and adds a small boost. Below `MATCH_THRESHOLD` the reply is a
hand-off ("I don't know that. Want me to ask Rosa?"), never a guess: a wrong policy answer is worse
than asking the owner.

One shared word decides a match only when it is distinctive: the question has a single content word,
the word appears in no other entry's variants, or it names the entry's topic ("cancel" for the
cancellation policy). Otherwise an entry must share at least two content words with the question
(across all its variants), so "how much is the diagnostic fee?" does not match "is there a
cancellation fee?" on the word "fee". Two entries on different topics scoring within
`AMBIGUITY_MARGIN` of each other (neither publishing the exact wording) are a hand-off, not a coin toss.
"""

from typing import Annotated, Any

from pydantic import Field

from mcp.server.mcpserver import Context
from shopfront_server.profile.model import JsonObj, Profile
from shopfront_server.tools import grounding, search, speech
from shopfront_server.tools.context import finish, get_deps, live_profile, tool_span
from shopfront_server.tools.discovery import BusinessId
from shopfront_server.tools.guard import sanitize_speak
from shopfront_server.tools.schemas import Card, CardLine, Fact, PolicyResult, check_spoken

MATCH_THRESHOLD = 0.5
MIN_COVERAGE = 0.5
TOPIC_BOOST = 0.2
EXACT_BONUS = 0.05
"""Tie-breaker: the entry that publishes the question's exact wording wins over equivalent ones."""
AMBIGUITY_MARGIN = 0.1
"""Best and runner-up entries closer than this (without an exact-wording match) are ambiguous."""

STOP_WORDS = frozenset(
    """a an the i im i'm me my you your we our us it its is are was be been do does did can could would will
    should shall to for of in on at by with from about any anything there this that these those what whats
    how when where who which why if or and but so just please tell know get have has had need much many
    some there's here hi hello hey whether also like want wondering possible able ok okay""".split()
)

_SUFFIXES = ("ations", "ation", "ings", "ing", "ments", "ment", "ness", "able", "ible", "ed", "es", "s")


def stem(token: str) -> str:
    """Tiny suffix stripper (keeps at least 4 characters): cancellation/cancelling/cancel -> cancel."""
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            token = token[: -len(suffix)]
            break
    if token.endswith("ll") and len(token) > 4:
        token = token[:-1]
    return token


def tokens(text: str) -> frozenset[str]:
    """Content words of `text`, normalised and stemmed."""
    return frozenset(stem(t) for t in search.normalize(text).split() if t not in STOP_WORDS and len(t) > 1)


def dice(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def coverage(question: frozenset[str], variant: frozenset[str]) -> float:
    """Share of the question's content words that the variant also has."""
    return len(question & variant) / len(question) if question else 0.0


def entry_tokens(entry: JsonObj) -> frozenset[str]:
    """Content words of every question variant of an entry."""
    return frozenset(t for variant in entry.get("question_variants") or () for t in tokens(str(variant)))


def document_frequency(entries: list[JsonObj]) -> dict[str, int]:
    """How many entries' variants use each content word (rare words can decide a match alone)."""
    counts: dict[str, int] = {}
    for entry in entries:
        for token in entry_tokens(entry):
            counts[token] = counts.get(token, 0) + 1
    return counts


def topic_tokens(entry: JsonObj) -> frozenset[str]:
    """Content words of the entry's topic (`cancellation` -> {cancel})."""
    return tokens(str(entry.get("topic") or "").replace("_", " "))


def decisive(question: frozenset[str], entry: JsonObj, frequency: dict[str, int] | None) -> bool:
    """True when the overlap with the entry is enough to answer from it (see module docstring)."""
    shared = question & entry_tokens(entry)
    if len(shared) >= 2 or (shared and len(question) == 1):
        return True
    if len(shared) == 1:
        word = next(iter(shared))
        return frequency is None or frequency.get(word, 0) <= 1 or word in topic_tokens(entry)
    return False


def score_entry(
    question: frozenset[str],
    entry: JsonObj,
    topic: str | None,
    *,
    text: str | None = None,
    frequency: dict[str, int] | None = None,
) -> float:
    """Best variant score for `question` tokens; `text` (the normalised question) enables the exact-wording
    bonus, and `frequency` (`document_frequency` over the profile) the distinctive-word rule."""
    entry_topic = str(entry.get("topic") or "")
    topic_named = bool(topic) and search.normalize(topic or "").replace(" ", "_") == entry_topic
    if not topic_named and not decisive(question, entry, frequency):
        return 0.0
    best = 0.0
    for variant in entry.get("question_variants") or ():
        words = tokens(str(variant))
        if topic_named or coverage(question, words) >= MIN_COVERAGE:
            exact = EXACT_BONUS if text is not None and search.normalize(str(variant)) == text else 0.0
            best = max(best, dice(question, words) + exact)
    if best == 0.0:
        return 0.0
    if topic_named:
        best += TOPIC_BOOST
    elif entry_topic and stem(search.normalize(entry_topic.replace("_", " ")).split()[0]) in question:
        best += TOPIC_BOOST / 2
    return best


def verbatim(answer_spoken: str) -> str:
    """The entry's `answer_spoken` unchanged; sanitised only if it would fail `check_spoken` (the schema
    already forbids that, so this is defence in depth against a hand-edited profile)."""
    try:
        return check_spoken(answer_spoken)
    except ValueError:
        return sanitize_speak(answer_spoken)


def best_entry(profile: Profile, question: str, topic: str | None) -> tuple[str, JsonObj, float] | None:
    """`(module id, entry, score)` of the best-scoring entry at or above the threshold, else None
    (also None when two different entries are within `AMBIGUITY_MARGIN` and neither publishes the
    question's exact wording)."""
    wanted = tokens(question)
    text = search.normalize(question)
    entries = profile.knowledge_entries()
    frequency = document_frequency([entry for _module_id, entry in entries])
    scored: list[tuple[float, int, str, JsonObj]] = []
    for index, (module_id, entry) in enumerate(entries):
        score = score_entry(wanted, entry, topic, text=text, frequency=frequency)
        if score >= MATCH_THRESHOLD:
            scored.append((score, -index, module_id, entry))
    if not scored:
        return None
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    score, _order, module_id, entry = scored[0]
    exact = any(search.normalize(str(v)) == text for v in entry.get("question_variants") or ())
    runner_up = next((row for row in scored[1:] if row[3].get("topic") != entry.get("topic")), None)
    if runner_up is not None and not exact and runner_up[0] > score - AMBIGUITY_MARGIN:
        return None  # two answers on different topics fit about equally well: ask instead of guessing
    return module_id, entry, score


async def ask_policy(
    business_id: BusinessId,
    question: Annotated[str, Field(description="The customer's question, e.g. 'can I get my deposit back?'.", max_length=300)],
    ctx: Context,
    topic: Annotated[
        str | None, Field(description="Optional topic: deposit, cancellation, parking, payment...", max_length=40)
    ] = None,
) -> PolicyResult:
    """Answer a policy or FAQ question from the business's published policies (returned verbatim, with
    the source on the card). If no entry matches, the reply offers to ask the owner instead of guessing."""
    deps = get_deps(ctx)
    with tool_span("ask_policy", business_id):
        profile = await live_profile(deps, business_id)
        match = best_entry(profile, question, topic)
        if match is None:
            result = grounding.unknown_answer(profile, question=question, result_type=PolicyResult, topic=topic)
            return finish(result, profile, outcome="handoff")
        module_id, entry, _score = match
        entry_ref = f"{module_id}/{entry['id']}"
        answer, fact_states = grounding.knowledge_answer(profile, entry_ref, entry)
        if not answer.ok:
            if answer.disputed:
                result = grounding.withheld_answer(
                    profile,
                    question=question,
                    fact_ref=answer.text,
                    question_id=answer.statability.question_id,
                    result_type=PolicyResult,
                    entry_ref=entry_ref,
                    topic=str(entry.get("topic") or "") or None,
                )
                return finish(result, profile, outcome="withheld")
            result = grounding.unknown_answer(
                profile, question=question, fact_ref=answer.text, result_type=PolicyResult, topic=topic
            )
            return finish(result, profile, outcome="handoff")
        facts: list[Fact] = [answer.fact]  # type: ignore[list-item]
        facts += [s.fact for s in fact_states if s.fact is not None]
        card_text: Any = entry.get("answer_card")
        result = PolicyResult(
            speak=verbatim(str(answer.value)),
            facts=facts,
            card=Card(
                kind="policy",
                title=speech.capitalize(str(entry.get("topic") or "Policy").replace("_", " ")),
                subtitle=profile.name,
                lines=[CardLine(label="Answer", value=str(card_text or answer.value))],
                citations=grounding.citations_for(profile, facts),
            ),
            entry_ref=entry_ref,
            topic=str(entry.get("topic") or "") or None,
        )
        return finish(result, profile, outcome="ok")
