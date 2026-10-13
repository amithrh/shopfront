"""Builders for grounded results: turn profile facts into `Fact`s, hand-offs and withheld answers.

The only path from a profile value to a tool result. Each helper resolves the fact ref, applies the
statable rule (L6) and returns either a usable fact or the reason it cannot be said:

    got = state(profile, "menu/celebration-cake#allergens")
    if got.ok:   speak about got.value, append got.fact
    else:        return unknown_answer(profile, ref, question=...)   # hand-off, never a guess

`profile.factref` and `profile.statable` are called through their modules (not imported names) so the
profile work package's implementations are always the ones in force.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from shopfront_server.profile import factref as _factref
from shopfront_server.profile import statable as _statable
from shopfront_server.profile.factref import FactRef
from shopfront_server.profile.model import Profile
from shopfront_server.profile.statable import Statability
from shopfront_server.tools import speech
from shopfront_server.tools.schemas import Card, CardAction, CardLine, Citation, Fact, Grounded, HandOff

G = TypeVar("G", bound=Grounded)

OWNER_URN_PREFIX = "urn:shopfront:owner-"
QUOTE_MAX = 300


@dataclass(frozen=True)
class Stated:
    """A fact lookup outcome. `fact` is set only when `ok`.

    `text` is the canonical ref string used in `Fact.fact_ref`; `evidence` is the resolved
    `(url, quote)` evidence (for card citations), empty when the fact is absent.
    """

    ref: FactRef
    ok: bool
    value: Any
    fact: Fact | None
    statability: Statability
    text: str = ""
    evidence: tuple[tuple[str, str], ...] = ()

    @property
    def disputed(self) -> bool:
        return self.statability.reason == "disputed"

    @property
    def absent(self) -> bool:
        return self.statability.reason == "absent"


def plain(value: Any) -> Any:
    """Deep copy of a profile value as plain JSON types (read-only mapping views become dicts)."""
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def state(profile: Profile, ref: FactRef | str) -> Stated:
    """Resolve + statable check (see `profile.factref.resolve`, `profile.statable.statability`)."""
    resolved = _factref.resolve(profile, ref)
    verdict = _statable.statability(profile, resolved)
    text = ref if isinstance(ref, str) else str(ref)
    evidence = tuple((e.url, e.quote) for e in resolved.evidence) if resolved.present else ()
    fact: Fact | None = None
    if verdict.statable:
        fact = Fact(
            fact_ref=text,
            value=plain(resolved.value),
            evidence_url=resolved.evidence_url,
            owner_confirmed=bool(resolved.owner_confirmed),
        )
    return Stated(
        ref=resolved.ref,
        ok=verdict.statable,
        value=plain(resolved.value) if verdict.statable else None,
        fact=fact,
        statability=verdict,
        text=text,
        evidence=evidence,
    )


def is_disputed(profile: Profile, got: Stated) -> bool:
    """True when `got` is withheld by an open contradiction/confirm question, including a fact that is
    absent but asked about ("the owner is double-checking" rather than "I don't know")."""
    if got.disputed:
        return True
    return got.absent and _statable.disputed_by(profile, got.ref) is not None


def facts_for(profile: Profile, refs: Sequence[str]) -> list[Fact]:
    """`Fact`s for refs that are statable, silently skipping the rest (use when every ref was already checked)."""
    out: list[Fact] = []
    seen: set[str] = set()
    for ref in refs:
        if ref in seen:
            continue
        seen.add(ref)
        try:
            got = state(profile, ref)
        except ValueError:  # malformed ref (FactRefError) from a caller-supplied list
            continue
        if got.fact is not None:
            out.append(got.fact)
    return out


SAFETY_TOPICS: frozenset[str] = frozenset({"allergens", "allergy", "dietary", "safety"})
"""Knowledge topics whose spoken answer is itself a safety claim (held to the owner-confirmed rule)."""


def knowledge_answer(profile: Profile, entry_ref: str, entry: Mapping[str, Any]) -> tuple[Stated, list[Stated]]:
    """`(answer_spoken, facts.*)` of a knowledge entry, with the answer withheld if any fact is disputed
    or is a safety fact the owner has not confirmed.

    `answer_spoken` embeds the facts kept in the entry's `facts` ("24 hours' notice" and
    `facts.notice_minutes`; "our kitchen isn't nut-free" and `facts.nut_free_kitchen`), so a fact that
    may not be said makes the spoken answer unsayable too, even though the two refs do not overlap
    field-wise. The same holds for an entry whose topic is a safety topic (`SAFETY_TOPICS`) when its
    answer is not owner-confirmed. The returned answer `Stated` then carries the blocking statability
    (reason `disputed` or `unconfirmed_safety`) and no `fact`.
    """
    answer = state(profile, f"{entry_ref}#answer_spoken")
    facts = [state(profile, f"{entry_ref}#facts.{key}") for key in sorted((entry.get("facts") or {}).keys())]
    blocking = next((f for f in facts if f.disputed), None) or next(
        (f for f in facts if f.statability.reason == "unconfirmed_safety"), None
    )
    if answer.ok and blocking is None and str(entry.get("topic") or "") in SAFETY_TOPICS:
        if answer.fact is not None and not answer.fact.owner_confirmed:
            blocking = Stated(
                ref=answer.ref,
                ok=False,
                value=None,
                fact=None,
                statability=Statability(statable=False, reason="unconfirmed_safety"),
                text=answer.text,
            )
    if answer.ok and blocking is not None:
        answer = Stated(
            ref=answer.ref,
            ok=False,
            value=None,
            fact=None,
            statability=blocking.statability,
            text=blocking.text,
            evidence=answer.evidence,
        )
    return answer, [f for f in facts if f.ok]


def dedupe_facts(facts: Sequence[Fact]) -> list[Fact]:
    """Facts in first-seen order without duplicate refs."""
    out: list[Fact] = []
    seen: set[str] = set()
    for fact in facts:
        if fact.fact_ref not in seen:
            seen.add(fact.fact_ref)
            out.append(fact)
    return out


def _source_title(profile: Profile, url: str) -> str | None:
    if url.startswith(OWNER_URN_PREFIX):
        return f"Confirmed by {profile.approver_spoken_name()}"
    found = profile.source_by_url(url)
    if found is None:
        return None
    _source_id, source = found
    title = source.get("title")
    return str(title) if title else None


def citations_for(profile: Profile, facts: Sequence[Fact]) -> list[Citation]:
    """Deduplicated card citations (web sources get the source title and the evidence quote)."""
    citations: list[Citation] = []
    seen: set[str] = set()
    for fact in facts:
        url = fact.evidence_url
        if not url or url in seen:
            continue
        seen.add(url)
        quote: str | None = None
        try:
            resolved = _factref.resolve(profile, fact.fact_ref)
        except ValueError:
            resolved = None
        if resolved is not None:
            for evidence in resolved.evidence:
                if evidence.url == url and evidence.quote:
                    quote = speech.truncate(" ".join(evidence.quote.split()), QUOTE_MAX)
                    break
        citations.append(Citation(url=url, title=_source_title(profile, url), quote=quote))
    return citations


# ---------------------------------------------------------------------------- hand-offs


def can_ask_owner(profile: Profile, module_id: str | None = None) -> bool:
    """True if the profile routes unknown questions to an owner (owner_approval module that answers)."""
    try:
        _owner_id, owner = profile.owner_approval(module_id)
    except Exception:  # noqa: BLE001 - no owner module (UnknownModule) means "cannot ask"
        return False
    return owner.get("answers_unknown_questions", True) is not False


def handoff(
    profile: Profile, *, reason: str, question: str, fact_ref: str | None = None, module_id: str | None = None
) -> HandOff:
    """A hand-off offer naming the approver ('Rosa')."""
    return HandOff(
        reason=reason,  # type: ignore[arg-type] - validated by the model
        approver_spoken_name=profile.approver_spoken_name(module_id),
        suggested_question=speech.truncate(" ".join(question.split()), 300) or "Can you help with my question?",
        fact_ref=fact_ref,
    )


def offer_sentence(profile: Profile, module_id: str | None = None) -> str:
    """'Want me to ask Rosa?' (or a pointer to contact the business when nobody answers questions)."""
    if can_ask_owner(profile, module_id):
        return f"Want me to ask {profile.approver_spoken_name(module_id)}?"
    return f"{profile.spoken_name} can tell you directly."


def handoff_card(profile: Profile, hand: HandOff | None, *, title: str) -> Card:
    actions: list[CardAction] = []
    if hand is not None:
        arguments: dict[str, Any] = {"business_id": profile.profile_id, "question": hand.suggested_question}
        if hand.fact_ref:
            arguments["fact_ref"] = hand.fact_ref
        actions.append(CardAction(label=f"Ask {hand.approver_spoken_name}", tool="ask_owner", arguments=arguments))
    return Card(kind="handoff", title=title, subtitle=profile.name, actions=actions)


def unknown_answer(
    profile: Profile,
    *,
    question: str,
    fact_ref: str | None = None,
    reason: str = "unknown",
    lead: str = "I don't know that.",
    module_id: str | None = None,
    result_type: type[G] = Grounded,  # type: ignore[assignment]
    **fields: Any,
) -> G:
    """'I don't know that. Want me to ask Rosa?' plus the hand-off; used for absent facts.

    `result_type` and `fields` build the calling tool's own result model (e.g. `PolicyResult`).
    """
    hand = handoff(profile, reason=reason, question=question, fact_ref=fact_ref, module_id=module_id)
    askable = can_ask_owner(profile, module_id)
    return result_type(
        speak=speech.sentences(lead, tail=offer_sentence(profile, module_id)),
        facts=[],
        card=handoff_card(profile, hand if askable else None, title="Not in the business's published info"),
        handoff=hand if askable else None,
        business_id=profile.profile_id,
        profile_revision=profile.revision,
        **fields,
    )


def withheld_answer(
    profile: Profile,
    *,
    question: str,
    fact_ref: str,
    question_id: str | None,
    result_type: type[G] = Grounded,  # type: ignore[assignment]
    **fields: Any,
) -> G:
    """For disputed facts (open contradiction): 'Rosa is double-checking that. Want me to ask her?'.

    Never mentions either candidate value. `question_id` is deliberately not spoken or returned (it is
    owner-facing); it is accepted so callers can trace which question withheld the fact.
    """
    del question_id  # owner-facing only
    approver = profile.approver_spoken_name()
    hand = handoff(profile, reason="disputed", question=question, fact_ref=fact_ref)
    askable = can_ask_owner(profile)
    return result_type(
        speak=speech.sentences(
            f"{approver} is double-checking that, so I can't say for sure yet.", tail=offer_sentence(profile)
        ),
        facts=[],
        card=handoff_card(profile, hand if askable else None, title="Being double-checked"),
        handoff=hand if askable else None,
        business_id=profile.profile_id,
        profile_revision=profile.revision,
        **fields,
    )


def notice_card(title: str, lines: list[tuple[str, str]] | None = None) -> Card:
    """A simple text card."""
    return Card(kind="notice", title=title, lines=[CardLine(label=label, value=value) for label, value in lines or []])
