"""Catalog tools: get_catalog (browse) and find_item (details incl. owner-confirmed allergens).

Item resolution (`resolve_item`, implemented in `tools.matching`) is shared with the transactional
tools: an item ref or bare id, an exact name / spoken name / synonym, then a scored match that reads
the customer's modifiers ("gluten-free cake", "a cake for 30", "a 10 inch cake", "a trim", "fix a
leaky faucet"). Several equally good matches are an ambiguity (the reply lists them) rather than a
guess, and a dietary need no candidate is confirmed to meet is a hand-off ("I don't know whether the
custom cake comes nut-free"), never the plain item. Transactional tools pass `module_type`, so only
items that can be booked / ordered / quoted that way compete ("cake" at the bakery is the custom cake
for an order, not the slice of cake), falling back to the whole catalog.

The option choices the words point at (the gluten-free flavours, the sizes that serve 30, the carrot
flavour) are the answer's focus: find_item leads with them ("The custom cake has gluten-free cake
flavors for $12 more per tier"), speaks their owner-confirmed allergens instead of every choice's,
cites their facts and returns them as `matched_options`.

Safety facts follow the statable rule: allergens and dietary tags are only spoken when the owner
confirmed them, and every allergen answer carries the catalog's kitchen-wide `facility_may_contain`
statement (or a hand-off when that statement is missing or withheld). Anything absent becomes a
hand-off, never "no allergens". Allergens that live on option choices ("choices replace item values")
are spoken per choice. Safety sentences are spoken first, before hand-offs and everything else, so a
long answer never truncates them away.

Customer words are never spoken back as if the business said them: "I don't see that at Thimbleberry
Bakehouse", not "I don't see <whatever was asked>". The words stay in the hand-off question only.
"""

from dataclasses import dataclass, field
from typing import Annotated, Literal, TypeVar

from pydantic import Field

from mcp.server.mcpserver import Context
from askonce_server.profile.model import JsonObj, Profile
from askonce_server.tools import grounding, knowledge, matching, render, search, speech
from askonce_server.tools.context import finish, get_deps, live_profile, tool_span
from askonce_server.tools.discovery import BusinessId
from askonce_server.tools.matching import MAX_CANDIDATES, CatalogItem, Focus, ItemLookup
from askonce_server.tools.schemas import (
    Card,
    CardAction,
    CardLine,
    CatalogResult,
    Fact,
    Grounded,
    HandOff,
    ItemDetailResult,
    ItemSummary,
    OptionChoiceView,
    OptionGroupView,
)

G = TypeVar("G", bound=Grounded)

Aspect = Literal["price", "options", "allergens", "dietary", "duration", "availability", "description"]
DEFAULT_ASPECTS: tuple[Aspect, ...] = ("price", "options")

_AVAILABILITY_SPEECH = {
    "available": "is available",
    "limited": "is available in limited numbers",
    "seasonal": "is seasonal",
    "made_to_order": "is made to order",
    "sold_out": "is sold out right now",
}


# ---------------------------------------------------------------------------- item resolution


def item_terms(item: JsonObj) -> list[str]:
    return matching.terms(item)


def catalog_entries(profile: Profile) -> list[CatalogItem]:
    return [CatalogItem(module_id, profile.item_ref(module_id, item), item) for module_id, item in profile.catalog_items()]


TRANSACTIONAL = "transactional"
"""`module_type` for `resolve_item`: any booking, custom_order or quote_request module."""


def _accepted_by(profile: Profile, entry: CatalogItem, module_type: str) -> bool:
    wanted = None if module_type == TRANSACTIONAL else module_type
    return bool(profile.transactional_modules_for(entry.ref, wanted))  # type: ignore[arg-type]


def resolve_item(profile: Profile, text: str, *, module_type: str | None = None) -> ItemLookup:
    """Resolve what the customer said to one catalog item (see `tools.matching`).

    With `module_type` (`booking`, `custom_order`, `quote_request` or `TRANSACTIONAL`), items that
    kind of module accepts are matched first; the whole catalog is the fallback, so an item that
    exists but cannot be booked still resolves (and the caller says so).
    """
    entries = catalog_entries(profile)
    lookup: ItemLookup | None = None
    if module_type is not None:
        accepted = [e for e in entries if _accepted_by(profile, e, module_type)]
        lookup = matching.resolve(profile, accepted, text)
        if lookup.found is None and not lookup.candidates:
            lookup = None
    if lookup is None:
        lookup = matching.resolve(profile, entries, text)
    if lookup.unexplained and declined_by_business(profile, text, lookup.unexplained):
        # "replace my sewer line": the business says it doesn't do that, so it is not the drain cleaning.
        return ItemLookup(found=None, dietary=lookup.dietary, servings=lookup.servings)
    return lookup


NOT_OFFERED_TOPIC = "not_offered"
"""Knowledge `topic` (or entry id `not-offered`) of the business's list of jobs it does not take."""


def declined_by_business(profile: Profile, text: str, unexplained: tuple[str, ...]) -> bool:
    """True when the business's not-offered statement is what `text` asks about (the policy matcher's
    best entry for it) and names a word the closest catalog item could not explain ("replace" in
    "replace my sewer line", when the item is drain cleaning)."""
    match = knowledge.best_entry(profile, text, None)
    if match is None:
        return False
    _module_id, entry, _score = match
    if entry.get("topic") != NOT_OFFERED_TOPIC and entry.get("id") != "not-offered":
        return False
    units = knowledge.place_units(profile)
    left = knowledge.tokens(" ".join(unexplained), units)
    return bool(left & (knowledge.entry_tokens(entry, units) | knowledge.answer_tokens(entry, units)))


def commit_item(profile: Profile, text: str, module_type: str) -> str:
    """What a transactional tool hands its domain service: the item ref when `text` resolves to one
    item (partial phrases and modifiers included, so "haircut" or "gluten-free cake" plans like
    `check_availability` resolves it), else the text unchanged (the plan then reports the unknown
    item, and `commit_flow` turns several candidates into "Did you mean ...?" and a dietary need no
    candidate is confirmed to meet into a hand-off)."""
    lookup = resolve_item(profile, text, module_type=module_type)
    return lookup.found.ref if lookup.found is not None else text


def unmet_dietary_answer(profile: Profile, lookup: ItemLookup) -> tuple[str, list[Fact]]:
    """'I don't know whether the custom cake comes nut-free.' for a lookup whose best candidates cannot
    be confirmed to meet a dietary need (the statable rule: an absent dietary tag is unknown, never
    "no"). The facts are the candidates' names. The caller adds the hand-off offer."""
    names = [c.spoken_name for c in lookup.candidates]
    label = speech.join([speech.dietary(t) for t in lookup.unmet_dietary])
    sentence = f"I don't know whether the {speech.join(names, conjunction='or')} comes {label}"
    return sentence, grounding.facts_for(profile, [f"{c.ref}#spoken_name" for c in lookup.candidates])


def _category_ids(profile: Profile, module_id: str, wanted: str) -> set[str]:
    query = search.normalize(wanted)
    try:
        categories = profile.module(module_id).get("categories") or ()
    except Exception:  # noqa: BLE001 - unknown module: no categories
        return set()
    out: set[str] = set()
    for category in categories:
        names = [category.get("id"), category.get("name"), category.get("spoken_name"), *(category.get("synonyms") or ())]
        for name in names:
            if name and (search.normalize(str(name)) == query or search.contains_phrase(search.normalize(str(name)), query)):
                out.add(str(category["id"]))
    return out


def _category_name(profile: Profile, module_id: str, category_id: str) -> str:
    try:
        for category in profile.module(module_id).get("categories") or ():
            if category.get("id") == category_id:
                return str(category.get("spoken_name") or category.get("name") or category_id)
    except Exception:  # noqa: BLE001
        pass
    return category_id.replace("-", " ")


def summary_of(profile: Profile, entry: CatalogItem) -> tuple[ItemSummary, list[Fact]]:
    """The item's card/list summary plus the facts behind what it states (price only if statable)."""
    facts: list[Fact] = []
    name = grounding.state(profile, f"{entry.ref}#spoken_name")
    if name.fact:
        facts.append(name.fact)
    price = grounding.state(profile, f"{entry.ref}#price")
    if price.fact:
        facts.append(price.fact)
    availability = (entry.item.get("availability") or {}).get("status")
    return (
        ItemSummary(
            item_ref=entry.ref,
            name=entry.name,
            spoken_name=entry.spoken_name,
            price=render.price_view(price.value) if price.ok else None,
            availability=str(availability) if availability else None,
            category_ids=[str(c) for c in entry.item.get("category_ids") or ()],
        ),
        facts,
    )


# ---------------------------------------------------------------------------- get_catalog


async def get_catalog(
    business_id: BusinessId,
    ctx: Context,
    category: Annotated[str | None, Field(description="Category id or name, e.g. cakes.", max_length=60)] = None,
    query: Annotated[str | None, Field(description="Words to match item names and synonyms.", max_length=120)] = None,
    limit: Annotated[int, Field(ge=1, le=25)] = 10,
) -> CatalogResult:
    """List what a business sells or offers, with prices exactly as published."""
    deps = get_deps(ctx)
    with tool_span("get_catalog", business_id):
        profile = await live_profile(deps, business_id)
        entries = catalog_entries(profile)
        label = _collective_noun(profile, entries)
        generic = label
        if category:
            wanted: set[str] = set()
            for module_id in {e.module_id for e in entries}:
                wanted |= _category_ids(profile, module_id, category)
            entries = [e for e in entries if wanted & set(e.item.get("category_ids") or ())]
            if len(wanted) == 1 and entries:
                label = _category_name(profile, entries[0].module_id, next(iter(wanted)))
        if query:
            entries = matching.matching(profile, entries, query)
            label = f"matching {generic}" if label == generic else label
        if not entries:
            asked = " ".join(p for p in (query, category) if p) or "that"
            result = grounding.unknown_answer(
                profile,
                question=f"Do you have {asked}?",
                reason="not_offered",
                lead=f"I couldn't find that at {profile.spoken_name}.",
                result_type=CatalogResult,
                total=0,
            )
            return finish(result, profile, outcome="not_offered")
        total = len(entries)
        shown = entries[:limit]
        items: list[ItemSummary] = []
        facts: list[Fact] = []
        for entry in shown:
            summary, item_facts = summary_of(profile, entry)
            items.append(summary)
            facts += item_facts
        speak = _list_sentence(profile.spoken_name, total, label, items)
        card = Card(
            kind="items",
            title=profile.name,
            subtitle=speech.capitalize(label) if label != generic else None,
            lines=[CardLine(label=i.name, value=(speech.price(i.price) or "Ask for a quote") if i.price else "Ask for price") for i in items],
            badges=[f"{total} {label}"] if total > 1 else [],
            citations=grounding.citations_for(profile, facts),
            actions=[
                CardAction(label=f"About {i.name}", tool="find_item", arguments={"business_id": business_id, "item": i.item_ref})
                for i in items[:3]
            ],
        )
        result = CatalogResult(speak=speak, facts=grounding.dedupe_facts(facts), card=card, items=items, total=total)
        return finish(result, profile, outcome="ok")


def _collective_noun(profile: Profile, entries: list[CatalogItem]) -> str:
    """'services' when what is offered is booked or quoted (a salon, a plumber), else 'items'."""
    kinds = {
        profile.module_type(module_id)
        for entry in entries
        for module_id in profile.transactional_modules_for(entry.ref)
    }
    return "services" if kinds and kinds <= {"booking", "quote_request"} else "items"


def priced_phrase(name: str, price: object) -> str:
    """'country sourdough for $9', 'leak repair from $150', 'drain cleaning for $80 to $120', or the
    bare name when there is no spoken amount (free, quote only)."""
    spoken = speech.price(price) if price is not None else None
    if not spoken or spoken == "free":
        return name
    if spoken.startswith("from "):
        return f"{name} {spoken}"
    return f"{name} for {spoken}"


def _list_sentence(business: str, total: int, label: str, items: list[ItemSummary]) -> str:
    """'Thimbleberry Bakehouse has 6 breads. They include country sourdough for $9 and seeded rye for $10.'

    Lists as many items as fit in the speech limit (prices only when statable and spoken as amounts;
    a `from` price is joined without "for": 'leak repair from $150').
    """
    phrases = [priced_phrase(summary.spoken_name, summary.price) for summary in items]
    if total == 1:
        return speech.sentences(f"{business} has {phrases[0]}")
    lead = speech.sentences(f"{business} has {total} {label}")
    best = lead
    for count in range(1, len(phrases) + 1):
        listing = speech.sentences(f"They include {speech.join(phrases[:count])}", limit=10_000)
        candidate = f"{lead} {listing}"
        if len(candidate) > speech.SPEAK_LIMIT:
            break
        best = candidate
    return best


# ---------------------------------------------------------------------------- find_item


@dataclass
class _Answer:
    """Sentences of a find_item reply, in speaking order: `lead` (what the customer's words pointed at,
    e.g. the gluten-free flavours of the custom cake), `safety` (allergens, cross-contact and their
    hand-offs: never truncated away), then `priority` (other hand-offs), then `sentences`."""

    lead: list[tuple[str, ...]] = field(default_factory=list)
    """Each lead sentence with its shorter fallbacks (most informative first)."""
    safety: list[str] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)
    priority: list[str] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    card_lines: list[CardLine] = field(default_factory=list)
    handoff: HandOff | None = None

    def hand_off(
        self, profile: Profile, *, reason: str, question: str, fact_ref: str | None, sentence: str, safety: bool = False
    ) -> None:
        (self.safety if safety else self.priority).append(sentence)
        if self.handoff is None and grounding.can_ask_owner(profile):
            self.handoff = grounding.handoff(profile, reason=reason, question=question, fact_ref=fact_ref)

    def speak(self, tail: str | None) -> str:
        """Everything that fits, lead and safety first. If the safety sentences do not all fit, the lead
        sentences shrink to their shortest forms, then the other sentences go, then all but the first
        lead sentence, and only then the lead itself."""
        longest = [forms[0] for forms in self.lead]
        shortest = [forms[-1] for forms in self.lead]
        attempts = (
            (*longest, *self.safety, *self.priority, *self.sentences),
            (*shortest, *self.safety, *self.priority, *self.sentences),
            (*shortest[:1], *self.safety),
            tuple(self.safety),
        )
        for parts in attempts:
            spoken = speech.sentences(*parts, tail=tail)
            if all(speech.clean(s).rstrip(".!?") in spoken for s in self.safety):
                return spoken
        return speech.sentences(*self.safety, tail=tail)


def _withheld_or_unknown(
    profile: Profile, answer: _Answer, got: grounding.Stated, *, what: str, question: str, safety: bool = False
) -> None:
    if grounding.is_disputed(profile, got):
        answer.hand_off(
            profile,
            reason="disputed",
            question=question,
            fact_ref=got.text,
            sentence=f"{profile.approver_spoken_name()} is double-checking {what}, so I can't say for sure yet",
            safety=safety,
        )
    else:
        answer.hand_off(
            profile, reason="unknown", question=question, fact_ref=got.text, sentence=f"I don't know {what}", safety=safety
        )


MAX_SPOKEN_CHOICES = 3
"""Option choices spoken by name in find_item; more are summarised ('and 4 more')."""

LEAD_LIMIT = 110
"""Longest lead sentence; longer forms fall back to shorter ones so the safety sentences still fit."""


def _choice_phrase(choice: OptionChoiceView) -> str:
    """'six inch round for $56', 'gluten-free vanilla bean for $12 more per tier', 'core area at no
    charge', or just the name."""
    if choice.price is not None:
        if speech.price(choice.price) == "free":
            return f"{choice.spoken_name} at no charge"  # never dropped: "core area or extended area for $35" misleads
        return priced_phrase(choice.spoken_name, choice.price)
    delta = speech.price_delta(choice.price_delta)
    return f"{choice.spoken_name} for {delta}" if delta else choice.spoken_name


def _price_suffix(choice: OptionChoiceView) -> str | None:
    """' for $12 more per tier' / ' for $120', or None when the choice has no spoken amount."""
    if choice.price is not None:
        spoken = speech.price(choice.price)
        return f" for {spoken}" if spoken and spoken != "free" and not spoken.startswith("from ") else None
    delta = speech.price_delta(choice.price_delta)
    return f" for {delta}" if delta else None


def _plural(noun: str) -> str:
    if noun.endswith(("s", "x", "ch", "sh")):
        return noun + "es"
    if noun.endswith("y") and noun[-2:-1] not in ("a", "e", "i", "o", "u"):
        return noun[:-1] + "ies"
    return noun + "s"


def _group_name(group: JsonObj) -> str:
    return str(group.get("name") or group["id"]).lower()


def _options(
    profile: Profile, entry: CatalogItem, answer: _Answer, focused: frozenset[str] = frozenset()
) -> list[OptionGroupView]:
    groups: list[OptionGroupView] = []
    for group in entry.item.get("options") or ():
        choices: list[OptionChoiceView] = []
        for choice in group.get("choices") or ():
            base = f"{entry.ref}/{group['id']}/{choice['id']}"
            price = grounding.state(profile, f"{base}#price") if "price" in choice else None
            delta = grounding.state(profile, f"{base}#price_delta") if "price_delta" in choice else None
            name = grounding.state(profile, f"{base}#spoken_name")
            for got in (price, delta):
                if got is not None and got.fact is not None:
                    answer.facts.append(got.fact)
            if name.fact is not None:
                answer.facts.append(name.fact)
            choices.append(
                OptionChoiceView(
                    choice_id=str(choice["id"]),
                    spoken_name=str(choice.get("spoken_name") or choice["id"]),
                    price=render.price_view(price.value) if price is not None and price.ok else None,
                    price_delta=render.price_view(delta.value) if delta is not None and delta.ok else None,
                )
            )
        groups.append(
            OptionGroupView(
                group_id=str(group["id"]), name=str(group.get("name") or group["id"]), required=bool(group.get("required")), choices=choices
            )
        )
    if not groups:
        return groups
    names = [g.name.lower() for g in groups]
    if len(groups) > 1:
        answer.sentences.append(f"You choose the {speech.join(names)}")
    # Then the choices of the single (or first required) group, with their prices, so a voice
    # customer hears what they can pick: "The hair length can be shoulder length or shorter for $245
    # or past the shoulder blades for $285." Groups the customer's words already settled (the lead
    # speaks them) are skipped.
    open_groups = [g for g in groups if g.group_id not in focused]
    first = next((g for g in open_groups if g.required), None if focused else open_groups[0] if open_groups else None)
    if first is None:
        return groups
    phrases = [_choice_phrase(c) for c in first.choices[:MAX_SPOKEN_CHOICES]]
    extra = len(first.choices) - MAX_SPOKEN_CHOICES
    if extra > 0:
        phrases.append("one other option" if extra == 1 else f"{extra} other options")
    if phrases:
        answer.sentences.append(f"The {first.name.lower()} can be {speech.join(phrases, conjunction='or')}")
    elif len(groups) == 1:
        answer.sentences.append(f"You choose the {names[0]}")
    return groups


# ---------------------------------------------------------------------------- find_item: matched options


@dataclass(frozen=True)
class _Picked:
    """One option choice the customer's words point at, as a view built from statable facts only."""

    group: JsonObj
    choice: JsonObj
    ref: str
    view: OptionChoiceView


def _picked(profile: Profile, entry: CatalogItem, focus: Focus, answer: _Answer) -> tuple[JsonObj, list[_Picked]] | None:
    group = next((g for g in entry.item.get("options") or () if str(g["id"]) == focus.group_id), None)
    if group is None:
        return None
    by_id = {str(c["id"]): c for c in group.get("choices") or ()}
    out: list[_Picked] = []
    for choice_id in focus.choice_ids:
        choice = by_id.get(choice_id)
        if choice is None:
            continue
        ref = f"{entry.ref}/{group['id']}/{choice['id']}"
        got: dict[str, grounding.Stated] = {}
        for key, path in (
            ("name", "spoken_name"),
            ("price", "price"),
            ("delta", "price_delta"),
            ("allergens", "allergens.contains"),
            ("dietary", "dietary.tags"),
            ("serves", "attributes.serves"),
        ):
            stated = grounding.state(profile, f"{ref}#{path}")
            if stated.ok and stated.fact is not None:
                got[key] = stated
                answer.facts.append(stated.fact)
        serves = got["serves"].value if "serves" in got else None
        view = OptionChoiceView(
            choice_id=str(choice["id"]),
            spoken_name=str(choice.get("spoken_name") or choice["id"]),
            price=render.price_view(got["price"].value) if "price" in got else None,
            price_delta=render.price_view(got["delta"].value) if "delta" in got else None,
            allergens=[str(a) for a in got["allergens"].value or ()] if "allergens" in got else None,
            dietary=[str(t) for t in got["dietary"].value or ()] if "dietary" in got else None,
            serves=serves if isinstance(serves, int) and not isinstance(serves, bool) else None,
        )
        out.append(_Picked(group=group, choice=choice, ref=ref, view=view))
    return (group, out) if out else None


def _choice_card_value(view: OptionChoiceView) -> str:
    """'$12 more per tier', '$120, serves up to 36', 'Gluten-free' ... for a matched option's card line."""
    parts = [
        speech.price(view.price) if view.price is not None else speech.price_delta(view.price_delta),
        f"serves up to {view.serves}" if view.serves is not None else None,
        speech.join([speech.dietary(t) for t in view.dietary or ()]) or None,
    ]
    return speech.capitalize(", ".join(p for p in parts if p)) or "Available"


def _lead(*forms: str) -> tuple[str, ...]:
    """A lead sentence's forms that fit `LEAD_LIMIT` (the shortest always kept), most informative first."""
    fitting = tuple(form for form in forms if len(form) <= LEAD_LIMIT)
    return fitting or forms[-1:]


def _dietary_lead(entry: CatalogItem, focus: Focus, group: JsonObj, picked: list[_Picked], answer: _Answer, *, with_price: bool) -> None:
    """'The custom cake has gluten-free cake flavors for $12 more per tier: ...' (names if they fit)."""
    item = entry.spoken_name
    label = speech.join([speech.dietary(t) for t in focus.tags])
    noun = _group_name(group)
    suffixes = {_price_suffix(p.view) for p in picked} if with_price else {None}
    suffix = next(iter(suffixes)) if len(suffixes) == 1 and None not in suffixes else ""
    if len(picked) == 1:
        name = picked[0].view.spoken_name
        phrase = _choice_phrase(picked[0].view) if with_price else name
        answer.lead.append(
            _lead(f"The {item} has a {label} {noun}, the {phrase}", f"The {item} has a {label} {noun}, the {name}", f"The {item} has a {label} {noun}")
        )
        return
    answer.lead.append(_lead(f"The {item} has {label} {_plural(noun)}{suffix}", f"The {item} has {label} {_plural(noun)}"))
    answer.sentences.insert(0, f"They are {speech.join([p.view.spoken_name for p in picked])}")


def _servings_lead(entry: CatalogItem, picked: list[_Picked], answer: _Answer, *, with_price: bool) -> bool:
    """'The smallest custom cake for that many is the ten inch round, which serves up to 36 for $120.'
    Returns True when a choice price was spoken."""
    order = [str(c["id"]) for c in picked[0].group.get("choices") or ()]
    ranked = sorted(
        (p for p in picked if p.view.serves is not None),
        key=lambda p: (p.view.serves, p.view.price.amount_cents if p.view.price and p.view.price.amount_cents is not None else 1 << 40, order.index(str(p.choice["id"]))),
    )
    if not ranked:
        return False
    first = ranked[0]
    price = _price_suffix(first.view) if with_price else None
    sentence = f"The smallest {entry.spoken_name} for that many is the {first.view.spoken_name}, which serves up to {first.view.serves}"
    answer.lead.append(_lead(f"{sentence}{price or ''}", sentence, f"The {first.view.spoken_name} serves up to {first.view.serves}"))
    others = [p.view.spoken_name for p in ranked[1:]]
    if others:
        answer.sentences.insert(0, f"The {speech.join(others)} also {'serves' if len(others) == 1 else 'serve'} that many")
    return bool(price) and first.view.price is not None


def _named_lead(entry: CatalogItem, named: list[tuple[JsonObj, list[_Picked]]], answer: _Answer, *, with_price: bool) -> bool:
    """'For the custom cake, the cake flavor can be carrot with walnuts.' Returns True when a fixed choice
    price was spoken."""
    def sentence(priced: bool) -> str:
        clauses: list[str] = []
        for group, picked in named[:2]:
            phrases = [_choice_phrase(p.view) if priced else p.view.spoken_name for p in picked[:MAX_SPOKEN_CHOICES]]
            extra = len(picked) - MAX_SPOKEN_CHOICES
            if extra > 0:
                phrases.append("one other option" if extra == 1 else f"{extra} other options")
            clauses.append(f"the {_group_name(group)} can be {speech.join(phrases, conjunction='or')}")
        return f"For the {entry.spoken_name}, {speech.join(clauses)}"

    def compact(priced: bool) -> str:
        """Without the group names: 'For the TV mounting, that's 56 to 85 inches for $199 and brick,
        stone or above a fireplace for $100 more.'"""
        parts = [
            speech.join([_choice_phrase(p.view) if priced else p.view.spoken_name for p in picked[:MAX_SPOKEN_CHOICES]], conjunction="or")
            for _group, picked in named[:2]
        ]
        return f"For the {entry.spoken_name}, that's {speech.join(parts)}"

    shortest = f"That's the {entry.spoken_name}"
    if with_price:
        lead = _lead(sentence(True), compact(True), sentence(False), compact(False), shortest)
    else:
        lead = _lead(sentence(False), compact(False), shortest)
    answer.lead.append(lead)
    priced = with_price and lead[0] in (sentence(True), compact(True))
    return priced and any(p.view.price is not None for _group, picked in named[:2] for p in picked[:MAX_SPOKEN_CHOICES])


def _servings_note(profile: Profile, entry: CatalogItem, lookup: ItemLookup, answer: _Answer) -> None:
    """The item's own head count when a count was asked for and no option choice answers it: 'The six
    inch case cake serves up to 12', or the largest choice when none serves that many."""
    if lookup.servings is None or lookup.servings_met is None or any(f.reason == "servings" for f in lookup.focus):
        return
    own = grounding.state(profile, f"{entry.ref}#attributes.serves")
    if own.ok and isinstance(own.value, int) and own.fact is not None:
        answer.facts.append(own.fact)
        answer.lead.append((f"The {entry.spoken_name} serves up to {own.value}",))
        return
    largest: tuple[int, str, Fact] | None = None
    for group in entry.item.get("options") or ():
        for choice in group.get("choices") or ():
            got = grounding.state(profile, f"{entry.ref}/{group['id']}/{choice['id']}#attributes.serves")
            if got.ok and isinstance(got.value, int) and got.fact is not None and (largest is None or got.value > largest[0]):
                largest = (got.value, str(choice.get("spoken_name") or choice["id"]), got.fact)
    if largest is not None:
        serves, name, fact = largest
        answer.facts += [fact, *grounding.facts_for(profile, [fact.fact_ref.split("#")[0] + "#spoken_name"])]
        answer.lead.append((f"The biggest {entry.spoken_name} is the {name}, which serves up to {serves}",))


def _focus(
    profile: Profile, entry: CatalogItem, lookup: ItemLookup, answer: _Answer, *, with_price: bool
) -> tuple[list[OptionGroupView], bool]:
    """Lead sentences, facts and `matched_options` for the option choices the words point at. Returns
    the views and whether a fixed choice price was spoken (then the item's own "from" price is not)."""
    views: list[OptionGroupView] = []
    named: list[tuple[JsonObj, list[_Picked]]] = []
    stated_price = False
    for focus in lookup.focus:
        found = _picked(profile, entry, focus, answer)
        if found is None:
            continue
        group, picked = found
        views.append(
            OptionGroupView(
                group_id=str(group["id"]),
                name=str(group.get("name") or group["id"]),
                required=bool(group.get("required")),
                choices=[p.view for p in picked],
            )
        )
        answer.card_lines += [CardLine(label=speech.capitalize(p.view.spoken_name), value=_choice_card_value(p.view)) for p in picked]
        if focus.reason == "servings":
            stated_price = _servings_lead(entry, picked, answer, with_price=with_price) or stated_price
        elif focus.tags:
            _dietary_lead(entry, focus, group, picked, answer, with_price=with_price)
        else:
            named.append((group, picked))
    if named:
        stated_price = _named_lead(entry, named, answer, with_price=with_price) or stated_price
    _servings_note(profile, entry, lookup, answer)
    return views, stated_price


def _dietary(profile: Profile, entry: CatalogItem, lookup: ItemLookup, answer: _Answer, fields: dict[str, object]) -> None:
    """The dietary aspect. A dietary need the words carried and an option choice meets is already the
    lead ('The custom cake has gluten-free cake flavors'); a named choice with its own owner-confirmed
    tags speaks those; otherwise the item's own tags (or the hand-off)."""
    spoken = entry.spoken_name
    if any(f.tags for f in lookup.focus):
        return
    choice_tags = [
        (str(choice.get("spoken_name") or choice_id), got)
        for focus in lookup.focus
        for group in entry.item.get("options") or ()
        if str(group["id"]) == focus.group_id
        for choice in group.get("choices") or ()
        if (choice_id := str(choice["id"])) in focus.choice_ids
        and (got := grounding.state(profile, f"{entry.ref}/{group['id']}/{choice_id}#dietary.tags")).ok
    ]
    if choice_tags:
        for name, got in choice_tags[:2]:
            tags = [str(t) for t in got.value or ()]
            answer.facts.append(got.fact)  # type: ignore[arg-type]
            answer.sentences.append(
                f"The {name} is {speech.join([speech.dietary(t) for t in tags])}" if tags else f"The {name} has no dietary labels"
            )
        return
    got = grounding.state(profile, f"{entry.ref}#dietary.tags")
    if got.ok:
        tags = [str(t) for t in got.value or ()]
        fields["dietary"] = tags
        answer.facts.append(got.fact)  # type: ignore[arg-type]
        answer.sentences.append(
            f"The {spoken} is {speech.join([speech.dietary(t) for t in tags])}" if tags else f"The {spoken} has no dietary labels"
        )
    else:
        _withheld_or_unknown(
            profile, answer, got, what=f"the dietary details of the {spoken}", question=f"Is the {spoken} vegan, vegetarian or gluten-free?"
        )


# ---------------------------------------------------------------------------- find_item


def unmet_result(profile: Profile, lookup: ItemLookup, *, asked: str, result_type: type[G], **fields: object) -> G:
    """The hand-off for a dietary need no candidate is confirmed to meet: 'I don't know whether the
    custom cake comes nut-free. Want me to ask Rosa?' (shared by find_item, check_availability and the
    commit tools). The customer's words go into the hand-off question only."""
    sentence, facts = unmet_dietary_answer(profile, lookup)
    askable = grounding.can_ask_owner(profile)
    single = lookup.candidates[0] if len(lookup.candidates) == 1 else None
    hand = grounding.handoff(
        profile, reason="unknown", question=f"Do you have {asked}?", fact_ref=f"{single.ref}#dietary.tags" if single else None
    )
    card = grounding.handoff_card(profile, hand if askable else None, title="Not confirmed")
    card.citations = grounding.citations_for(profile, facts)
    return result_type(
        speak=speech.sentences(sentence, tail=grounding.offer_sentence(profile)),
        facts=facts,
        card=card,
        handoff=hand if askable else None,
        business_id=profile.profile_id,
        profile_revision=profile.revision,
        **fields,
    )


async def find_item(
    business_id: BusinessId,
    item: Annotated[str, Field(description="Item name as the customer said it, or an item ref.", max_length=120)],
    ctx: Context,
    aspects: Annotated[
        list[Literal["price", "options", "allergens", "dietary", "duration", "availability", "description"]] | None,
        Field(description="What the customer asked about; default price and options."),
    ] = None,
) -> ItemDetailResult:
    """Details of one item, as the customer said it ("gluten-free cake", "a cake for 30", "a trim").
    Allergen and dietary answers are only given when the owner confirmed them (always with the
    kitchen-wide cross-contact note); otherwise the reply offers to ask the owner."""
    deps = get_deps(ctx)
    with tool_span("find_item", business_id):
        profile = await live_profile(deps, business_id)
        lookup = resolve_item(profile, item)
        if lookup.unmet_dietary:
            single = lookup.candidates[0] if len(lookup.candidates) == 1 else None
            result = unmet_result(
                profile, lookup, asked=item, result_type=ItemDetailResult, item=summary_of(profile, single)[0] if single else None
            )
            return finish(result, profile, outcome="handoff")
        if lookup.found is None and lookup.candidates:
            names = [c.spoken_name for c in lookup.candidates]
            facts = grounding.facts_for(profile, [f"{c.ref}#spoken_name" for c in lookup.candidates])
            result = ItemDetailResult(
                speak=speech.sentences(f"Did you mean {speech.join(names, conjunction='or')}?"),
                facts=facts,
                card=Card(
                    kind="items",
                    title="Which one?",
                    subtitle=profile.name,
                    lines=[CardLine(label=c.name, value=c.spoken_name) for c in lookup.candidates],
                    citations=grounding.citations_for(profile, facts),
                    actions=[
                        CardAction(label=c.name, tool="find_item", arguments={"business_id": business_id, "item": c.ref})
                        for c in lookup.candidates
                    ],
                ),
            )
            return finish(result, profile, outcome="ambiguous")
        if lookup.found is None:
            result = grounding.unknown_answer(
                profile,
                question=f"Do you have {item}?",
                reason="not_offered",
                lead=f"I don't see that at {profile.spoken_name}.",
                result_type=ItemDetailResult,
            )
            return finish(result, profile, outcome="not_offered")
        entry = lookup.found
        answer = _Answer()
        summary, summary_facts = summary_of(profile, entry)
        answer.facts += summary_facts
        spoken = entry.spoken_name
        fields: dict[str, object] = {}
        wanted = list(dict.fromkeys(aspects or DEFAULT_ASPECTS))
        if lookup.dietary and "dietary" not in wanted:
            wanted.insert(0, "dietary")  # "a gluten-free cookie": say whether it is, whatever else was asked
        matched, stated_price = _focus(profile, entry, lookup, answer, with_price="price" in wanted)
        focused = frozenset(view.group_id for view in matched)
        for aspect in wanted:
            if aspect == "price":
                if not stated_price and not _price(profile, entry, answer) and "options" not in wanted:
                    _options(profile, entry, answer, focused)
            elif aspect == "options":
                fields["options"] = _options(profile, entry, answer, focused)
            elif aspect == "allergens":
                fields.update(_allergens(profile, entry, answer, lookup.focus))
            elif aspect == "dietary":
                _dietary(profile, entry, lookup, answer, fields)
            elif aspect == "duration":
                got = grounding.state(profile, f"{entry.ref}#duration_minutes")
                if got.ok and isinstance(got.value, int):
                    fields["duration_minutes"] = got.value
                    answer.facts.append(got.fact)  # type: ignore[arg-type]
                    answer.sentences.append(f"The {spoken} takes about {speech.duration(got.value)}")
                else:
                    _withheld_or_unknown(profile, answer, got, what=f"how long the {spoken} takes", question=f"How long does the {spoken} take?")
            elif aspect == "availability":
                got = grounding.state(profile, f"{entry.ref}#availability")
                if got.ok and isinstance(got.value, dict):
                    answer.facts.append(got.fact)  # type: ignore[arg-type]
                    note = got.value.get("note_spoken")
                    status = _AVAILABILITY_SPEECH.get(str(got.value.get("status")), "is available")
                    answer.sentences.append(note if isinstance(note, str) and note else f"The {spoken} {status}")
                else:
                    _withheld_or_unknown(profile, answer, got, what=f"when the {spoken} is available", question=f"When is the {spoken} available?")
            elif aspect == "description":
                got = grounding.state(profile, f"{entry.ref}#description_spoken")
                if got.ok and isinstance(got.value, str):
                    fields["description_spoken"] = got.value
                    answer.facts.append(got.fact)  # type: ignore[arg-type]
                    answer.sentences.append(got.value)
                else:
                    _withheld_or_unknown(profile, answer, got, what=f"more about the {spoken}", question=f"Can you tell me more about the {spoken}?")
        offer = grounding.offer_sentence(profile) if answer.handoff is not None else None
        speak = answer.speak(offer) or speech.sentences(f"The {spoken} is on the menu at {profile.spoken_name}")
        facts = grounding.dedupe_facts(answer.facts)
        lines = []
        if summary.price is not None:
            lines.append(CardLine(label="Price", value=speech.price(summary.price) or "By quote"))
        for key, label in (("allergens", "Contains"), ("may_contain", "May contain"), ("facility_may_contain", "Kitchen also handles")):
            values = fields.get(key)
            if isinstance(values, list):
                lines.append(CardLine(label=label, value=speech.join([speech.allergen(v) for v in values]) or "None of the major allergens"))
        lines += answer.card_lines
        if isinstance(fields.get("dietary"), list):
            lines.append(CardLine(label="Dietary", value=speech.join([speech.dietary(t) for t in fields["dietary"]]) or "No labels"))  # type: ignore[union-attr]
        result = ItemDetailResult(
            speak=speak,
            facts=facts,
            card=Card(
                kind="item",
                title=entry.name,
                subtitle=profile.name,
                lines=lines,
                badges=[b for b in ([summary.availability.replace("_", " ")] if summary.availability else [])],
                citations=grounding.citations_for(profile, facts),
                actions=_item_actions(profile, entry, business_id),
            ),
            handoff=answer.handoff,
            item=summary,
            offered_via=profile.transactional_modules_for(entry.ref),
            matched_options=matched,
            **fields,  # type: ignore[arg-type]
        )
        outcome = "ok" if answer.handoff is None else ("withheld" if answer.handoff.reason == "disputed" else "handoff")
        return finish(result, profile, outcome=outcome)


def _price(profile: Profile, entry: CatalogItem, answer: _Answer) -> bool:
    """The item's own price sentence. Returns False (and says nothing) when the item publishes no price
    of its own but its option choices do (the travel fee: no charge in the core area, $35 in the
    extended area): the options sentence carries the prices."""
    got = grounding.state(profile, f"{entry.ref}#price")
    spoken = entry.spoken_name
    if not got.ok:
        if got.absent and not grounding.is_disputed(profile, got) and _choices_priced(profile, entry):
            return False
        _withheld_or_unknown(profile, answer, got, what=f"the price of the {spoken}", question=f"How much is the {spoken}?")
        return True
    answer.facts.append(got.fact)  # type: ignore[arg-type]
    phrase = speech.price(got.value)
    if phrase == "free":
        answer.sentences.append(f"The {spoken} is free")
    elif phrase and phrase.startswith("from "):
        answer.sentences.append(f"The {spoken} starts at {phrase[len('from '):]}")
    elif phrase:
        answer.sentences.append(f"The {spoken} is {phrase}")
    else:
        answer.sentences.append(f"The {spoken} is priced by quote, so {profile.spoken_name} gives you the price")
    return True


def _choices_priced(profile: Profile, entry: CatalogItem) -> bool:
    """Every choice of the item's first option group has a statable price of its own."""
    groups = list(entry.item.get("options") or ())
    choices = list(groups[0].get("choices") or ()) if groups else []
    return bool(choices) and all(
        "price" in choice and grounding.state(profile, f"{entry.ref}/{groups[0]['id']}/{choice['id']}#price").ok for choice in choices
    )


def _allergen_list(values: object) -> list[str]:
    return [str(a) for a in values or ()]  # type: ignore[attr-defined]


def _contains_sentence(subject: str, listed: list[str]) -> str:
    if not listed:
        return f"{subject} contains none of the nine major allergens"
    return f"{subject} contains {speech.join([speech.allergen(a) for a in listed])}"


def _allergens(
    profile: Profile, entry: CatalogItem, answer: _Answer, focus: tuple[Focus, ...] = ()
) -> dict[str, list[str] | None]:
    """Item allergens (or per-choice ones), may-contain, and the kitchen-wide statement. Every safety
    sentence and safety hand-off goes to `answer.safety` (spoken first)."""
    spoken = entry.spoken_name
    fields: dict[str, list[str] | None] = {}
    contains = grounding.state(profile, f"{entry.ref}#allergens.contains")
    if contains.ok:
        listed = _allergen_list(contains.value)
        fields["allergens"] = listed
        answer.facts.append(contains.fact)  # type: ignore[arg-type]
        answer.safety.append(_contains_sentence(f"The {spoken}", listed))
        may = grounding.state(profile, f"{entry.ref}#allergens.may_contain")
        if may.ok:
            if may.value:
                fields["may_contain"] = _allergen_list(may.value)
                answer.facts.append(may.fact)  # type: ignore[arg-type]
                answer.safety.append(f"It may contain {speech.join([speech.allergen(a) for a in fields['may_contain'] or []])}")
        elif not may.absent or grounding.is_disputed(profile, may):
            # Withheld (disputed, unconfirmed): never let "contains X" stand as the whole answer.
            _withheld_or_unknown(
                profile, answer, may, what=f"whether the {spoken} may contain anything else",
                question=f"Could the {spoken} contain traces of other allergens?", safety=True,
            )
    elif contains.absent and not grounding.is_disputed(profile, contains) and _choice_allergens(profile, entry, answer, focus):
        pass  # spoken per option choice
    else:
        _withheld_or_unknown(
            profile, answer, contains, what=f"the allergens in the {spoken}",
            question=f"What allergens are in the {spoken}?", safety=True,
        )
    facility = grounding.state(profile, f"{entry.module_id}#facility_may_contain.allergens")
    if facility.ok:
        kitchen = _allergen_list(facility.value)
        fields["facility_may_contain"] = kitchen
        answer.facts.append(facility.fact)  # type: ignore[arg-type]
        if kitchen:
            answer.safety.append(f"The kitchen also handles {speech.join([speech.allergen(a) for a in kitchen])}")
    else:
        # Missing or withheld: an allergen answer is never complete without the cross-contact note.
        _withheld_or_unknown(
            profile, answer, facility, what="what else the kitchen handles",
            question="What allergens does your kitchen handle?", safety=True,
        )
    return fields


def _allergen_groups(entry: CatalogItem) -> list[JsonObj]:
    """Option groups where some choice states allergens ('choices replace item values')."""
    return [g for g in entry.item.get("options") or () if any("allergens" in choice for choice in g.get("choices") or ())]


def _choice_allergens(profile: Profile, entry: CatalogItem, answer: _Answer, focus: tuple[Focus, ...] = ()) -> bool:
    """Allergens stated on option choices ('choices replace item values'). Returns False when no
    choice states any (the caller hands off). With a focus on choices that state allergens (the
    gluten-free flavors), only those are spoken for their group, plus what the other groups can add;
    otherwise up to `MAX_SPOKEN_CHOICES` choices are spoken one by one and more are summarised as
    everything the item can contain depending on the choices."""
    groups = _allergen_groups(entry)
    focused = {f.group_id: f for f in focus if any(str(g["id"]) == f.group_id for g in groups)}
    if focused:
        return _focused_allergens(profile, entry, answer, groups, focused)
    stated: list[tuple[str, list[str]]] = []
    unknown_groups: list[str] = []
    for group in groups:
        for choice in group.get("choices") or ():
            got = grounding.state(profile, f"{entry.ref}/{group['id']}/{choice['id']}#allergens.contains")
            if got.ok:
                answer.facts.append(got.fact)  # type: ignore[arg-type]
                stated.append((str(choice.get("spoken_name") or choice["id"]), _allergen_list(got.value)))
            else:
                unknown_groups.append(_group_name(group))
    if not stated:
        return False
    for name, listed in stated:
        answer.card_lines.append(
            CardLine(label=f"Contains ({name})", value=speech.join([speech.allergen(a) for a in listed]) or "None of the major allergens")
        )
    if len(stated) <= MAX_SPOKEN_CHOICES:
        answer.safety.extend(_contains_sentence(f"The {name}", listed) for name, listed in stated)
    else:
        union = list(dict.fromkeys(a for _name, listed in stated for a in listed))
        answer.safety.append(
            f"Depending on what you choose, the {entry.spoken_name} can contain {speech.join([speech.allergen(a) for a in union])}"
            if union
            else f"None of the {entry.spoken_name} options contain the nine major allergens"
        )
    _unknown_choice_allergens(profile, entry, answer, unknown_groups)
    return True


def _unknown_choice_allergens(profile: Profile, entry: CatalogItem, answer: _Answer, unknown_groups: list[str]) -> None:
    if unknown_groups:
        groups = speech.join(list(dict.fromkeys(unknown_groups)))
        answer.hand_off(
            profile, reason="unknown", question=f"What allergens are in each {groups} option of the {entry.spoken_name}?",
            fact_ref=f"{entry.ref}#allergens", sentence=f"I don't know the allergens in every {groups} option", safety=True,
        )


def _focused_allergens(
    profile: Profile, entry: CatalogItem, answer: _Answer, groups: list[JsonObj], focused: dict[str, Focus]
) -> bool:
    """'The carrot with walnuts contains wheat, milk, eggs and tree nuts. Depending on the filling and
    frosting, it can also contain soy.' or, for several matched choices, 'Depending on what you choose,
    a gluten-free one can contain milk, eggs and soy.' Only owner-confirmed choice allergens count; a
    matched choice without them is a hand-off, never 'none'."""
    picked: list[tuple[str, list[str]]] = []
    other: list[str] = []
    other_names: list[str] = []
    unknown_picked: list[tuple[str, grounding.Stated]] = []
    unknown_groups: list[str] = []
    tags: tuple[str, ...] = ()
    for group in groups:
        gid = str(group["id"])
        focus = focused.get(gid)
        if focus is not None:
            tags = tags or focus.tags
        for choice in group.get("choices") or ():
            if focus is not None and str(choice["id"]) not in focus.choice_ids:
                continue
            name = str(choice.get("spoken_name") or choice["id"])
            got = grounding.state(profile, f"{entry.ref}/{gid}/{choice['id']}#allergens.contains")
            if got.ok:
                answer.facts.append(got.fact)  # type: ignore[arg-type]
                listed = _allergen_list(got.value)
                if focus is not None:
                    picked.append((name, listed))
                    answer.card_lines.append(
                        CardLine(label=f"Contains ({name})", value=speech.join([speech.allergen(a) for a in listed]) or "None of the major allergens")
                    )
                else:
                    other += listed
            elif focus is not None:
                unknown_picked.append((name, got))
            else:
                unknown_groups.append(_group_name(group))
        if focus is None and _group_name(group) not in other_names:
            other_names.append(_group_name(group))
    others = list(dict.fromkeys(other))
    if others:
        answer.card_lines.append(
            CardLine(label=f"{speech.capitalize(speech.join(other_names))} can add", value=speech.join([speech.allergen(a) for a in others]))
        )
    if len(picked) == 1 and not unknown_picked:
        name, listed = picked[0]
        answer.safety.append(_contains_sentence(f"The {name}", listed))
        extra = [a for a in others if a not in listed]
        if extra:
            answer.safety.append(f"The {speech.join(other_names)} can add {speech.join([speech.allergen(a) for a in extra])}")
    elif picked:
        union = list(dict.fromkeys([a for _name, listed in picked for a in listed] + others))
        label = speech.join([speech.dietary(t) for t in tags])
        subject = f"a {label} one" if label else f"the {entry.spoken_name}"
        answer.safety.append(
            f"Depending on what you choose, {subject} can contain {speech.join([speech.allergen(a) for a in union])}"
            if union
            else f"{speech.capitalize(subject)} contains none of the nine major allergens"
        )
    if unknown_picked:
        # Withheld (disputed) or unknown: never "none". A disputed one is "being double-checked".
        names = speech.join([name for name, _got in unknown_picked], conjunction="or")
        got = next((g for _name, g in unknown_picked if grounding.is_disputed(profile, g)), unknown_picked[0][1])
        _withheld_or_unknown(
            profile, answer, got, what=f"the allergens in the {names}",
            question=f"What allergens are in the {names} for the {entry.spoken_name}?", safety=True,
        )
    _unknown_choice_allergens(profile, entry, answer, unknown_groups)
    return bool(picked or unknown_picked)


def _item_actions(profile: Profile, entry: CatalogItem, business_id: str) -> list[CardAction]:
    actions: list[CardAction] = []
    for module_id in profile.transactional_modules_for(entry.ref):
        kind = profile.module_type(module_id)
        tool = {"booking": "check_availability", "custom_order": "place_custom_order", "quote_request": "request_quote"}.get(kind)
        if tool:
            label = {"check_availability": "See open times", "place_custom_order": "Order", "request_quote": "Get a quote"}[tool]
            actions.append(CardAction(label=label, tool=tool, arguments={"business_id": business_id, "item": entry.ref}))
    return actions
