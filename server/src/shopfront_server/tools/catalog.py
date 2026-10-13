"""Catalog tools: get_catalog (browse) and find_item (details incl. owner-confirmed allergens).

Item resolution (`resolve_item`) is shared with the transactional tools: an item ref or bare id, an
exact name / spoken name / synonym, then the longest partial phrase match. Several equally good
partial matches are an ambiguity (the reply lists them) rather than a guess. Transactional tools pass
`module_type`, so only items that can be booked / ordered / quoted that way compete ("cake" at the
bakery is the custom cake for an order, not the slice of cake), falling back to the whole catalog.

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
from typing import Annotated, Literal

from pydantic import Field

from mcp.server.mcpserver import Context
from shopfront_server.profile.model import JsonObj, Profile
from shopfront_server.tools import grounding, render, search, speech
from shopfront_server.tools.context import finish, get_deps, live_profile, tool_span
from shopfront_server.tools.discovery import BusinessId
from shopfront_server.tools.schemas import (
    Card,
    CardAction,
    CardLine,
    CatalogResult,
    Fact,
    HandOff,
    ItemDetailResult,
    ItemSummary,
    OptionChoiceView,
    OptionGroupView,
)

Aspect = Literal["price", "options", "allergens", "dietary", "duration", "availability", "description"]
DEFAULT_ASPECTS: tuple[Aspect, ...] = ("price", "options")
MAX_CANDIDATES = 3

_AVAILABILITY_SPEECH = {
    "available": "is available",
    "limited": "is available in limited numbers",
    "seasonal": "is seasonal",
    "made_to_order": "is made to order",
    "sold_out": "is sold out right now",
}


# ---------------------------------------------------------------------------- item resolution


@dataclass(frozen=True)
class CatalogItem:
    module_id: str
    ref: str
    item: JsonObj

    @property
    def spoken_name(self) -> str:
        return str(self.item.get("spoken_name") or self.item.get("name") or self.ref)

    @property
    def name(self) -> str:
        return str(self.item.get("name") or self.spoken_name)


@dataclass(frozen=True)
class ItemLookup:
    """`found` when one item matched; `candidates` when several matched equally well."""

    found: CatalogItem | None
    candidates: list[CatalogItem] = field(default_factory=list)


def item_terms(item: JsonObj) -> list[str]:
    return [str(t) for t in (item.get("name"), item.get("spoken_name"), *(item.get("synonyms") or ())) if t]


def catalog_entries(profile: Profile) -> list[CatalogItem]:
    return [CatalogItem(module_id, profile.item_ref(module_id, item), item) for module_id, item in profile.catalog_items()]


TRANSACTIONAL = "transactional"
"""`module_type` for `resolve_item`: any booking, custom_order or quote_request module."""


def _accepted_by(profile: Profile, entry: CatalogItem, module_type: str) -> bool:
    wanted = None if module_type == TRANSACTIONAL else module_type
    return bool(profile.transactional_modules_for(entry.ref, wanted))  # type: ignore[arg-type]


def resolve_item(profile: Profile, text: str, *, module_type: str | None = None) -> ItemLookup:
    """Resolve what the customer said to one catalog item (see module docstring).

    With `module_type` (`booking`, `custom_order`, `quote_request` or `TRANSACTIONAL`), items that
    kind of module accepts are matched first; the whole catalog is the fallback, so an item that
    exists but cannot be booked still resolves (and the caller says so).
    """
    entries = catalog_entries(profile)
    if module_type is not None:
        accepted = [e for e in entries if _accepted_by(profile, e, module_type)]
        lookup = _resolve_in(accepted, text)
        if lookup.found is not None or lookup.candidates:
            return lookup
    return _resolve_in(entries, text)


def commit_item(profile: Profile, text: str, module_type: str) -> str:
    """What a transactional tool hands its domain service: the item ref when `text` resolves to one
    item (partial phrases included, so "haircut" plans like `check_availability` resolves it), else
    the text unchanged (the plan then reports the unknown item, and `commit_flow` turns several
    candidates into "Did you mean ...?")."""
    lookup = resolve_item(profile, text, module_type=module_type)
    return lookup.found.ref if lookup.found is not None else text


def _resolve_in(entries: list[CatalogItem], text: str) -> ItemLookup:
    wanted = text.strip()
    for entry in entries:
        if wanted in (entry.ref, entry.item.get("id")):
            return ItemLookup(found=entry)
    query = search.normalize(wanted)
    if not query:
        return ItemLookup(found=None)
    exact = [e for e in entries if any(search.normalize(t) == query for t in item_terms(e.item))]
    if len(exact) == 1:
        return ItemLookup(found=exact[0])
    if exact:
        return ItemLookup(found=None, candidates=exact[:MAX_CANDIDATES])
    scored: list[tuple[int, CatalogItem]] = []
    for entry in entries:
        best = 0
        for term in item_terms(entry.item):
            norm = search.normalize(term)
            if search.contains_phrase(query, norm) or search.contains_phrase(norm, query):
                best = max(best, len(norm) if search.contains_phrase(query, norm) else len(query))
        if best:
            scored.append((best, entry))
    if not scored:
        return ItemLookup(found=None)
    top = max(score for score, _ in scored)
    winners = [entry for score, entry in scored if score == top]
    if len(winners) == 1:
        return ItemLookup(found=winners[0])
    return ItemLookup(found=None, candidates=winners[:MAX_CANDIDATES])


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


def _dietary_tags_wanted(query: str) -> set[str]:
    norm = search.normalize(query)
    return {
        tag for tag, phrases in search.DIETARY_PHRASES.items() if any(search.contains_phrase(norm, search.normalize(p)) for p in phrases)
    }


def _matches_query(profile: Profile, entry: CatalogItem, query: str) -> bool:
    norm = search.normalize(query)
    for term in item_terms(entry.item):
        term_norm = search.normalize(term)
        if search.contains_phrase(term_norm, norm) or search.contains_phrase(norm, term_norm):
            return True
    wanted_tags = _dietary_tags_wanted(query)
    if wanted_tags:
        got = grounding.state(profile, f"{entry.ref}#dietary.tags")
        if got.ok and wanted_tags <= set(got.value or ()):
            return True
    return False


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
            entries = [e for e in entries if _matches_query(profile, e, query)]
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
    """Sentences of a find_item reply, in speaking order: `safety` (allergens, cross-contact and their
    hand-offs: never truncated away), then `priority` (other hand-offs), then `sentences`."""

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
        """Everything that fits, safety first. If even the safety sentences and the tail overflow, the
        other sentences go before any safety sentence does."""
        spoken = speech.sentences(*self.safety, *self.priority, *self.sentences, tail=tail)
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


def _choice_phrase(choice: OptionChoiceView) -> str:
    """'six inch round for $56', 'gluten-free vanilla for $12 more', or just the name."""
    if choice.price is not None:
        return priced_phrase(choice.spoken_name, choice.price)
    delta = speech.price(choice.price_delta) if choice.price_delta is not None else None
    if delta and delta != "free" and not delta.startswith("from "):
        return f"{choice.spoken_name} for {delta} more"
    return choice.spoken_name


def _options(profile: Profile, entry: CatalogItem, answer: _Answer) -> list[OptionGroupView]:
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
    # or past the shoulder blades for $285."
    first = next((g for g in groups if g.required), groups[0])
    phrases = [_choice_phrase(c) for c in first.choices[:MAX_SPOKEN_CHOICES]]
    extra = len(first.choices) - MAX_SPOKEN_CHOICES
    if extra > 0:
        phrases.append("one other option" if extra == 1 else f"{extra} other options")
    if phrases:
        answer.sentences.append(f"The {first.name.lower()} can be {speech.join(phrases, conjunction='or')}")
    elif len(groups) == 1:
        answer.sentences.append(f"You choose the {names[0]}")
    return groups


async def find_item(
    business_id: BusinessId,
    item: Annotated[str, Field(description="Item name as the customer said it, or an item ref.", max_length=120)],
    ctx: Context,
    aspects: Annotated[
        list[Literal["price", "options", "allergens", "dietary", "duration", "availability", "description"]] | None,
        Field(description="What the customer asked about; default price and options."),
    ] = None,
) -> ItemDetailResult:
    """Details of one item. Allergen and dietary answers are only given when the owner confirmed them
    (always with the kitchen-wide cross-contact note); otherwise the reply offers to ask the owner."""
    deps = get_deps(ctx)
    with tool_span("find_item", business_id):
        profile = await live_profile(deps, business_id)
        lookup = resolve_item(profile, item)
        if lookup.found is None and lookup.candidates:
            names = [c.spoken_name for c in lookup.candidates]
            result = ItemDetailResult(
                speak=speech.sentences(f"Did you mean {speech.join(names, conjunction='or')}?"),
                facts=grounding.facts_for(profile, [f"{c.ref}#spoken_name" for c in lookup.candidates]),
                card=Card(
                    kind="items",
                    title="Which one?",
                    subtitle=profile.name,
                    lines=[CardLine(label=c.name, value=c.spoken_name) for c in lookup.candidates],
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
        for aspect in dict.fromkeys(aspects or DEFAULT_ASPECTS):
            if aspect == "price":
                _price(profile, entry, answer)
            elif aspect == "options":
                fields["options"] = _options(profile, entry, answer)
            elif aspect == "allergens":
                fields.update(_allergens(profile, entry, answer))
            elif aspect == "dietary":
                got = grounding.state(profile, f"{entry.ref}#dietary.tags")
                if got.ok:
                    tags = [str(t) for t in got.value or ()]
                    fields["dietary"] = tags
                    answer.facts.append(got.fact)  # type: ignore[arg-type]
                    answer.sentences.append(
                        f"The {spoken} is {speech.join([speech.dietary(t) for t in tags])}"
                        if tags
                        else f"The {spoken} has no dietary labels"
                    )
                else:
                    _withheld_or_unknown(
                        profile, answer, got, what=f"the dietary details of the {spoken}", question=f"Is the {spoken} vegan, vegetarian or gluten-free?"
                    )
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
            **fields,  # type: ignore[arg-type]
        )
        outcome = "ok" if answer.handoff is None else ("withheld" if answer.handoff.reason == "disputed" else "handoff")
        return finish(result, profile, outcome=outcome)


def _price(profile: Profile, entry: CatalogItem, answer: _Answer) -> None:
    got = grounding.state(profile, f"{entry.ref}#price")
    spoken = entry.spoken_name
    if not got.ok:
        _withheld_or_unknown(profile, answer, got, what=f"the price of the {spoken}", question=f"How much is the {spoken}?")
        return
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


def _allergen_list(values: object) -> list[str]:
    return [str(a) for a in values or ()]  # type: ignore[attr-defined]


def _contains_sentence(subject: str, listed: list[str]) -> str:
    if not listed:
        return f"{subject} contains none of the nine major allergens"
    return f"{subject} contains {speech.join([speech.allergen(a) for a in listed])}"


def _allergens(profile: Profile, entry: CatalogItem, answer: _Answer) -> dict[str, list[str] | None]:
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
    elif contains.absent and not grounding.is_disputed(profile, contains) and _choice_allergens(profile, entry, answer):
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


def _choice_allergens(profile: Profile, entry: CatalogItem, answer: _Answer) -> bool:
    """Allergens stated on option choices ('choices replace item values'). Returns False when no
    choice states any (the caller hands off). Up to `MAX_SPOKEN_CHOICES` choices are spoken one by
    one; more are summarised as everything the item can contain depending on the choices."""
    stated: list[tuple[str, list[str]]] = []
    unknown_groups: list[str] = []
    for group in entry.item.get("options") or ():
        if not any("allergens" in choice for choice in group.get("choices") or ()):
            continue
        for choice in group.get("choices") or ():
            got = grounding.state(profile, f"{entry.ref}/{group['id']}/{choice['id']}#allergens.contains")
            if got.ok:
                answer.facts.append(got.fact)  # type: ignore[arg-type]
                stated.append((str(choice.get("spoken_name") or choice["id"]), _allergen_list(got.value)))
            else:
                unknown_groups.append(str(group.get("name") or group["id"]).lower())
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
    if unknown_groups:
        groups = speech.join(list(dict.fromkeys(unknown_groups)))
        answer.hand_off(
            profile, reason="unknown", question=f"What allergens are in each {groups} option of the {entry.spoken_name}?",
            fact_ref=f"{entry.ref}#allergens", sentence=f"I don't know the allergens in every {groups} option", safety=True,
        )
    return True


def _item_actions(profile: Profile, entry: CatalogItem, business_id: str) -> list[CardAction]:
    actions: list[CardAction] = []
    for module_id in profile.transactional_modules_for(entry.ref):
        kind = profile.module_type(module_id)
        tool = {"booking": "check_availability", "custom_order": "place_custom_order", "quote_request": "request_quote"}.get(kind)
        if tool:
            label = {"check_availability": "See open times", "place_custom_order": "Order", "request_quote": "Get a quote"}[tool]
            actions.append(CardAction(label=label, tool=tool, arguments={"business_id": business_id, "item": entry.ref}))
    return actions
