"""Knowledge tool: ask_policy answers policy/FAQ questions verbatim from the profile.

Matching is deterministic and explainable: the question is normalised (`search.normalize`, accents
dropped), stop words are removed and a light suffix stemmer folds "cancel/cancelling/cancellation" and
"charge/charged" together. A few generic folds make customer wording meet the business's: an age ("my
16 year old") becomes the age group the variants name ("teenager"), synonyms and idioms become the
plain word (`SYNONYMS`, `PHRASES`: "accept" is "take", "put down a deposit" is paying it, "without an
appointment" is a walk-in), words for the business itself ("the bakery") count only when they are all
the question says, a bookable resource's name brings its kind ("Wren" -> stylist), and the multi-word
place names of the business's own locations ("Central Park") are single words, so "parking" never
matches a neighbourhood called Central Park.

Each entry scores the best Dice coefficient between the question and one of its `question_variants`,
counting only variants that cover at least `MIN_COVERAGE` of the question's content words, alone or
(sharing two words with it) together with the entry's answer and topic words, so "do you validate
parking at the airport" does not match "is there parking". A `topic` equal to the entry's topic lifts
the coverage rule and adds a small boost; so, with half that boost, does a question that names the
entry's topic word ("can I park out front", "how much is the deposit on a big cake") unless most of
its words are ones the profile never uses. Below `MATCH_THRESHOLD` the reply is a hand-off ("I don't
know that. Want me to ask Rosa?"), never a guess: a wrong policy answer is worse than asking the owner.

An entry also qualifies when its variants and its spoken answer together contain every content word of
the question ("how fast will I get a quote" -> the entry whose variants say "how fast" and "quote"; "do
you do mold removal" -> the entry whose answer lists mold removal), as long as each word only the
answer has is rare in the profile (so an answer that mentions "the same day" in passing never answers
"same day cake"; a one-word question, "refund", needs the one entry that uses it). Topics are ranked by
how many of the question's words their entries leave unexplained first, then entries by score, so a
topic that explains the whole question beats one that matches part of it more closely; a kind of thing
the business sells (a catalog category: "bread") left unexplained weighs more than any other word.

One shared word decides a match only when it is distinctive: the question has a single content word,
the word names the entry's topic ("cancel" for the cancellation policy), or no other entry's variants
use it and the entry explains the rest of the question (so "tomorrow" alone never makes "what's the
weather like tomorrow" a cake question, nor "card" a loyalty card a payment one). Otherwise an entry
must share at least two content words with the question (across all its variants), so "how much is the
diagnostic fee?" does not match "is there a cancellation fee?" on the word "fee". Two entries on
different topics that explain as much of the question and score within `AMBIGUITY_MARGIN` of each other
(neither publishing the exact wording) are a hand-off, not a coin toss, unless only one of them is on
the topic the question names.
"""

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Annotated, Any

from pydantic import Field

from mcp.server.mcpserver import Context
from askonce_server.profile.model import JsonObj, Profile
from askonce_server.tools import grounding, search, speech
from askonce_server.tools.context import finish, get_deps, live_profile, tool_span
from askonce_server.tools.discovery import BusinessId
from askonce_server.tools.guard import sanitize_speak
from askonce_server.tools.schemas import Card, CardLine, Fact, PolicyResult, check_spoken

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
    within inside something someone somebody anyone anybody policy no not going gonna form kind type sort""".split()
)
"""Words that carry no topic. `doe` and `ha` are how `search.normalize` (which strips a plural `s`)
spells "does" and "has"; "offer", "give" and "provide" only frame what is asked about ("do you offer
a warranty" is about the warranty), as do "policy" ("what's your refund policy") and "form", "kind",
"type" ("what forms of payment")."""

SYNONYMS: dict[str, str] = {
    "accept": "take", "teenager": "teen", "owner": "own", "request": "ask",
    "loaf": "bread", "loave": "bread", "dye": "color", "colour": "color", "highlight": "color", "hate": "dislike",
    "restroom": "bathroom", "washroom": "bathroom", "contactless": "tap",
}  # fmt: skip
"""Stemmed word -> the word the business's variants use for it ("do you accept checks", "who owns",
"can I request Wren"). A few name the kind of thing: a loaf is bread, hair dye
and highlights are hair color, contactless is tap to pay."""

PHRASES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("dont", "like"): ("dislike",),
    ("do", "not", "like"): ("dislike",),
    ("without", "an", "appointment"): ("walk", "in"),
    ("without", "appointment"): ("walk", "in"),
    ("put", "down"): ("pay",),
    ("apple", "pay"): ("tap", "pay"),
    ("google", "pay"): ("tap", "pay"),
    ("samsung", "pay"): ("tap", "pay"),
}
"""Normalised idioms -> the plain words they mean ("put down a deposit" is paying it, coming in "without
an appointment" is a walk-in, Apple Pay is tap to pay), folded before stop words are removed."""

WEAK_WORDS = frozenset("bakery bakehouse salon studio shop store business company work".split())
"""Words for the business itself and for working there ("who owns the bakery", "how many stylists work
there", "do you work in Central Park"): dropped when the text has other content words, kept when they
are all it says ("tell me about the bakery", "who will do the work")."""

_SUFFIXES = (
    "ability", "ibility", "ations", "ation", "ings", "ing", "ments", "ment", "ness", "able", "ible", "ed", "es", "s",
)  # fmt: skip

_TEEN_NUMBERS = {
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip


def normalize(text: str) -> str:
    """`search.normalize` without accents, so "espanol" and "Ines" meet "español" and "Inés"."""
    decomposed = unicodedata.normalize("NFKD", text)
    return search.normalize("".join(c for c in decomposed if not unicodedata.combining(c)))


def stem(token: str) -> str:
    """Tiny suffix stripper (keeps at least 4 characters): cancellation/cancelling/cancel -> cancel,
    charged/charging/charge -> charge."""
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            token = token[: -len(suffix)]
            if suffix in ("ed", "ing") and token[-1] in "cgvz" and token[-2] != token[-1]:
                token += "e"  # charged/charging -> charge, placed -> place
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


def _join_units(
    words: list[str], units: Sequence[tuple[str, ...]], replace: dict[tuple[str, ...], tuple[str, ...]] | None = None
) -> list[str]:
    """Replace each multi-word place name (normalised words, longest first) by one token, or (`replace`)
    each phrase by the words it means."""
    if not units:
        return words
    out: list[str] = []
    i = 0
    while i < len(words):
        for unit in units:
            if tuple(words[i : i + len(unit)]) == unit:
                out.extend(replace[unit] if replace is not None else [" ".join(unit)])
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
    units = {tuple(normalize(name).split()) for name in names}
    return tuple(sorted((u for u in units if len(u) >= 2), key=lambda u: (-len(u), u)))


_PLACE_AFTER = re.compile(
    r"\b(?:to|in|near|around|into|out to|over to|up to|down to)\s+"
    r"((?:[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,3})|\d{5}(?:-\d{4})?|[A-Z]\d[A-Z]\s?\d[A-Z]\d)"
)
"""A place the customer names after a place preposition: a capitalised name ("to Lake Oswego"), a ZIP code
or a Canadian postal code. Capitalisation is what tells "to Beaverton" from "to cancel"."""


def named_places(question: str, units: Sequence[tuple[str, ...]] = ()) -> frozenset[str]:
    """The content words of the places `question` names (see `_PLACE_AFTER`)."""
    words: set[str] = set()
    for match in _PLACE_AFTER.finditer(question):
        words |= tokens(match.group(1), units)
    return frozenset(words)


def tokens(text: str, units: Sequence[tuple[str, ...]] = ()) -> frozenset[str]:
    """Content words of `text`, normalised and stemmed (`units`: multi-word place names kept whole)."""
    out: set[str] = set()
    words = _join_units(_age_group(normalize(text).split()), units)
    for word in _join_units(words, tuple(PHRASES), PHRASES):
        if " " in word:
            out.add(word)
        elif word not in STOP_WORDS and len(word) > 1:
            out.add(fold(word))
    content = out - WEAK_WORDS
    return frozenset(content or out)


def fold(word: str) -> str:
    """One normalised word -> its stemmed, synonym-folded form."""
    stemmed = stem(word)
    return SYNONYMS.get(stemmed, stemmed)


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


def topic_head(entry: JsonObj) -> str | None:
    """The word that names the entry's topic: its first word (`walk_ins` -> walk, `gift_cards` -> gift)."""
    words = normalize(str(entry.get("topic") or "").replace("_", " ")).split()
    return fold(words[0]) if words else None


def decisive(
    question: frozenset[str],
    entry: JsonObj,
    frequency: dict[str, int] | None,
    units: Sequence[tuple[str, ...]] = (),
) -> bool:
    """True when the overlap with the entry's variants is enough to answer from it (see module docstring)."""
    variants = entry_tokens(entry, units)
    shared = question & variants
    if len(shared) >= 2 or (shared and len(question) == 1):
        return True
    if len(shared) != 1:
        return False
    word = next(iter(shared))
    if frequency is None or word == topic_head(entry):
        return True
    # A word no other entry uses decides only when the entry also explains the rest of the question:
    # "tomorrow" alone does not make "what's the weather like tomorrow" a cake question.
    return frequency.get(word, 0) <= 1 and question - {word} <= variants | answer_tokens(entry, units)


def variant_tokens(entry: JsonObj, units: Sequence[tuple[str, ...]] = ()) -> list[frozenset[str]]:
    """Content words of each question variant of an entry."""
    return [tokens(str(variant), units) for variant in entry.get("question_variants") or ()]


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


def unknown_words(question: frozenset[str], answer_counts: dict[str, int]) -> frozenset[str]:
    """Question words the business never uses in any variant or answer (numbers aside)."""
    return frozenset(word for word in question if word not in answer_counts and not word.isdigit())


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
    business's multi-word place names.

    A variant counts when it covers `MIN_COVERAGE` of the question, alone or, when it shares at least two
    words with it, together with the entry's answer and topic words. A question that names the entry's
    topic ("can I park out front", "what forms of payment") lifts that rule, unless most of its words are
    ones the business never uses (`answer_counts`, from `answer_frequency`: then it is about something
    the profile does not cover). An entry whose variants and answer explain the question completely
    scores at least `MATCH_THRESHOLD`, provided each word only its answer has is a rare one (in at most
    `ANSWER_WORD_ENTRIES` entries; for a one-word question, in that entry alone), so an answer that
    mentions "the same day" in passing does not answer "same day cake"."""
    entry_topic = str(entry.get("topic") or "")
    topic_named = bool(topic) and normalize(topic or "").replace(" ", "_") == entry_topic
    head_named = topic_head(entry) in question
    lifted = topic_named or (
        head_named
        and answer_counts is not None
        and 2 * len(unknown_words(question, answer_counts) - topic_tokens(entry)) < len(question)
    )
    answer = answer_tokens(entry, units)
    variants = entry_tokens(entry, units)
    from_answer = question - variants
    limit = ANSWER_WORD_ENTRIES if len(question) >= 2 else 1 if answer_counts is not None else 0
    complete = from_answer <= answer and all((answer_counts or {}).get(word, 1) <= limit for word in from_answer)
    if not lifted and not complete and not decisive(question, entry, frequency, units):
        return 0.0
    extra = answer | topic_tokens(entry)
    best = 0.0
    for variant, words in zip(entry.get("question_variants") or (), variant_tokens(entry, units), strict=True):
        with_answer = coverage(question, words | extra) if len(question & words) >= 2 else 0.0
        if lifted or max(coverage(question, words), with_answer) >= MIN_COVERAGE:
            exact = EXACT_BONUS if text is not None and normalize(str(variant)) == text else 0.0
            best = max(best, dice(question, words) + exact)
    if complete:
        best = max(best, MATCH_THRESHOLD + (ANSWER_BONUS if question <= answer else 0.0))
    if best == 0.0:
        return 0.0
    if topic_named:
        best += TOPIC_BOOST
    elif head_named:
        best += TOPIC_BOOST / 2
    return best


def verbatim(answer_spoken: str) -> str:
    """The entry's `answer_spoken` unchanged; sanitised only if it would fail `check_spoken` (the schema
    already forbids that, so this is defence in depth against a hand-edited profile)."""
    try:
        return check_spoken(answer_spoken)
    except ValueError:
        return sanitize_speak(answer_spoken)


def named_resources(profile: Profile, text: str) -> frozenset[str]:
    """The kind of every bookable resource the normalised question names ("can I request Wren" ->
    stylist), so a question about one stylist meets the entries about stylists."""
    kinds: set[str] = set()
    for _module_id, module in profile.modules_of_type("booking"):
        for resource in module.get("resources") or ():
            name = normalize(str(resource.get("spoken_name") or ""))
            if resource.get("kind") and search.contains_phrase(text, name):
                kinds |= tokens(str(resource["kind"]))
    return frozenset(kinds)


def catalog_kinds(profile: Profile) -> frozenset[str]:
    """Content words of the catalog's category names: the kinds of thing the business sells (breads, cakes)."""
    return frozenset(
        word
        for _module_id, module in profile.modules_of_type("catalog")
        for category in module.get("categories") or ()
        for word in tokens(str(category.get("spoken_name") or category.get("name") or ""))
    )


def best_entry(profile: Profile, question: str, topic: str | None) -> tuple[str, JsonObj, float] | None:
    """`(module id, entry, score)` of the best entry at or above the threshold, else None. Topics whose
    entries leave fewer of the question's words unexplained rank first (an unexplained kind of thing the
    business sells, like "bread" against a cake entry, before any other word), then entries that explain
    every kind the question names, then entries by score. When
    entries on different topics explain as much and score within `AMBIGUITY_MARGIN` (the best not
    publishing the question's exact wording), the one whose topic the question names wins ("the deposit on
    a big cake"); with none or several named, None: ask instead of guessing."""
    units = place_units(profile)
    text = normalize(question)
    wanted = tokens(question, units) | named_resources(profile, text)
    entries = profile.knowledge_entries()
    frequency = document_frequency([entry for _module_id, entry in entries], units)
    in_answers = answer_frequency([entry for _module_id, entry in entries], units)
    # The business's own name is in many answers, but an answer that names it ("Kettlewick started in 2014")
    # explains it in a question as well as a rare word would ("when did Kettlewick start").
    for word in tokens(str(profile.business.get("name") or "")) | tokens(str(profile.business.get("spoken_name") or "")):
        if word in in_answers:
            in_answers[word] = 1
    # "Do you deliver to Beaverton?": a place the customer names is where, not what, they ask about. A
    # place the business never mentions is left out of the match (one it does mention still counts).
    unknown_places = {w for w in named_places(question, units) if not frequency.get(w) and not in_answers.get(w)}
    if wanted - unknown_places:
        wanted = wanted - unknown_places
    kinds = catalog_kinds(profile)
    scored: list[tuple[tuple[int, int], float, int, str, JsonObj]] = []
    for index, (module_id, entry) in enumerate(entries):
        score = score_entry(wanted, entry, topic, text=text, frequency=frequency, units=units, answer_counts=in_answers)
        if score >= MATCH_THRESHOLD:
            missing = wanted - entry_tokens(entry, units) - answer_tokens(entry, units)
            scored.append(((len(missing & kinds), len(missing)), score, -index, module_id, entry))
    if not scored:
        return None

    def topic_of(row: tuple[Any, ...]) -> Any:
        return row[4].get("topic") or row[4].get("id")

    # A topic is as good as its best-explaining entry: a business may split one subject over several
    # entries ("can I cancel", "how do I cancel"), and the closest wording within it should win.
    by_topic: dict[Any, tuple[int, int]] = {}
    for row in scored:
        by_topic[topic_of(row)] = min(by_topic.get(topic_of(row), row[0]), row[0])
    fewest = min(by_topic.values())

    def exact(row: tuple[Any, ...]) -> bool:
        return any(normalize(str(v)) == text for v in row[4].get("question_variants") or ())

    # Within the tier an entry that leaves a kind of thing unexplained itself ranks last, even beside a
    # better-worded entry of its own topic ("gluten-free bread" is not the gluten-free cakes entry), and
    # the entry publishing the question's exact wording first.
    tier = sorted(
        (row for row in scored if by_topic[topic_of(row)] == fewest),
        key=lambda row: (-row[0][0], exact(row), row[1], row[2]),
        reverse=True,
    )
    best = tier[0]
    close = [row for row in tier if row[1] > best[1] - AMBIGUITY_MARGIN]
    if not exact(best) and len({topic_of(row) for row in close}) > 1:
        named = [row for row in close if topic_head(row[4]) in wanted]
        if not named or len({topic_of(row) for row in named}) > 1:
            return None  # two answers on different topics fit about equally well: ask instead of guessing
        best = named[0]
    _missing, score, _order, module_id, entry = best
    return module_id, entry, score


async def ask_policy(
    business_id: BusinessId,
    question: Annotated[str, Field(description="The customer's question, e.g. 'can I get my deposit back?'.", max_length=300)],
    ctx: Context,
    topic: Annotated[
        str | None, Field(description="Optional topic: deposit, cancellation, parking, payment...", max_length=40)
    ] = None,
) -> PolicyResult:
    """Answer a policy or FAQ question from the business's published policies and the owner's earlier
    answers (returned verbatim, with the source on the card): delivery, payment, deposits, cancellation,
    parking, gift cards, "do you..." questions. If no entry matches, the reply offers to ask the owner
    instead of guessing."""
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
        return finish(_stated_policy(profile, entry_ref, entry, answer, fact_states), profile, outcome="ok")


def _stated_policy(profile: Profile, entry_ref: str, entry: JsonObj, answer: Any, fact_states: Any) -> PolicyResult:
    """The result for a policy entry whose answer is statable (`answer.ok`): verbatim, with its sources."""
    facts: list[Fact] = [answer.fact]
    facts += [s.fact for s in fact_states if s.fact is not None]
    card_text: Any = entry.get("answer_card")
    return PolicyResult(
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


def stated_policy_answer(profile: Profile, question: str, topic: str | None = None) -> PolicyResult | None:
    """The published (or owner-answered) policy that answers `question`, when one matches and is
    statable; None otherwise (unknown or disputed: the caller keeps its own hand-off)."""
    match = best_entry(profile, question, topic)
    if match is None:
        return None
    module_id, entry, _score = match
    entry_ref = f"{module_id}/{entry['id']}"
    answer, fact_states = grounding.knowledge_answer(profile, entry_ref, entry)
    if not answer.ok:
        return None
    return _stated_policy(profile, entry_ref, entry, answer, fact_states)
