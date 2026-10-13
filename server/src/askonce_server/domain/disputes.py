"""The statable rule (L6) in the transactional paths: nothing is booked, ordered or refused on the
strength of a disputed fact.

Read-only tools withhold a disputed fact and offer to ask the owner. Plans must do the same for the
facts they *act* on: the opening hours and closures that decide whether a slot exists, the prices read
back and stored, the service area that decides "we don't go there". A disputed one becomes the plan
problem `disputed_fact` (rendered as "Rosa is double-checking some details" plus a hand-off).

Only disputes count here (an open `contradiction` / `confirm` question whose targets overlap the ref,
`profile.statable.disputed_by`): it applies whether or not the fact is present, so "absent, but a
question about it is open" withholds too.
"""

from __future__ import annotations

from collections.abc import Iterable

from askonce_server.domain.planning import PlanProblem
from askonce_server.profile import statable
from askonce_server.profile.factref import FactRef, FactRefError
from askonce_server.profile.model import Profile

DISPUTED_DETAIL = "the business is double-checking a detail this depends on"


def first_disputed(profile: Profile, refs: Iterable[str | None]) -> tuple[str, str] | None:
    """`(ref, question id)` of the first ref an open contradiction/confirm question disputes, else None.

    Malformed refs are skipped (the loader rejects them; an answer must never crash on one).
    """
    seen: set[str] = set()
    for ref in refs:
        if not ref or ref in seen:
            continue
        seen.add(ref)
        try:
            parsed = FactRef.parse(ref)
        except FactRefError:
            continue
        question_id = statable.disputed_by(profile, parsed)
        if question_id is not None:
            return ref, question_id
    return None


def disputed_problem(profile: Profile, refs: Iterable[str | None], *, field: str | None = None) -> PlanProblem | None:
    """A `disputed_fact` problem for the first disputed ref in `refs`, else None."""
    found = first_disputed(profile, refs)
    if found is None:
        return None
    return PlanProblem(code="disputed_fact", detail=DISPUTED_DETAIL, field=field, fact_ref=found[0])
