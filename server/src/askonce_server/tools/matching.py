"""What the customer said -> catalog item(s), shared by find_item, get_catalog and the transactional tools.

Customers rarely say an item's name. They say "a gluten-free cake", "a cake for 30", "a 10 inch cake",
"a trim" or "fix a leaky faucet". A phrase is read as words (normalised like `search.normalize`:
casefold, punctuation, simple plurals) and split into:

  * modifiers: dietary phrases ("gluten-free", "vegan", "no nuts"; `search.DIETARY_PHRASES`) and a
    head count ("for 30", "30 people", "serves 30");
  * filler: articles, pronouns and request words ("a", "my", "please", "fix", "need") that never
    have to be explained;
  * content: every other word.

When the words are read against a catalog (`parse(text, vocabulary)`), each content word also stands
for the catalog words it is a form of: "leaking" for "leak", "clogging" for "clog" and "clogged",
"draining" for "drain", "replace" for "replacement", "12" for "twelve". When the words as said find
nothing, a word one letter away from a catalog word of five or more letters ("watter", "drywal",
"furnature"; six for a changed letter) stands for it, but then the answer is a "Did you mean ...?",
never a silent pick. A number before a noun ("two dressers", "3 chairs") is a quantity that needs no
explaining, and a size ("75 inch", "2 by 2 foot") picks the option choice whose published range holds
it ("up to 2 by 2 feet", "56 to 85 inches"). Inside an item, a choice term's words that repeat the
item's own names are implied: the drain cleaning's "sink drain" is named by "sink".

Each catalog item is then scored on how it explains the content words:

  kind 0  an item term (name, spoken name, synonym) is contained in the phrase, word order free
          ("mount a 65 inch tv" -> "mount a tv"). Leftover words are allowed but counted.
  kind 1  the remaining words all sit inside one item term ("cake" -> "custom cake").
  kind 2  the remaining words occur in the item's vocabulary (its terms, its option choices'
          terms and its category names). Words may be left over ("clogged kitchen sink", "assemble
          two ikea dressers") only beside an option choice the phrase names, when at least two words
          are explained, more are explained than left over, and the head of the phrase (its last
          content word before a preposition or verb: "sink") is explained, so "chocolate chip
          pancakes" never borrows the chocolate chip cookie, nor "hair cut for my son" the curly cut.
  kind 3  option choices explain every word ("carrot cake" -> the custom cake's carrot flavour).
  kind 4  modifiers only ("gluten-free"): items that satisfy them.

Option choices named by the phrase ("10 inch", "long hair", "carrot cake") explain their words before
the item terms are tried. Modifiers are constraints, checked against owner-statable facts only
(`grounding.state`): a dietary tag holds when the item's tags carry it, or one option choice carries
every wanted tag (that choice becomes the answer's focus), or the item's own matched name says it; a
head count holds when the item, or some option choice, serves at least that many.

Ranking: unmet dietary tags, then an unmet head count, then unexplained words, then whether the head
of the phrase, where a preposition or verb marks it, is explained ("my water heater is leaking" is
about the water heater, not a leak; "faucet leak" stays a question), then kind. Among
equals, the longest match wins only when nothing was left unexplained; otherwise (and for any tie that
remains) the result is an ambiguity listing the candidates, never a silent pick. When the best
candidates cannot meet a dietary tag, `ItemLookup.unmet_dietary` says so and nothing is found: a
"nut-free cake" is never answered as if it were the plain cake.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from askonce_server.profile.model import JsonObj, Profile
from askonce_server.tools import grounding, search

MAX_CANDIDATES = 3

FILLER: frozenset[str] = frozenset(
    {
        # articles, determiners, pronouns
        "a", "an", "the", "some", "any", "this", "that", "these", "those", "one", "my", "me", "i", "im", "id",
        "ive", "we", "our", "u", "us", "you", "your", "it", "its", "their", "his", "her", "him", "them",
        "something", "anything", "thing", "item", "kind", "sort", "type", "option", "version", "please",
        "just", "also", "really", "maybe", "safe", "friendly", "someone", "somebody", "anyone",
        # prepositions and conjunctions
        "for", "of", "with", "and", "or", "to", "in", "on", "at", "from", "by", "about", "into", "up",
        # auxiliaries and request verbs
        "is", "are", "be", "was", "do", "doe", "did", "can", "could", "would", "will", "should", "may",
        "might", "need", "want", "wanted", "like", "looking", "look", "get", "got", "getting", "have",
        "ha", "had", "order", "buy", "help", "fix", "repair", "install", "done",
    }
)  # fmt: skip
"""Phrase words that never need explaining. Normalised forms (`search.normalize` strips a plural `s`,
so "does" and "has" arrive as "doe" and "ha"). Item terms that contain them still match them.
"replace" is not filler: replacing a sewer line is not cleaning one."""

OPTIONAL_IN_TERMS: frozenset[str] = frozenset({"a", "an", "the", "my", "your", "our", "his", "her", "their"})
"""Words of an item or choice term the phrase may leave out ("mount a tv" matches "mount my tv").
Deliberately small: a term keeps its verbs and nouns, so "fix my color" never matches a bare "color"."""

PREPOSITIONS: frozenset[str] = frozenset(
    {"with", "for", "under", "in", "on", "from", "over", "without", "at", "into", "behind", "near", "inside",
     "above", "below", "beside", "outside"}
)  # fmt: skip
"""Words after which a phrase only adds detail ("a leaky pipe under the sink"): the head comes before."""

HEAD_ENDS: frozenset[str] = PREPOSITIONS | frozenset(
    {"is", "are", "was", "were", "be", "been", "keep", "wont", "doesnt", "dont", "isnt", "cant", "ha", "have"}
)
"""Words that end the noun phrase naming the job: prepositions and the verb that follows a subject
("my water heater is leaking", "toilets keep clogging")."""

_NUMBER_WORDS: dict[str, int] = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "dozen": 12, "hundred": 100,
}  # fmt: skip
_TENS: dict[str, int] = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_SERVING_NOUNS = frozenset({"people", "person", "guest", "serving", "portion", "kid", "adult", "child", "children", "head"})
_SERVING_VERBS = frozenset({"serve", "feed", "feeding", "serving"})
_UNITS = frozenset(
    {"inch", "in", "foot", "feet", "ft", "minute", "min", "hour", "hr", "day", "week", "month", "year", "dollar",
     "buck", "cent", "percent", "pound", "lb", "layer", "tier", "am", "pm", "oclock", "piece", "slice", "loaf",
     "gallon", "gal"}
)  # fmt: skip
"""Words after a number that make it a size, a time or an amount rather than a head count."""

_LENGTH_UNITS: dict[str, float] = {"inch": 1.0, "in": 1.0, "foot": 12.0, "feet": 12.0, "ft": 12.0}
"""Length units (in inches) a size is read in: "75 inch", "2 by 2 foot"."""

_DIGIT_WORDS: dict[str, str] = {
    str(n): w for w, n in {**_NUMBER_WORDS, **_TENS}.items() if w not in ("dozen", "hundred")
}
"""'12' -> 'twelve' (and back), so "12 cookies" meets the synonym "twelve cookies"."""

_FORM_SUFFIXES = ("ing", "ed", "ment")
_MIN_BASE = 4
"""A word form is only folded onto a base of at least this many letters ("leaking" -> "leak", never
"icing" -> "ic"). Adjectives in "-y" are not folded: "brownies" is not "brown"."""
_MIN_TYPO_LENGTH = 5
"""Only catalog words this long take a one-letter correction ("watter" -> "water"); a changed or
swapped letter needs one more ("drywal" -> "drywall", "furnature" -> "furniture", never "hatch" ->
"patch" or "grain" -> "drain")."""

Kind = Literal[0, 1, 2, 3, 4]
FocusReason = Literal["named", "dietary", "servings"]


# ---------------------------------------------------------------------------- data


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
class Focus:
    """Option choices of one group that the customer's words point at."""

    group_id: str
    choice_ids: tuple[str, ...]
    reason: FocusReason
    tags: tuple[str, ...] = ()
    """For `dietary`: the dietary tags every choice here carries (owner-confirmed)."""


@dataclass(frozen=True)
class ItemLookup:
    """`found` when one item matched; `candidates` when several matched equally well, or when the one
    match relied on correcting a misspelt word (a "Did you mean ...?").

    `focus` (for `found`) lists the option choices the words named or that satisfy the modifiers.
    `unmet_dietary` is set (and `found` is None) when the best candidates cannot be confirmed to meet
    a dietary tag the customer asked for; `candidates` then lists them. `unexplained` lists the
    content words the found item (or any equally good candidate) left unexplained.
    """

    found: CatalogItem | None
    candidates: list[CatalogItem] = field(default_factory=list)
    focus: tuple[Focus, ...] = ()
    dietary: tuple[str, ...] = ()
    unmet_dietary: tuple[str, ...] = ()
    servings: int | None = None
    servings_met: bool | None = None
    """For `found` with `servings`: True if it (or a choice) serves that many, False if it is known
    not to, None if the profile does not say."""
    unexplained: tuple[str, ...] = ()
    corrected: bool = False
    """True when a misspelt word had to be corrected (the candidates are then a suggestion)."""


@dataclass(frozen=True)
class Measure:
    """A size in the phrase ("75 inch", "2 by 2 foot"): its largest dimension in inches."""

    inches: float
    words: frozenset[str]
    positions: frozenset[int]


@dataclass(frozen=True)
class Phrase:
    """A customer phrase as normalised words plus the modifiers found in it."""

    text: str
    tokens: tuple[str, ...]
    dietary: dict[str, frozenset[str]]
    """Wanted dietary tag -> the words of the phrase that named it."""
    servings: int | None
    modifier_positions: frozenset[int]
    aliases: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """Content word -> the catalog words it is a form of (or, see `typos`, a misspelling of)."""
    typos: frozenset[str] = frozenset()
    """Content words read as a misspelling of a catalog word."""
    quantity_positions: frozenset[int] = frozenset()
    """Numbers that count something ("two dressers", "3 chairs")."""
    measure: Measure | None = None

    @property
    def words(self) -> frozenset[str]:
        return frozenset(self.tokens).union(*self.aliases.values()) if self.aliases else frozenset(self.tokens)

    @property
    def content(self) -> tuple[str, ...]:
        """Words that must be explained: not filler and not part of a modifier."""
        return tuple(
            t for i, t in enumerate(self.tokens) if i not in self.modifier_positions and t not in FILLER
        )

    @property
    def quantities(self) -> frozenset[str]:
        return frozenset(self.tokens[i] for i in self.quantity_positions)

    def _head(self) -> tuple[str | None, bool]:
        last: str | None = None
        skip = self.modifier_positions | self.quantity_positions | (self.measure.positions if self.measure else frozenset())
        for i, token in enumerate(self.tokens):
            if token in HEAD_ENDS and last is not None:
                return last, True
            if i not in skip and token not in FILLER:
                last = token
        return last, False

    @property
    def head(self) -> str | None:
        """The last content word before the first preposition or verb that follows one ("leaky pipe under
        the sink" -> "pipe", "my water heater is leaking" -> "heater"), or the last content word."""
        return self._head()[0]

    @property
    def marked_head(self) -> str | None:
        """`head`, only when a preposition or verb marks where it ends ("faucet leak" has none: it may
        be the faucet or the leak)."""
        head, marked = self._head()
        return head if marked else None

    def forms(self, word: str) -> frozenset[str]:
        """`word` and the catalog words it stands for."""
        return frozenset({word}) | self.aliases.get(word, frozenset())


@dataclass(frozen=True)
class Match:
    entry: CatalogItem
    kind: Kind
    score: int
    unexplained: int
    missing_tags: tuple[str, ...]
    servings_met: bool | None
    """None when no head count was asked for, or the profile does not say how many it serves."""
    servings_rank: int
    """0 when no head count was asked for or it is met; 1 when it is not met or not known."""
    focus: tuple[Focus, ...]
    head_rank: int = 0
    """0 when the marked head of the phrase is explained by this item (or there is none), else 1."""
    left: tuple[str, ...] = ()
    """The content words this item leaves unexplained."""

    def rank(self) -> tuple[int, int, int, int, int]:
        return (len(self.missing_tags), self.servings_rank, self.unexplained, self.head_rank, self.kind)


# ---------------------------------------------------------------------------- phrase parsing


def tokens(text: str) -> tuple[str, ...]:
    return tuple(search.normalize(text).split())


def _key(words: Sequence[str]) -> frozenset[str]:
    """The words of a term that must appear in a phrase for the term to match. Optional words are only
    dropped while two or more words remain: "some highlights" or "your mount" must not shrink to a
    single generic word that every phrase about highlights or mounts contains."""
    meaningful = frozenset(w for w in words if w not in OPTIONAL_IN_TERMS)
    return meaningful if len(meaningful) >= 2 else frozenset(words)


def _number_at(words: Sequence[str], i: int) -> tuple[int, int] | None:
    """(value, words used) of a number starting at `i`: digits, "thirty", "twenty five", "two dozen"."""
    if i >= len(words):
        return None
    word = words[i]
    value: int | None = None
    used = 1
    if word == "half" and i + 1 < len(words) and words[i + 1] == "dozen":
        return 6, 2
    if word.isdigit():
        value = int(word)
    elif word in _TENS:
        value = _TENS[word]
        if i + 1 < len(words) and words[i + 1] in _NUMBER_WORDS and _NUMBER_WORDS[words[i + 1]] < 10:
            value += _NUMBER_WORDS[words[i + 1]]
            used = 2
    elif word in _NUMBER_WORDS:
        value = _NUMBER_WORDS[word]
    if value is None:
        return None
    if i + used < len(words) and words[i + used] in ("dozen", "hundred"):
        value *= _NUMBER_WORDS[words[i + used]]
        used += 1
    return value, used


def _servings(words: Sequence[str]) -> tuple[int | None, set[int]]:
    """A head count ("for 30", "30 people", "serves 30", "feeds twenty five guests") and its positions."""
    for i in range(len(words)):
        start = i
        if words[i] in _SERVING_VERBS or words[i] == "for":
            number = _number_at(words, i + 1)
            if number is None:
                continue
            value, used = number
            end = i + 1 + used
            follower = words[end] if end < len(words) else None
            if follower in _UNITS:
                continue  # "for 10 inches", "for 2 pm": a size or a time, not a head count
            if follower in _SERVING_NOUNS:
                end += 1
            return value, set(range(start, end))
        number = _number_at(words, i)
        if number is not None:
            value, used = number
            end = i + used
            if end < len(words) and words[end] in _SERVING_NOUNS:
                return value, set(range(start, end + 1))
    return None, set()


def _measure(words: Sequence[str], taken: set[int]) -> Measure | None:
    """A size: "75 inch", "seventy five inches", "2 by 2 foot", "2 x 2 ft" (the largest dimension)."""
    for i in range(len(words)):
        if i in taken:
            continue
        first = _number_at(words, i)
        if first is None:
            continue
        values = [float(first[0])]
        end = i + first[1]
        if end < len(words) and words[end] in ("by", "x"):
            second = _number_at(words, end + 1)
            if second is not None:
                values.append(float(second[0]))
                end = end + 1 + second[1]
        unit = words[end] if end < len(words) else None
        if unit in _LENGTH_UNITS:
            positions = frozenset(range(i, end + 1))
            return Measure(
                inches=max(values) * _LENGTH_UNITS[unit],
                words=frozenset(words[p] for p in positions),
                positions=positions,
            )
    return None


def _quantities(words: Sequence[str], taken: set[int]) -> set[int]:
    """Numbers that count the next word ("two dressers", "3 chairs"), not sizes, times or head counts."""
    out: set[int] = set()
    i = 0
    while i < len(words):
        number = _number_at(words, i) if i not in taken else None
        if number is None:
            i += 1
            continue
        _value, used = number
        follower = words[i + used] if i + used < len(words) else None
        if follower is not None and follower not in _UNITS and follower not in FILLER and follower not in _SERVING_NOUNS:
            out.update(range(i, i + used))
        i += used
    return out


def _bases(word: str) -> set[str]:
    """Possible base forms of a word: itself, without an inflection or derivation suffix (with the
    final "e" restored and a doubled consonant undone), and without a final "e"."""
    out = {word}
    if word.endswith("e") and len(word) - 1 >= _MIN_BASE:
        out.add(word[:-1])
    for suffix in _FORM_SUFFIXES:
        if not word.endswith(suffix) or len(word) - len(suffix) < _MIN_BASE - 1:
            continue
        stem = word[: -len(suffix)]
        candidates = {stem}
        if suffix in ("ing", "ed"):
            candidates.add(stem + "e")
        if suffix != "ment" and len(stem) >= 2 and stem[-1] == stem[-2] and stem[-1] not in "aeiouls":
            candidates.add(stem[:-1])
        out |= {c for c in candidates if len(c) >= _MIN_BASE}
    return out


def _one_edit(a: str, b: str) -> bool:
    """True when `a` and `b` differ by one insertion, deletion, substitution or adjacent swap."""
    if a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diffs = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diffs) == 1:
            return True
        return len(diffs) == 2 and diffs[1] == diffs[0] + 1 and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]]
    short, long = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(short) and short[i] == long[i]:
        i += 1
    return short[i:] == long[i + 1 :]


def _other_form(a: str, b: str) -> bool:
    """True when one word is the other plus a final letter ("brown" / "browny", "bake" / "baker"):
    another word, not a misspelling. A doubled final letter ("drywal" / "drywall") is a misspelling."""
    short, long = (a, b) if len(a) < len(b) else (b, a)
    return len(long) == len(short) + 1 and long.startswith(short) and long[-1] != short[-1]


@dataclass(frozen=True)
class Vocabulary:
    """The catalog's words (every item's vocabulary), indexed by base form for folding."""

    words: frozenset[str]
    by_base: Mapping[str, frozenset[str]]

    @classmethod
    def of(cls, words: Iterable[str]) -> Vocabulary:
        known = frozenset(w for w in words if w and w not in FILLER)
        index: dict[str, set[str]] = {}
        for word in known:
            if word.isdigit():
                continue
            for base in _bases(word):
                index.setdefault(base, set()).add(word)
        return cls(words=known, by_base={k: frozenset(v) for k, v in index.items()})

    def forms(self, word: str) -> frozenset[str]:
        """Catalog words `word` is a form of ("clogging" -> {"clog", "clogged"}), itself excluded."""
        out: set[str] = set()
        if word in _DIGIT_WORDS:
            out.add(_DIGIT_WORDS[word])
        elif word in _NUMBER_WORDS or word in _TENS:
            value = _NUMBER_WORDS.get(word, _TENS.get(word))
            if word not in ("dozen", "hundred"):
                out.add(str(value))
        if not word.isdigit():
            for base in _bases(word):
                out |= self.by_base.get(base, frozenset())
        out.discard(word)
        return frozenset(out)

    def corrections(self, word: str) -> frozenset[str]:
        """Catalog words of `_MIN_TYPO_LENGTH` letters or more (one more for a changed or swapped letter)
        that `word` misspells by one letter."""
        if word.isdigit() or len(word) < _MIN_TYPO_LENGTH - 1:
            return frozenset()
        return frozenset(
            v
            for v in self.words
            if len(v) >= _MIN_TYPO_LENGTH + (len(v) == len(word)) and not v.isdigit() and _one_edit(word, v) and not _other_form(word, v)
        )


def parse(text: str, vocabulary: Vocabulary | None = None, *, corrections: bool = True) -> Phrase:
    """Read `text` (see module docstring). Without a `vocabulary` only modifiers, filler and content are
    told apart; with one, content words are also read as forms (or, with `corrections`, misspellings)
    of catalog words, and quantities and sizes are found."""
    words = tokens(text)
    positions: set[int] = set()
    dietary: dict[str, frozenset[str]] = {}
    for tag, phrases in search.DIETARY_PHRASES.items():
        for phrase in phrases:
            needle = tokens(phrase)
            for i in range(len(words) - len(needle) + 1):
                if needle and words[i : i + len(needle)] == needle:
                    dietary.setdefault(tag, frozenset(needle))
                    positions.update(range(i, i + len(needle)))
    servings, used = _servings(words)
    positions |= used
    if vocabulary is None:
        return Phrase(text=text, tokens=words, dietary=dietary, servings=servings, modifier_positions=frozenset(positions))
    measure = _measure(words, positions)
    taken = positions | set(measure.positions if measure else ())
    quantities = _quantities(words, taken)
    aliases: dict[str, frozenset[str]] = {}
    typos: set[str] = set()
    for i, word in enumerate(words):
        if i in positions or word in FILLER or word in aliases:
            continue
        forms = vocabulary.forms(word)
        if corrections and word not in vocabulary.words and not forms & vocabulary.words:
            corrected = vocabulary.corrections(word)
            if corrected:
                typos.add(word)
                forms |= corrected
        if forms:
            aliases[word] = forms
    return Phrase(
        text=text,
        tokens=words,
        dietary=dietary,
        servings=servings,
        modifier_positions=frozenset(positions),
        aliases=aliases,
        typos=frozenset(typos),
        quantity_positions=frozenset(quantities),
        measure=measure,
    )


# ---------------------------------------------------------------------------- per-item evaluation


def terms(obj: JsonObj) -> list[str]:
    """Name, spoken name and synonyms of an item or option choice."""
    return [str(t) for t in (obj.get("name"), obj.get("spoken_name"), *(obj.get("synonyms") or ())) if t]


def _groups(item: JsonObj) -> list[JsonObj]:
    return list(item.get("options") or ())


def _category_words(profile: Profile, entry: CatalogItem) -> set[str]:
    wanted = {str(c) for c in entry.item.get("category_ids") or ()}
    if not wanted:
        return set()
    try:
        categories = profile.module(entry.module_id).get("categories") or ()
    except Exception:  # noqa: BLE001 - unknown module: no categories
        return set()
    out: set[str] = set()
    for category in categories:
        if str(category.get("id")) in wanted:
            for name in (category.get("name"), category.get("spoken_name"), *(category.get("synonyms") or ())):
                if name:
                    out.update(tokens(str(name)))
    return out


def _vocabulary(profile: Profile, entry: CatalogItem) -> set[str]:
    words: set[str] = set()
    for term in terms(entry.item):
        words.update(tokens(term))
    for group in _groups(entry.item):
        for choice in group.get("choices") or ():
            for term in terms(choice):
                words.update(tokens(term))
    words |= _category_words(profile, entry)
    return {w for w in words if w not in FILLER}


def catalog_vocabulary(profile: Profile, entries: Iterable[CatalogItem]) -> Vocabulary:
    """Every word of every entry's vocabulary (what customer words are folded onto)."""
    return Vocabulary.of(w for entry in entries for w in _vocabulary(profile, entry))


def _statable_tags(profile: Profile, ref: str) -> set[str] | None:
    got = grounding.state(profile, f"{ref}#dietary.tags")
    return {str(t) for t in got.value or ()} if got.ok else None


def _statable_serves(profile: Profile, ref: str) -> int | None:
    got = grounding.state(profile, f"{ref}#attributes.serves")
    return got.value if got.ok and isinstance(got.value, int) and not isinstance(got.value, bool) else None


def choice_ref(entry: CatalogItem, group: JsonObj, choice: JsonObj) -> str:
    return f"{entry.ref}/{group['id']}/{choice['id']}"


_RANGE_DASH = re.compile(r"(\d)\s*[-‐‑‒–—]\s*(\d)")


def _size_range(choice: JsonObj) -> tuple[float, float] | None:
    """The size range (in inches) a choice publishes in its spoken name or name: "up to 6 inches",
    "up to 2 by 2 feet", "56 to 85 inches", "56–85 in"."""
    for text in (choice.get("spoken_name"), choice.get("name")):
        if not text:
            continue
        raw = _RANGE_DASH.sub(r"\1 to \2", str(text)).replace("×", " by ")
        words = tokens(raw)
        for i in range(len(words)):
            upto = words[i : i + 2] == ("up", "to")
            start = i + 2 if upto else i
            first = _number_at(words, start)
            if first is None or (not upto and i > 0 and words[i - 1] == "to"):
                continue
            end = start + first[1]
            values = [float(first[0])]
            low = 0.0
            if words[end : end + 1] in (("by",), ("x",)):
                second = _number_at(words, end + 1)
                if second is None:
                    continue
                values.append(float(second[0]))
                end = end + 1 + second[1]
            elif not upto and words[end : end + 1] == ("to",):
                second = _number_at(words, end + 1)
                if second is None:
                    continue
                low, values = values[0], [float(second[0])]
                end = end + 1 + second[1]
            elif not upto:
                continue
            unit = words[end] if end < len(words) else None
            if unit in _LENGTH_UNITS:
                factor = _LENGTH_UNITS[unit]
                return low * factor, max(values) * factor
    return None


def _measured_choices(group: JsonObj, measure: Measure) -> list[str]:
    """The choices of `group` whose published size range holds the measure (the tightest ones)."""
    fitting: list[tuple[float, str]] = []
    for choice in group.get("choices") or ():
        bounds = _size_range(choice)
        if bounds is not None and bounds[0] <= measure.inches <= bounds[1]:
            fitting.append((bounds[1], str(choice["id"])))
    if not fitting:
        return []
    tightest = min(upper for upper, _id in fitting)
    return [choice_id for upper, choice_id in fitting if upper == tightest]


def _named_choices(entry: CatalogItem, phrase: Phrase) -> tuple[dict[str, list[str]], set[str], int]:
    """Choices whose term the phrase contains, leaving out words of the item's own names (longest per
    group; ties keep all), or whose published size range holds the phrase's size, the words they
    explain and the summed length of the best term per group."""
    named: dict[str, list[str]] = {}
    explained: set[str] = set()
    score = 0
    words = phrase.words
    own = {w for term in terms(entry.item) for w in tokens(term)}
    for group in _groups(entry.item):
        best = 0
        hits: list[str] = []
        used: set[str] = set()
        for choice in group.get("choices") or ():
            length = 0
            for term in terms(choice):
                term_words = tokens(term)
                key = _key(term_words)
                # The item's own words are implied inside it: the drain cleaning's "sink drain" is the sink.
                for needed in (key, key - own):
                    if term_words and needed and needed <= words:
                        length = max(length, len(" ".join(w for w in term_words if w in needed)))
                        used |= needed
                        break
            if length > best:
                best, hits = length, [str(choice["id"])]
            elif length and length == best:
                hits.append(str(choice["id"]))
        if not hits and phrase.measure is not None:
            hits = _measured_choices(group, phrase.measure)
            if hits:
                used = set(phrase.measure.words)
                best = len(" ".join(phrase.tokens[i] for i in sorted(phrase.measure.positions)))
        if hits:
            named[str(group["id"])] = hits
            explained |= used
            score += best
    return named, explained, score


def _dietary_focus(
    profile: Profile, entry: CatalogItem, wanted: dict[str, frozenset[str]], named_by_item: set[str]
) -> tuple[tuple[str, ...], dict[str, list[str]], tuple[str, ...]]:
    """(missing tags, {group: choices that carry every remaining tag}, tags those choices carry)."""
    if not wanted:
        return (), {}, ()
    item_tags = _statable_tags(profile, entry.ref) or set()
    remaining = tuple(
        tag for tag, said in wanted.items() if tag not in item_tags and not said <= named_by_item
    )
    if not remaining:
        return (), {}, ()
    focus: dict[str, list[str]] = {}
    for group in _groups(entry.item):
        for choice in group.get("choices") or ():
            tags = _statable_tags(profile, choice_ref(entry, group, choice))
            if tags is not None and set(remaining) <= tags:
                focus.setdefault(str(group["id"]), []).append(str(choice["id"]))
    if focus:
        return (), focus, remaining
    return remaining, {}, ()


def _servings_focus(profile: Profile, entry: CatalogItem, count: int | None) -> tuple[bool | None, dict[str, list[str]]]:
    """(met?, {group: choices serving at least `count`}). met is None when nothing says how many it serves."""
    if count is None:
        return None, {}
    own = _statable_serves(profile, entry.ref)
    if own is not None and own >= count:
        return True, {}
    focus: dict[str, list[str]] = {}
    known = own is not None
    for group in _groups(entry.item):
        for choice in group.get("choices") or ():
            serves = _statable_serves(profile, choice_ref(entry, group, choice))
            if serves is None:
                continue
            known = True
            if serves >= count:
                focus.setdefault(str(group["id"]), []).append(str(choice["id"]))
    if focus:
        return True, focus
    return (False if known else None), {}


def _merge_focus(
    entry: CatalogItem,
    named: dict[str, list[str]],
    dietary: dict[str, list[str]],
    dietary_tags: tuple[str, ...],
    servings: dict[str, list[str]],
) -> tuple[Focus, ...]:
    """One `Focus` per group, in option-group order. Constraints narrow what was named; a constraint the
    named choices cannot meet wins (it is a safety or size requirement), and the reason says which."""
    out: list[Focus] = []
    for group in _groups(entry.item):
        gid = str(group["id"])
        order = [str(c["id"]) for c in group.get("choices") or ()]
        chosen: list[str] = list(named.get(gid, ()))
        reason: FocusReason = "named"
        tags: tuple[str, ...] = ()
        for constraint, why in ((dietary.get(gid), "dietary"), (servings.get(gid), "servings")):
            if not constraint:
                continue
            both = [c for c in chosen if c in constraint]
            if both:
                chosen = both
            else:
                chosen, reason = list(constraint), why  # type: ignore[assignment]
            if why == "dietary":
                tags = dietary_tags
        if chosen:
            out.append(Focus(group_id=gid, choice_ids=tuple(sorted(set(chosen), key=order.index)), reason=reason, tags=tags))
    return tuple(out)


def evaluate(profile: Profile, entry: CatalogItem, phrase: Phrase) -> Match | None:
    """How well `entry` explains `phrase` (see module docstring), or None if it does not match at all."""
    words = phrase.words
    named, choice_words, choice_score = _named_choices(entry, phrase)
    item_words: set[str] = set()
    full_score = 0
    for term in terms(entry.item):
        term_words = tokens(term)
        if term_words and _key(term_words) <= words:
            item_words |= set(term_words)
            full_score = max(full_score, len(" ".join(term_words)))
    content = phrase.content
    matched = choice_words | item_words
    # "3 chairs", "two dressers": the count needs no explaining (an item term may still name it).
    rest = [w for w in content if not phrase.forms(w) & matched and w not in phrase.quantities]
    vocabulary = _vocabulary(profile, entry)
    head = phrase.head
    marked = phrase.marked_head
    head_rank = 0 if marked is None or phrase.forms(marked) & (matched | vocabulary) else 1
    kind: Kind
    left: list[str] = []
    if full_score:
        kind, score = 0, full_score + choice_score
        left = list(dict.fromkeys(w for w in rest if not phrase.forms(w) & vocabulary))
    else:
        if rest:
            if any(all(phrase.forms(w) & _key(tokens(term)) for w in rest) for term in terms(entry.item)):
                kind = 1
            elif all(phrase.forms(w) & vocabulary for w in rest):
                kind = 2
            else:
                # Words left over only beside an option choice the phrase names ("clogged kitchen sink":
                # the sink drain), so loose vocabulary never borrows an item ("hair cut for my son").
                left = list(dict.fromkeys(w for w in rest if not phrase.forms(w) & vocabulary))
                explained = [w for w in content if w not in left and w not in phrase.quantities]
                head_explained = head is not None and bool(phrase.forms(head) & (matched | vocabulary))
                if not named or len(explained) < 2 or len(explained) <= len(left) or not head_explained:
                    return None
                kind = 2
            score = len(" ".join(rest)) + choice_score
        elif content and named:
            kind, score = 3, choice_score
        elif not content and (phrase.dietary or phrase.servings is not None):
            kind, score = 4, choice_score
        else:
            return None
    missing, dietary_focus, tags = _dietary_focus(profile, entry, phrase.dietary, item_words)
    servings_met, servings_focus = _servings_focus(profile, entry, phrase.servings)
    if kind == 4 and (missing or servings_met is not True and phrase.servings is not None):
        return None  # a bare "gluten-free" or "for 30" only lists items that meet it
    return Match(
        entry=entry,
        kind=kind,
        score=score,
        unexplained=len(left),
        missing_tags=missing,
        servings_met=servings_met,
        servings_rank=0 if phrase.servings is None or servings_met else 1,
        focus=_merge_focus(entry, named, dietary_focus, tags, servings_focus),
        head_rank=head_rank,
        left=tuple(left),
    )


# ---------------------------------------------------------------------------- resolution


def _exact(entries: Sequence[CatalogItem], text: str) -> ItemLookup | None:
    wanted = text.strip()
    for entry in entries:
        if wanted in (entry.ref, entry.item.get("id")):
            return ItemLookup(found=entry)
    query = search.normalize(wanted)
    if not query:
        return ItemLookup(found=None)
    exact = [e for e in entries if any(search.normalize(t) == query for t in terms(e.item))]
    if len(exact) == 1:
        return ItemLookup(found=exact[0])
    if exact:
        return ItemLookup(found=None, candidates=exact[:MAX_CANDIDATES])
    return None


def best_matches(matches: Iterable[Match]) -> list[Match]:
    """The matches that rank best (see module docstring), in catalog order."""
    ranked = list(matches)
    if not ranked:
        return []
    top = min(m.rank() for m in ranked)
    winners = [m for m in ranked if m.rank() == top]
    if len(winners) > 1 and winners[0].unexplained == 0:
        longest = max(m.score for m in winners)
        winners = [m for m in winners if m.score == longest]
    return winners


def resolve(profile: Profile, entries: Sequence[CatalogItem], text: str) -> ItemLookup:
    """Resolve `text` among `entries` (ref or id, exact term, then the scored match). A misspelt word
    is only corrected when the words as said find nothing, and what that finds is a suggestion."""
    exact = _exact(entries, text)
    vocabulary = catalog_vocabulary(profile, entries)
    phrase = parse(text, vocabulary)
    if exact is not None:
        if exact.found is not None and exact.found.ref != text.strip() and exact.found.item.get("id") != text.strip():
            # An exact term can still name an option choice ("clogged toilet", "tankless").
            match = evaluate(profile, exact.found, phrase)
            focus = match.focus if match is not None else ()
            return ItemLookup(found=exact.found, focus=focus, dietary=tuple(phrase.dietary))
        return exact
    if not phrase.typos:
        return _scored(profile, entries, phrase)
    as_said = _scored(profile, entries, parse(text, vocabulary, corrections=False))
    if as_said.found is not None or as_said.candidates:
        return as_said
    corrected = _scored(profile, entries, phrase)
    if corrected.found is not None:
        return ItemLookup(found=None, candidates=[corrected.found], dietary=corrected.dietary, servings=corrected.servings, corrected=True)
    return ItemLookup(
        found=None,
        candidates=corrected.candidates,
        dietary=corrected.dietary,
        unmet_dietary=corrected.unmet_dietary,
        servings=corrected.servings,
        corrected=bool(corrected.candidates),
    )


def _scored(profile: Profile, entries: Sequence[CatalogItem], phrase: Phrase) -> ItemLookup:
    dietary = tuple(phrase.dietary)
    matches = [m for m in (evaluate(profile, entry, phrase) for entry in entries) if m is not None]
    winners = best_matches(matches)
    if not winners:
        return ItemLookup(found=None, dietary=dietary, servings=phrase.servings)
    if winners[0].missing_tags:
        return ItemLookup(
            found=None,
            candidates=[m.entry for m in winners[:MAX_CANDIDATES]],
            dietary=dietary,
            unmet_dietary=tuple(dict.fromkeys(tag for m in winners for tag in m.missing_tags)),
            servings=phrase.servings,
        )
    if len(winners) > 1:
        return ItemLookup(
            found=None,
            candidates=[m.entry for m in winners[:MAX_CANDIDATES]],
            dietary=dietary,
            servings=phrase.servings,
            unexplained=tuple(dict.fromkeys(w for m in winners for w in m.left)),
        )
    only = winners[0]
    return ItemLookup(
        found=only.entry,
        focus=only.focus,
        dietary=dietary,
        servings=phrase.servings,
        servings_met=only.servings_met,
        unexplained=only.left,
    )


def matching(profile: Profile, entries: Sequence[CatalogItem], text: str) -> list[CatalogItem]:
    """Every entry `text` describes, for browsing (`get_catalog(query=...)`), in catalog order.

    Entries must meet every dietary tag asked for; when some entry meets a head count, only those
    that do are kept; and entries that explain every word win over ones that leave words over.
    """
    phrase = parse(text, catalog_vocabulary(profile, entries))
    if not phrase.tokens:
        return []
    matches = [m for m in (evaluate(profile, entry, phrase) for entry in entries) if m is not None and not m.missing_tags]
    if phrase.servings is not None and any(m.servings_met for m in matches):
        matches = [m for m in matches if m.servings_met]
    if not matches:
        return []
    fewest = min(m.unexplained for m in matches)
    return [m.entry for m in matches if m.unexplained == fewest]
