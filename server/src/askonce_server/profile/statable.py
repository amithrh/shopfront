"""The statable rule (invariant L6): may the assistant say this fact?

A fact may be stated only if all of these hold:
  * it is present (absent = unknown -> "I don't know. Want me to ask <approver>?");
  * it has provenance (L5) and does not sit under an `x-*` key or module;
  * no `open` question of kind `contradiction` or `confirm` targets an overlapping fact ref
    (disputed facts are withheld until the owner answers);
  * safety facts (`allergens`, `dietary`, `facility_may_contain`) are owner-confirmed. The schema
    already forbids unconfirmed ones; this re-check is defence in depth. Vocabulary attributes with
    `claim_class: safety` (e.g. `nut_free_kitchen`) are held to the same rule.

`missing` questions do not withhold a present fact (the fact is normally absent anyway).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from askonce_server.profile.factref import FactRef, FactRefError, ResolvedFact
from askonce_server.profile.model import Profile

SAFETY_FIELDS: frozenset[str] = frozenset({"allergens", "dietary", "facility_may_contain"})
DISPUTING_KINDS: frozenset[str] = frozenset({"contradiction", "confirm"})
"""Open questions of these kinds withhold every overlapping fact."""

StatabilityReason = Literal["ok", "absent", "disputed", "unconfirmed_safety", "no_provenance", "extension"]


@dataclass(frozen=True)
class Statability:
    statable: bool
    reason: StatabilityReason
    question_id: str | None = None
    """For `disputed`: the open question that withholds the fact (shown to the owner, never spoken)."""


OK = Statability(statable=True, reason="ok")


def disputed_by(profile: Profile, ref: FactRef) -> str | None:
    """Id of an open contradiction/confirm question whose targets overlap `ref`, else None."""
    for question in profile.open_questions():
        if question.get("kind") not in DISPUTING_KINDS:
            continue
        for target in question.get("targets") or ():
            try:
                target_ref = FactRef.parse(target)
            except FactRefError:
                continue  # the loader (L1) rejects these; never let one crash an answer
            if target_ref.overlaps(ref):
                return str(question.get("id"))
    return None


def statability(profile: Profile, fact: ResolvedFact) -> Statability:
    """Apply the rule above to an already-resolved fact."""
    if not fact.present:
        return Statability(statable=False, reason="absent")
    if fact.in_extension:
        return Statability(statable=False, reason="extension")
    if fact.provenance is None:
        return Statability(statable=False, reason="no_provenance")
    question_id = disputed_by(profile, fact.ref)
    if question_id is not None:
        return Statability(statable=False, reason="disputed", question_id=question_id)
    if is_safety_fact(profile, fact) and not fact.owner_confirmed:
        return Statability(statable=False, reason="unconfirmed_safety")
    return OK


def is_safety_field(ref: FactRef) -> bool:
    """True for allergen/dietary/cross-contact facts."""
    return ref.field in SAFETY_FIELDS


def is_safety_fact(profile: Profile, fact: ResolvedFact) -> bool:
    """`is_safety_field`, or an `attributes`/`facts` value whose vocabulary `claim_class` is `safety`."""
    ref = fact.ref
    if is_safety_field(ref):
        return True
    if ref.field not in ("attributes", "facts"):
        return False
    if ref.subfields:
        keys: list[str] = [ref.subfields[0]]
    elif isinstance(fact.value, Mapping):
        keys = [str(k) for k in fact.value]
    else:
        keys = []
    for key in keys:
        definition = profile.vocabulary_attribute(key)
        if definition is not None and definition.get("claim_class") == "safety":
            return True
    return False
