"""What the item phrase says about option choices, for the transactional tools (order, book, quote).

Customers name choices inside the item: "a half sheet cake", "carrot cake", "a ten inch round vanilla
bean cake with lemon curd", "balayage for long hair", "a vegan cake". `matching.resolve` already reads
that (`ItemLookup.focus`); this module turns it into the `choices` the domain plan takes, plus the
limits the next question must respect:

  * a group the phrase names with one choice, and the caller left open, is filled from the phrase;
    a choice passed explicitly in `choices` always wins (it is the more deliberate answer);
  * a dietary need ("gluten-free", "vegan", or one given in a diet intake field) is a safety
    constraint on every option group. Each choice is classified from owner-statable facts only:
      - certified: its dietary tags carry the need;
      - conflict: its owner-confirmed allergens contain one that rules the need out (wheat for
        gluten-free, milk or eggs for vegan, ...);
      - clear: its allergens are known and contain none of those (only for needs that are fully
        decided by the nine major allergens, e.g. dairy-free; for vegan it means "without milk or
        eggs", and gluten-free is never inferred from the absence of wheat, per the schema);
      - unknown otherwise.
    A group with a certified or a conflicting choice is constrained to its certified (and clear)
    choices. An explicit choice that conflicts is never taken silently: when the customer's own words
    for it fit exactly one allowed choice ("chocolate" -> "gluten-free dark chocolate") that one is
    used (the read-back names it, so the customer hears it before confirming); otherwise it is dropped,
    a note says why ("Lemon curd has milk and eggs, so it isn't vegan."), and the question lists only
    the allowed choices. An open group with exactly one certified choice is filled with it.
  * a head count ("cake for 30") or several named choices only narrow the question ("Which size for 30
    people: ..."); they never override what the customer chose.

`apply` records the limits and notes on the `CommitPlan` (`choice_focus`, `choice_notes`), which
`render.problem_result` speaks when it asks for a choice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from dataclasses import dataclass, field
from typing import Any

from askonce_server.domain.planning import ChoiceFocus, CommitPlan
from askonce_server.domain.services.base import resolve_choices
from askonce_server.profile.model import JsonObj, Profile
from askonce_server.tools import grounding, search, speech
from askonce_server.tools.catalog import resolve_item
from askonce_server.tools.matching import ItemLookup, terms, tokens

RULED_OUT_BY: dict[str, frozenset[str]] = {
    "gluten_free": frozenset({"wheat"}),
    "vegan": frozenset({"milk", "eggs", "fish", "shellfish"}),
    "vegetarian": frozenset({"fish", "shellfish"}),
    "dairy_free": frozenset({"milk"}),
    "egg_free": frozenset({"eggs"}),
    "nut_free": frozenset({"tree_nuts", "peanuts"}),
    "peanut_free": frozenset({"peanuts"}),
    "soy_free": frozenset({"soybeans"}),
}
"""Owner-confirmed allergens that make a choice incompatible with a dietary need."""

CLEAR_MEANS: dict[str, str] = {
    "dairy_free": "dairy-free",
    "egg_free": "egg-free",
    "nut_free": "without nuts",
    "peanut_free": "without peanuts",
    "soy_free": "without soy",
    "vegan": "without milk or eggs",
}
"""Needs whose allergens-only reading is meaningful, and how such a choice is described. The other
needs (gluten-free, vegetarian, halal, kosher, sugar-free) need the owner's dietary tag."""

DIET_FIELD_HINTS = ("diet", "allerg")
"""Intake keys whose free text may state a dietary need ("dietary_needs": "vegan")."""

HEAD_COUNT_FIELD = re.compile(r"serving|guest|people|person|party|head ?count|attendee", re.IGNORECASE)
"""A business's integer intake field that counts people ("servings", "guests", "party size")."""




@dataclass(frozen=True)
class ItemRequest:
    """The item as the domain should see it, the choices to plan with, and the question limits."""

    item: str
    choices: dict[str, Any]
    intake: dict[str, Any] = field(default_factory=dict)
    """The caller's details under the business's own field names ("phone_number" -> "customer_phone",
    "party_size" -> "servings"), plus a head count said only in the item phrase ("for 30 people")."""
    focus: tuple[ChoiceFocus, ...] = ()
    notes: tuple[str, ...] = ()
    note_refs: tuple[str, ...] = ()
    lookup: ItemLookup | None = field(default=None, compare=False)


def _needs_from_intake(intake: Mapping[str, Any]) -> list[str]:
    tags: list[str] = []
    for key, value in intake.items():
        if not isinstance(value, str) or not any(h in str(key).lower() for h in DIET_FIELD_HINTS):
            continue
        text = search.normalize(value)
        for tag, phrases in search.DIETARY_PHRASES.items():
            if any(search.contains_phrase(text, search.normalize(p)) for p in phrases) and tag not in tags:
                tags.append(tag)
    return tags


_WORD_SPLIT = re.compile(r"[^a-z0-9]+")
_PHONE_WORDS = frozenset({"phone", "mobile", "cell", "tel", "telephone", "contact"})
_EMAIL_WORDS = frozenset({"email", "mail"})
_NAME_WORDS = frozenset({"name", "names"})


def _key_words(text: str) -> frozenset[str]:
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text).lower()
    return frozenset(w.rstrip("s") if len(w) > 3 else w for w in _WORD_SPLIT.split(spaced) if w)


def _field_for(key: str, value: Any, fields: Sequence[JsonObj]) -> str | None:
    """The business's intake field a host meant by `key` ("phone_number" -> "customer_phone",
    "party_size" -> "servings"), when exactly one field fits; None otherwise."""
    words = _key_words(key)

    def unique(found: list[JsonObj]) -> str | None:
        return str(found[0]["key"]) if len(found) == 1 else None

    if words & _EMAIL_WORDS:
        return unique([f for f in fields if f.get("type") == "email"])
    if words & _PHONE_WORDS:
        return unique([f for f in fields if f.get("type") == "phone"])
    if HEAD_COUNT_FIELD.search(key.replace("_", " ")) or words & {"count", "size"} and words & {"party", "group"}:
        return unique(
            [
                f
                for f in fields
                if f.get("type") == "integer" and HEAD_COUNT_FIELD.search(f"{f.get('key', '')} {f.get('label', '')}")
            ]
        )
    if words & _NAME_WORDS:
        return unique([f for f in fields if f.get("type") == "text" and "name" in _key_words(f"{f.get('key', '')} {f.get('label', '')}")])
    scored = [
        (len(words & _key_words(f"{f.get('key', '')} {f.get('label', '')}")), f) for f in fields
    ]
    best = max((n for n, _f in scored), default=0)
    return unique([f for n, f in scored if n == best]) if best else None


def _with_field_names(
    profile: Profile, module_id: str | None, intake: Mapping[str, Any], phrase_count: int | None, group_ids: set[str]
) -> dict[str, Any]:
    """`intake` with each detail a host sent under its own name moved to the business's field, and the
    head-count field filled from the item phrase ("a cake for 30 people") when nothing else gave it.
    A field the caller already filled is never overwritten; option-group keys are left alone."""
    out = dict(intake)
    if module_id is None:
        return out
    try:
        fields = list(profile.intake_fields(module_id))
    except Exception:  # noqa: BLE001 - unknown module: nothing to map
        return out
    known = {str(f["key"]) for f in fields}
    for key in list(out):
        if key in known or key in group_ids or out[key] in (None, ""):
            continue
        target = _field_for(key, out[key], fields)
        if target is not None and out.get(target) in (None, ""):
            out[target] = out.pop(key)
    head = next(
        (
            str(f["key"])
            for f in fields
            if f.get("type") == "integer" and HEAD_COUNT_FIELD.search(f"{f.get('key', '')} {f.get('label', '')}")
        ),
        None,
    )
    if head is not None and out.get(head) in (None, "") and phrase_count is not None:
        out[head] = phrase_count
    return out


def _state(profile: Profile, ref: str) -> Any | None:
    got = grounding.state(profile, ref)
    return got.value if got.ok else None


@dataclass(frozen=True)
class _Verdict:
    kind: str  # certified | conflict | clear | unknown
    allergens: tuple[str, ...] = ()
    """For `conflict`: the ruling-out allergens it contains."""
    refs: tuple[str, ...] = ()


def _verdict(profile: Profile, ref: str, need: str) -> _Verdict:
    tags = _state(profile, f"{ref}#dietary.tags")
    if tags is not None and need in {str(t) for t in tags}:
        return _Verdict("certified", refs=(f"{ref}#dietary.tags",))
    contains = _state(profile, f"{ref}#allergens.contains")
    if contains is None:
        return _Verdict("unknown")
    present = tuple(a for a in (str(x) for x in contains) if a in RULED_OUT_BY.get(need, frozenset()))
    if present:
        return _Verdict("conflict", allergens=present, refs=(f"{ref}#allergens.contains",))
    if need in CLEAR_MEANS:
        return _Verdict("clear", refs=(f"{ref}#allergens.contains",))
    return _Verdict("unknown")


def _groups(item: JsonObj) -> list[JsonObj]:
    return list(item.get("options", ()) or ())


def _words(text: str) -> frozenset[str]:
    return frozenset(tokens(text)) - {"cake", "a", "the"}


def _fits(choice: JsonObj, said: str) -> bool:
    """The customer's words for a choice all appear in one of `choice`'s terms."""
    wanted = _words(said)
    return bool(wanted) and any(wanted <= frozenset(tokens(term)) for term in terms(choice))


def _choice_name(choice: JsonObj) -> str:
    return str(choice.get("spoken_name") or choice.get("name") or choice["id"])


def _explicit(
    ref: str, item: JsonObj, choices: Mapping[str, Any]
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """For explicit choices: ({group id: resolved choice id}, {group id: the text the caller gave},
    {group id: the caller's key}). A key may name the group or not ({"sponge": "gluten-free vanilla"})."""
    resolved: dict[str, str] = {}
    said: dict[str, str] = {}
    keys: dict[str, str] = {}
    for key, value in choices.items():
        one, _problems = resolve_choices(ref, item, {key: value})
        for gid, cid in one.items():
            resolved[gid] = cid
            said[gid] = value if isinstance(value, str) else str(value)
            keys[gid] = key
    return resolved, said, keys


def request_for(
    profile: Profile,
    text: str,
    module_type: str,
    choices: Mapping[str, Any] | None,
    intake: Mapping[str, Any] | None = None,
) -> ItemRequest:
    """See module docstring. `text` is the item as the customer said it."""
    given = dict(choices or {})
    lookup = resolve_item(profile, text, module_type=module_type)
    entry = lookup.found
    if entry is None:
        return ItemRequest(item=text, choices=given, intake=dict(intake or {}), lookup=lookup)
    modules = profile.transactional_modules_for(entry.ref)
    typed = [m for m in modules if profile.module_type(m) == module_type]
    group_ids = {str(g["id"]) for g in _groups(entry.item)}
    details = _with_field_names(profile, typed[0] if typed else None, intake or {}, lookup.servings, group_ids)
    ref, item = entry.ref, entry.item
    needs = list(dict.fromkeys([*lookup.dietary, *_needs_from_intake(details)]))
    item_tags = {str(t) for t in (_state(profile, f"{ref}#dietary.tags") or ())}
    needs = [n for n in needs if n not in item_tags]
    explicit, said, keys = _explicit(ref, item, given)
    out = dict(given)
    focus: list[ChoiceFocus] = []
    notes: list[str] = []
    note_refs: list[str] = []
    phrase_focus = {f.group_id: f for f in lookup.focus}

    for group in _groups(item):
        gid = str(group["id"])
        choice_list: Sequence[JsonObj] = group.get("choices") or ()
        allowed: list[str] | None = None
        label: str | None = None
        for need in needs:
            verdicts = {str(c["id"]): _verdict(profile, f"{ref}/{gid}/{c['id']}", need) for c in choice_list}
            kinds = {v.kind for v in verdicts.values()}
            if not kinds & {"certified", "conflict"}:
                continue  # nothing the profile says constrains this group for this need
            ok = [cid for cid, v in verdicts.items() if v.kind in ("certified", "clear")]
            allowed = ok if allowed is None else [c for c in allowed if c in ok]
            all_certified = all(verdicts[c].kind == "certified" for c in ok)
            this_label = speech.dietary(need) if all_certified else CLEAR_MEANS.get(need, speech.dietary(need))
            label = this_label if label is None else f"{label} and {this_label}"
            chosen = explicit.get(gid)
            if chosen is not None and verdicts.get(chosen, _Verdict("unknown")).kind == "conflict":
                verdict = verdicts[chosen]
                fitting = [c for c in choice_list if str(c["id"]) in ok and _fits(c, said[gid])]
                if len(fitting) == 1:
                    explicit[gid] = str(fitting[0]["id"])
                    out.pop(keys[gid], None)
                    out[gid] = str(fitting[0]["id"])
                else:
                    bad = next(c for c in choice_list if str(c["id"]) == chosen)
                    notes.append(
                        f"{speech.capitalize(_choice_name(bad))} has "
                        f"{speech.join([speech.allergen(a) for a in verdict.allergens])}, "
                        f"so it isn't {speech.dietary(need)}."
                    )
                    note_refs.extend(verdict.refs)
                    explicit.pop(gid, None)
                    out.pop(keys[gid], None)
        if allowed is not None:
            certified = [c for c in allowed if all(_verdict(profile, f"{ref}/{gid}/{c}", n).kind == "certified" for n in needs)]
            if gid not in explicit and len(allowed) == 1 and certified == allowed:
                out[gid] = allowed[0]
                explicit[gid] = allowed[0]
            elif gid not in explicit and allowed:
                named = phrase_focus.get(gid)
                narrowed = [c for c in allowed if named is not None and c in named.choice_ids]
                if len(narrowed) == 1:
                    out[gid] = narrowed[0]
                    explicit[gid] = narrowed[0]
                else:
                    focus.append(ChoiceFocus(group_id=gid, choice_ids=tuple(narrowed or allowed), reason="dietary", label=label, hard=True))
            continue
        named = phrase_focus.get(gid)
        if named is None or gid in explicit:
            continue
        if len(named.choice_ids) == 1:
            out[gid] = named.choice_ids[0]
        else:
            servings = f"for {lookup.servings} people" if named.reason == "servings" and lookup.servings else None
            focus.append(ChoiceFocus(group_id=gid, choice_ids=named.choice_ids, reason=named.reason, label=servings, hard=False))
    return ItemRequest(
        item=ref, choices=out, intake=details, focus=tuple(focus), notes=tuple(notes), note_refs=tuple(dict.fromkeys(note_refs)), lookup=lookup
    )


def apply(plan: CommitPlan, request: ItemRequest) -> CommitPlan:
    """Record the request's question limits and notes on the plan (render speaks them)."""
    if not request.focus and not request.notes:
        return plan
    return plan.model_copy(
        update={
            "choice_focus": request.focus,
            "choice_notes": request.notes,
            "choice_note_refs": request.note_refs,
            "fact_refs": tuple(dict.fromkeys([*plan.fact_refs, *request.note_refs])),
        }
    )
