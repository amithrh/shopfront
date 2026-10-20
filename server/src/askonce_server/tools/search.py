"""`find_business` matching over directory entries (live profiles only).

Scoring (deterministic, explainable; each matched signal is reported in `matched_on`):
  category  +3  a category token, or a synonym of one (CATEGORY_SYNONYMS), appears in `category`/`need`
  item      +2  a need phrase matches an item term (name, spoken name, synonym), longest match first;
                or one item's words (its terms and its option choices') explain most of the need,
                including its last word ("someone to put together ikea furniture": put, together and
                furniture are words of the furniture assembly item)
  dietary   +2  a dietary phrase in `need` ("gluten free", "vegan") matches an offered dietary tag
  keyword   +1  a business keyword or synonym appears in `need`
  locality  +2  `locality` matches a locality or postal code (address or service area) and is a hard
                filter: a place no certified business serves returns nothing (the tool says so)
                rather than a business in another city. "Portland, OR", "Austin TX", "East Austin"
                and ZIP+4 codes all match; "near me" and the like are no locality at all.
Ties break by specificity (item, dietary and keyword signals before a category-only match), then by
business name. A business with score 0 is never returned.
Text normalisation: casefold, NFC, strip punctuation, collapse whitespace, simple plural stripping.

Each signal counts at most once per business, so a long list of synonyms cannot outweigh a direct
category match. Phrases match on whole words only ("hair" does not match "chair").
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

from pydantic import BaseModel

from askonce_server.profile.directory import DirectoryEntry

CATEGORY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "bakery": (
        "bakery", "baker", "bakeries", "cake", "cakes", "cake shop", "pastry", "patisserie", "bread", "cupcake",
        "cookie", "croissant",
    ),
    "custom_cakes": ("custom cake", "birthday cake", "wedding cake", "celebration cake"),
    "hair_salon": (
        "hair salon", "salon", "hairdresser", "hairstylist", "stylist", "haircut", "barber", "barbershop",
        "barber shop", "hair", "trim", "beard", "bangs",
    ),
    "plumber": (
        "plumber", "plumbing", "leak", "leaking", "drain", "water heater", "toilet", "faucet", "clog", "clogged",
        "sink", "pipe",
    ),
    "handyman": ("handyman", "handy man", "odd jobs", "assembly", "tv mounting", "furniture assembly"),
    "home_repair": ("home repair", "home services", "house repair", "drywall"),
}
"""Spoken/colloquial phrases -> open category tokens. Extend per vertical template (data, not logic).
Bare verbs ("fix", "repair", "mount") are deliberately absent: they say nothing about the trade, so
"fix my hair" must not find a handyman."""

IGNORED_LOCALITIES: frozenset[str] = frozenset(
    {"near me", "nearby", "here", "around here", "local", "my area", "close by", "closest", "anywhere"}
)
"""Locality values that name no place (hosts sometimes pass the customer's words through)."""

_ZIP = re.compile(r"^(\d{5})(?:-?\d{4})?$")

DIETARY_PHRASES: dict[str, tuple[str, ...]] = {
    "gluten_free": (
        "gluten free", "glutenfree", "gf", "without gluten", "no gluten", "celiac", "coeliac", "wheat free", "no wheat",
        "without wheat",
    ),
    "vegan": ("vegan", "plant based"),
    "vegetarian": ("vegetarian", "veggie"),
    "dairy_free": ("dairy free", "no dairy", "lactose free"),
    "egg_free": ("egg free", "no egg", "eggless"),
    "nut_free": ("nut free", "no nut"),
    "peanut_free": ("peanut free", "no peanut"),
    "soy_free": ("soy free", "no soy"),
    "sugar_free": ("sugar free", "no sugar"),
    "halal": ("halal",),
    "kosher": ("kosher",),
}
"""Customer phrases for each dietary tag of the profile schema's closed enum. "Wheat-free" asks for the
stricter gluten-free (every gluten-free item is wheat-free; a wheat-free one may still have rye)."""

NEED_FILLER: frozenset[str] = frozenset(
    """a an the some any my me i we our you your it its someone somebody anyone who that which can could
    will would to for of with and or in on at from need want looking find get help please do doe is are
    be have ha just something anything fix repair install done""".split()
)
"""Words of a need that say nothing about what is needed (normalised: "does" arrives as "doe")."""

SCORE_CATEGORY = 3.0
SCORE_ITEM = 2.0
SCORE_DIETARY = 2.0
SCORE_KEYWORD = 1.0
SCORE_LOCALITY = 2.0

_PUNCT = re.compile(r"[^\w\s]|_")
_SPACES = re.compile(r"\s+")


class Match(BaseModel):
    entry: DirectoryEntry
    score: float
    matched_on: list[str]


def _singular(token: str) -> str:
    """Very small plural stripper: bakeries -> bakery, cakes -> cake, glasses -> glass, bus -> bus."""
    if len(token) <= 3 or token.isdigit():
        return token
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith(("sses", "shes", "ches", "xes")):
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def normalize(text: str) -> str:
    """Casefold, NFC, punctuation to spaces (apostrophes vanish), collapse whitespace, singularise words."""
    out = unicodedata.normalize("NFC", text).casefold()
    out = out.replace("'", "").replace("’", "")
    out = _PUNCT.sub(" ", out)
    words = [_singular(w) for w in _SPACES.split(out) if w]
    return " ".join(words)


def contains_phrase(text: str, phrase: str) -> bool:
    """Whole-word phrase containment over already-normalised text."""
    return bool(phrase) and f" {phrase} " in f" {text} "


def _phrases(values: Iterable[str]) -> list[str]:
    """Normalised, de-duplicated phrases, longest first (so the most specific match is reported)."""
    seen = {normalize(v) for v in values if v and v.strip()}
    return sorted((p for p in seen if p), key=lambda p: (-len(p), p))


def _first_match(text: str, phrases: Iterable[str]) -> str | None:
    for phrase in _phrases(phrases):
        if contains_phrase(text, phrase):
            return phrase
    return None


GENERIC_HEADS: frozenset[str] = frozenset({"salon", "shop", "studio", "bar", "parlor", "parlour", "store"})
"""Category synonyms that name a kind of place, not a trade: "salon" is a hair salon only when nothing
else qualifies it ("a salon near me", "hair salon"), never in "nail salon" or "tanning salon"."""


def _category_phrases(token: str) -> list[str]:
    return [token.replace("_", " "), *CATEGORY_SYNONYMS.get(token, ())]


def _category_match(text: str, token: str) -> str | None:
    """The category phrase `text` contains, skipping a bare `GENERIC_HEADS` word that another word
    qualifies (the word before it is neither filler nor part of one of this category's phrases)."""
    phrases = _category_phrases(token)
    known = {w for p in phrases for w in normalize(p).split()}
    words = text.split()
    for phrase in _phrases(phrases):
        if not contains_phrase(text, phrase):
            continue
        if phrase in GENERIC_HEADS:
            before = [words[i - 1] for i, w in enumerate(words) if w == phrase and i > 0]
            if before and all(b not in NEED_FILLER and b not in known for b in before):
                continue
        return phrase
    return None


def _postal(text: str) -> str:
    """Upper-case, no spaces or dashes inside a US ZIP+4 (`80211-1234` -> `80211`)."""
    compact = re.sub(r"\s+", "", text).upper()
    zip_match = _ZIP.match(compact)
    return zip_match.group(1) if zip_match else compact


def locality_wanted(locality: str | None) -> str | None:
    """The raw locality to filter on, or None when it names no place ("near me")."""
    if not locality or not locality.strip():
        return None
    return None if normalize(locality) in IGNORED_LOCALITIES else locality.strip()


def _matches_locality(entry: DirectoryEntry, wanted: str) -> bool:
    """`wanted` (raw) names one of the entry's postal codes or localities, allowing a state, region or
    country after the town ("Portland, OR") and a neighbourhood around it ("East Austin")."""
    if not wanted:
        return False
    code = _postal(wanted)
    if any(code == _postal(known) for known in entry.postal_codes):
        return True
    place = normalize(wanted)
    for loc in entry.localities:
        known = normalize(loc)
        if known and (known == place or contains_phrase(place, known) or contains_phrase(known, place)):
            return True
    return False


def score_entry(entry: DirectoryEntry, *, need: str, category: str | None) -> tuple[float, list[str]]:
    """Score one entry against normalised `need` (+ optional category text). Locality is applied by `rank`."""
    text = normalize(" ".join(p for p in (category, need) if p))
    need_text = normalize(need)
    score = 0.0
    matched: list[str] = []
    for token in entry.categories:
        if _category_match(text, token):
            score += SCORE_CATEGORY
            matched.append("category")
            break
    if _first_match(need_text, entry.item_terms) or _item_words_match(need_text, entry.item_term_groups):
        score += SCORE_ITEM
        matched.append("item")
    for tag in entry.dietary_tags:
        if _first_match(need_text, DIETARY_PHRASES.get(tag, (tag.replace("_", " "),))):
            score += SCORE_DIETARY
            matched.append("dietary")
            break
    if _first_match(need_text, [*entry.keywords, *entry.synonyms, entry.name, entry.spoken_name]):
        score += SCORE_KEYWORD
        matched.append("keyword")
    return score, matched


def _item_words_match(need_text: str, groups: Sequence[Sequence[str]]) -> bool:
    """One item's words explain at least two of the need's words, more than they leave over, and its
    last word (the thing needed): "put together ikea furniture" for furniture assembly, never
    "chocolate chip pancakes" for a chocolate chip cookie."""
    words = [w for w in need_text.split() if w not in NEED_FILLER]
    if len(words) < 2:
        return False
    for terms in groups:
        vocabulary = {w for term in terms for w in normalize(term).split()} - NEED_FILLER
        explained = [w for w in words if w in vocabulary]
        if len(explained) >= 2 and len(explained) > len(words) - len(explained) and words[-1] in vocabulary:
            return True
    return False


def _specificity(match: Match) -> int:
    """Signals that name what the customer wants (item, dietary, keyword), not just the trade."""
    return sum(1 for signal in match.matched_on if signal in ("item", "dietary", "keyword"))


def rank(
    entries: Sequence[DirectoryEntry],
    *,
    need: str | None,
    category: str | None = None,
    locality: str | None = None,
    limit: int = 5,
) -> list[Match]:
    """Score and order entries (see module docstring)."""
    wanted_locality = locality_wanted(locality) or ""
    local_ids = {e.business_id for e in entries if _matches_locality(e, wanted_locality)}
    matches: list[Match] = []
    for entry in entries:
        if wanted_locality and entry.business_id not in local_ids:
            continue  # a locality is a hard filter: never a business in another city
        score, matched = score_entry(entry, need=need or "", category=category)
        if score > 0 and entry.business_id in local_ids:
            score += SCORE_LOCALITY
            matched.append("locality")
        if score <= 0:
            continue
        matches.append(Match(entry=entry, score=score, matched_on=matched))
    matches.sort(key=lambda m: (-m.score, -_specificity(m), m.entry.name.casefold(), m.entry.business_id))
    return matches[: max(0, limit)]
