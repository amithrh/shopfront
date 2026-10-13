"""Knowledge tool: ask_policy answers policy/FAQ questions verbatim from the profile.

Matching is deterministic and explainable: the question is normalised (`search.normalize`), stop
words are removed and a light suffix stemmer folds "cancel/cancelling/cancellation" together. A few
generic folds make customer wording meet the business's: an age ("my 16 year old") becomes the age
group the variants name ("teenager"), "accept" is "take", and the multi-word place names of the
business's own locations ("Central Park") are single words, so "parking" never matches a
neighbourhood called Central Park.

Each entry scores the best Dice coefficient between the question and one of its `question_variants`,
counting only variants that cover at least `MIN_COVERAGE` of the question's content words (so "do
you validate parking at the airport" does not match "is there parking"); a `topic` equal to the
entry's topic lifts the coverage rule and adds a small boost. Below `MATCH_THRESHOLD` the reply is a
hand-off ("I don't know that. Want me to ask Rosa?"), never a guess: a wrong policy answer is worse
than asking the owner.

An entry also qualifies when its variants and its spoken answer together contain every content word of
a question of two or more words ("how fast will I get a quote" -> the entry whose variants say "how
fast" and "quote"; "do you do mold removal" -> the entry whose answer lists mold removal), as long as
each word only the answer has is rare in the profile (so an answer that mentions "the same day" in
passing never answers "same day cake"). Topics are
ranked by how many of the question's words their entries leave unexplained first, then entries by
score, so a topic that explains the whole question beats one that matches part of it more closely.

One shared word decides a match only when it is distinctive: the question has a single content word,
the word appears in no other entry's variants, or it names the entry's topic ("cancel" for the
cancellation policy). Otherwise an entry must share at least two content words with the question
(across all its variants), so "how much is the diagnostic fee?" does not match "is there a
cancellation fee?" on the word "fee". Two entries on different topics that explain as much of the
question and score within `AMBIGUITY_MARGIN` of each other (neither publishing the exact wording) are a
hand-off, not a coin toss.
"""

from collections.abc import Iterable, Sequence
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
ANSWER_WORD_ENTRIES = 2
"""A question word only an entry's answer contains counts when at most this many entries use it."""
ANSWER_BONUS = 0.1
"""Added to the floor score of a complete entry whose spoken answer alone contains every content word
of the question (it never lifts a variant match above an exact one)."""

STOP_WORDS = frozenset(
    """a an the i im i'm me my you your we our us it its is are was be been do does doe did can could would will
    should shall to for of in on at by with from about any anything there this that these those what whats
    how when where who which why if or and but so just please tell know get have has ha had need much many
    some there's here hi hello hey whether also like want wondering possible able ok okay give offer provide
    within inside something someone somebody anyone anybody""".split()
)
"""Words that carry no topic. `doe` and `ha` are how `search.normalize` (which strips a plural `s`)
spells "does" and "has"; "offer", "give" and "provide" only frame what is asked about ("do you offer
a warranty" is about the warranty)."""

SYNONYMS: dict[str, str] = {"accept": "take", "teenager": "teen"}
"""Stemmed word -> the word the business's variants use for it ("do you accept checks")."""

_SUFFIXES = ("ations", "ation", "ings", "ing", "ments", "ment", "ness", "able", "ible", "ed", "es", "s")

_TEEN_NUMBERS = {
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip


def stem(token: str) -> str:
    """Tiny suffix stripper (keeps at least 4 characters): cancellation/cancelling/cancel -> cancel."""
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            token = token[: -len(suffix)]
            break
    if token.endswith("ll") and len(token) > 4:
        token = token[:-1]
    return token


def _age_group(words: list[str]) -> list[str]:
    """'16 year old' / 'sixteen years old' -> 'teen' (13 to 19), 'kid' below, 'adult' above."""
    out: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        age = int(word) if word.isdigit() else _TEEN_NUMBERS.get(word)
        if age is not None and words[i + 1 : i + 2] in (["year"], ["yr"]) and words[i + 2 : i + 3] == ["old"]:
            out.append("kid" if age < 13 else "teen" if age < 20 else "adult")
            i += 3
            continue
        out.append(word)
        i += 1
    return out


def _join_units(words: list[str], units: Sequence[tuple[str, ...]]) -> list[str]:
    """Replace each multi-word place name (normalised words, longest first) by one token."""
    if not units:
        return words
    out: list[str] = []
    i = 0
    while i < len(words):
        for unit in units:
            if tuple(words[i : i + len(unit)]) == unit:
                out.append(" ".join(unit))
                i += len(unit)
                break
        else:
            out.append(words[i])
            i += 1
    return out


def place_units(profile: Profile) -> tuple[tuple[str, ...], ...]:
    """The normalised words of every multi-word place name of the business's locations (address
    localities and service-area localities), longest first."""
    names: list[str] = []
    for location in profile.locations():
        address = location.get("address") or {}
        if isinstance(address, dict) and address.get("locality"):
            names.append(str(address["locality"]))
        area = location.get("area") or {}
        if isinstance(area, dict):
            names += [str(place) for place in area.get("localities") or ()]
    units = {tuple(search.normalize(name).split()) for name in names}
    return tuple(sorted((u for u in units if len(u) >= 2), key=lambda u: (-len(u), u)))


def tokens(text: str, units: Sequence[tuple[str, ...]] = ()) -> frozenset[str]:
    """Content words of `text`, normalised and stemmed (`units`: multi-word place names kept whole)."""
    out: set[str] = set()
    for word in _join_units(_age_group(search.normalize(text).split()), units):
        if " " in word:
            out.add(word)
        elif word not in STOP_WORDS and len(word) > 1:
            stemmed = stem(word)
            out.add(SYNONYMS.get(stemmed, stemmed))
    return frozenset(out)


def dice(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def coverage(question: frozenset[str], variant: frozenset[str]) -> float:
    """Share of the question's content words that the variant also has."""
    return len(question & variant) / len(question) if question else 0.0


def entry_tokens(entry: JsonObj, units: Sequence[tuple[str, ...]] = ()) -> frozenset[str]:
    """Content words of every question variant of an entry."""
    return frozenset(t for variant in entry.get("question_variants") or () for t in tokens(str(variant), units))


def answer_tokens(entry: JsonObj, units: Sequence[tuple[str, ...]] = ()) -> frozenset[str]:
    """Content words of the entry's spoken answer."""
    return tokens(str(entry.get("answer_spoken") or ""), units)


def document_frequency(entries: Iterable[JsonObj], units: Sequence[tuple[str, ...]] = ()) -> dict[str, int]:
    """How many entries' variants use each content word (rare words can decide a match alone)."""
    counts: dict[str, int] = {}
    for entry in entries:
        for token in entry_tokens(entry, units):
            counts[token] = counts.get(token, 0) + 1
    return counts


def topic_tokens(entry: JsonObj) -> frozenset[str]:
    """Content words of the entry's topic (`cancellation` -> {cancel})."""
    return tokens(str(entry.get("topic") or "").replace("_", " "))


def decisive(
    question: frozenset[str], entry: JsonObj, frequency: dict[str, int] | None, units: Sequence[tuple[str, ...]] = ()
) -> bool:
    """True when the overlap with the entry's variants is enough to answer from it (see module docstring)."""
    shared = question & entry_tokens(entry, units)
    if len(shared) >= 2 or (shared and len(question) == 1):
        return True
    if len(shared) == 1:
        word = next(iter(shared))
        return frequency is None or frequency.get(word, 0) <= 1 or word in topic_tokens(entry)
    return False


def answer_frequency(entries: Iterable[JsonObj], units: Sequence[tuple[str, ...]] = ()) -> dict[str, int]:
    """How many entries use each content word in their variants or their spoken answer."""
    counts: dict[str, int] = {}
    for entry in entries:
        for token in entry_tokens(entry, units) | answer_tokens(entry, units):
            counts[token] = counts.get(token, 0) + 1
    return counts


def unexplained(question: frozenset[str], entry: JsonObj, units: Sequence[tuple[str, ...]] = ()) -> int:
    """How many of the question's content words neither the entry's variants nor its answer contain."""
    return len(question - entry_tokens(entry, units) - answer_tokens(entry, units))


def score_entry(
    question: frozenset[str],
    entry: JsonObj,
    topic: str | None,
    *,
    text: str | None = None,
    frequency: dict[str, int] | None = None,
    units: Sequence[tuple[str, ...]] = (),
    answer_counts: dict[str, int] | None = None,
) -> float:
    """Best variant score for `question` tokens; `text` (the normalised question) enables the exact-wording
    bonus, `frequency` (`document_frequency` over the profile) the distinctive-word rule and `units` the
    business's multi-word place names. An entry whose variants and answer explain a multi-word question
    completely scores at least `MATCH_THRESHOLD`, provided each word only its answer has is a rare one
    (`answer_counts`, from `answer_frequency`: in at most `ANSWER_WORD_ENTRIES` entries), so an answer
    that mentions "the same day" in passing does not answer "same day cake"."""
    entry_topic = str(entry.get("topic") or "")
    topic_named = bool(topic) and search.normalize(topic or "").replace(" ", "_") == entry_topic
    answer = answer_tokens(entry, units)
    variants = entry_tokens(entry, units)
    from_answer = question - variants
    complete = (
        len(question) >= 2
        and from_answer <= answer
        and all((answer_counts or {}).get(word, 1) <= ANSWER_WORD_ENTRIES for word in from_answer)
    )
    if not topic_named and not complete and not decisive(question, entry, frequency, units):
        return 0.0
    best = 0.0
    for variant in entry.get("question_variants") or ():
        words = tokens(str(variant), units)
        if topic_named or coverage(question, words) >= MIN_COVERAGE:
            exact = EXACT_BONUS if text is not None and search.normalize(str(variant)) == text else 0.0
            best = max(best, dice(question, words) + exact)
    if complete:
        best = max(best, MATCH_THRESHOLD + (ANSWER_BONUS if question <= answer else 0.0))
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
    """`(module id, entry, score)` of the best entry at or above the threshold, else None. Topics whose
    entries leave fewer of the question's words unexplained rank first, then entries by score. Also None
    when entries on two different topics explain as much and score within `AMBIGUITY_MARGIN`, and the
    best does not publish the question's exact wording."""
    units = place_units(profile)
    wanted = tokens(question, units)
    text = search.normalize(question)
    entries = profile.knowledge_entries()
    frequency = document_frequency([entry for _module_id, entry in entries], units)
    in_answers = answer_frequency([entry for _module_id, entry in entries], units)
    scored: list[tuple[int, float, int, str, JsonObj]] = []
    for index, (module_id, entry) in enumerate(entries):
        score = score_entry(wanted, entry, topic, text=text, frequency=frequency, units=units, answer_counts=in_answers)
        if score >= MATCH_THRESHOLD:
            scored.append((unexplained(wanted, entry, units), score, -index, module_id, entry))
    if not scored:
        return None
    # A topic is as good as its best-explaining entry: a business may split one subject over several
    # entries ("can I cancel", "how do I cancel"), and the closest wording within it should win.
    by_topic: dict[Any, int] = {}
    for row in scored:
        key = row[4].get("topic") or row[4].get("id")
        by_topic[key] = min(by_topic.get(key, row[0]), row[0])
    ranked = [(by_topic[row[4].get("topic") or row[4].get("id")], *row[1:]) for row in scored]
    fewest = min(row[0] for row in ranked)
    tier = sorted((row for row in ranked if row[0] == fewest), key=lambda row: (row[1], row[2]), reverse=True)
    _missing, score, _order, module_id, entry = tier[0]
    exact = any(search.normalize(str(v)) == text for v in entry.get("question_variants") or ())
    runner_up = next((row for row in tier[1:] if row[4].get("topic") != entry.get("topic")), None)
    if runner_up is not None and not exact and runner_up[1] > score - AMBIGUITY_MARGIN:
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
