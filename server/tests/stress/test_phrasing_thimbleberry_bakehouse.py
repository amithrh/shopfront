"""Phrasing stress test for Thimbleberry Bakehouse (bakery: breads, pastries, cookies, case cakes and
custom cakes with sizes, flavors, fillings, frostings, gluten-free and vegan options, allergens).

Every case is something a voice assistant would plausibly pass straight through from a customer:
item names with modifiers and quantities, plurals, capitals, ASR word splits and misspellings,
colloquial words, sizes and head counts, dietary needs, price, policy, hours and availability
questions, custom-cake orders, and out-of-scope asks. Each case asserts the RIGHT behaviour per the
gold profile (profiles/thimbleberry-bakehouse.json, served as a live copy through the real loader and
the in-process MCP client from tests/conftest.py):

  * the correct item (and option choices) resolved, or an honest "Did you mean ...?";
  * the correct grounded fact (price, allergens, hours, policy answer verbatim from the profile);
  * an honest hand-off ("... Want me to ask Rosa?") only when the profile truly does not say;
  * never a wrong item, a wrong policy entry, or an invented fact.

Every result also passes `assert_grounded` (speak is spoken-safe, every cited fact is statable) and,
for catalog answers, every spoken dollar amount equals a cited fact value.

Known failures are marked `xfail(strict=True)` with the precise defect, so the suite stays green
until a fix lands (a fix turns the case into an XPASS, which fails the run: remove the mark then).
Product code is not changed here.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from support import ElicitationScript, load_raw_profiles

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))  # `tools_support` (importlib mode)
from tools_support import assert_grounded, structured  # noqa: E402

pytestmark = pytest.mark.anyio

BAKERY = "thimbleberry-bakehouse"
KITCHEN = "The kitchen also handles wheat, milk, eggs, soy, sesame, tree nuts and peanuts"
ASK_ROSA = "Want me to ask Rosa?"
NOT_SEEN = "I don't see that at Thimbleberry Bakehouse. Want me to ask Rosa?"
DONT_KNOW = "I don't know that. Want me to ask Rosa?"

GOLD = load_raw_profiles()[BAKERY]
ANSWERS: dict[str, str] = {f"info/{e['id']}": e["answer_spoken"] for e in GOLD["modules"]["info"]["entries"]}
"""Policy answers exactly as published: ask_policy must speak them verbatim."""

GF_FLAVORS = ("vanilla-bean-gf", "dark-chocolate-gf", "lemon-poppyseed-gf")
VEGAN_FLAVORS = ("vanilla-bean-vegan", "dark-chocolate-vegan")
MILK_COOKIES = ("brown butter chocolate chip cookie", "oatmeal raisin cookie", "peanut butter cookie")
"""Cookies whose owner-confirmed allergens include milk AND eggs."""


def xfail(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, reason=reason)


def case_id(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


async def call(client: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return structured(await client.call_tool(tool, {"business_id": BAKERY, **args} if tool != "find_business" else args))


_DOLLARS = re.compile(r"\$(\d+(?:\.\d\d)?)")


def _cents(value: Any) -> Iterable[int]:
    if isinstance(value, dict):
        for key, inner in value.items():
            if key in ("amount_cents", "max_cents") and isinstance(inner, int):
                yield inner
            else:
                yield from _cents(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _cents(inner)


def assert_amounts_cited(data: dict[str, Any]) -> None:
    """Every '$N' spoken is the amount of a fact the result cites (no arithmetic, no invented price)."""
    cited = {c for fact in data.get("facts") or () for c in _cents(fact.get("value"))}
    for amount in _DOLLARS.findall(data["speak"]):
        cents = round(float(amount) * 100)
        assert cents in cited, f"${amount} spoken but not a cited fact value: {sorted(cited)} / {data['speak']!r}"


# ============================================================================ find_item


ANY = object()


@dataclass(frozen=True)
class Item:
    """What find_item must do for one phrasing."""

    ref: str | None = None
    """The item it must resolve to (None: it must not resolve to any single item)."""
    focus: dict[str, tuple[str, ...]] | None = None
    """matched_options it must return ({group: choices}); None = not checked."""
    says: tuple[str, ...] = ()
    never: tuple[str, ...] = ()
    handoff: Any = ANY
    """Expected handoff reason (None = no hand-off); ANY = not checked."""
    ask: str | None = None
    """An honest ambiguity: the exact 'Did you mean ...?' sentence."""
    not_offered: bool = False


def check_item(data: dict[str, Any], want: Item) -> None:
    speak = data["speak"]
    found = (data.get("item") or {}).get("item_ref")
    if want.not_offered:
        assert speak == NOT_SEEN, speak
        assert data["handoff"]["reason"] == "not_offered" and found is None
        return
    if want.ask is not None:
        assert speak == want.ask and found is None, speak
        return
    if want.ref is not None:
        assert found == want.ref, (found, speak)
        assert not speak.startswith("I don't see that"), speak
    if want.focus is not None:
        got = {g["group_id"]: tuple(c["choice_id"] for c in g["choices"]) for g in data.get("matched_options") or ()}
        assert got == want.focus, got
    for text in want.says:
        assert text in speak, (text, speak)
    for text in want.never:
        assert text not in speak, (text, speak)
    if want.handoff is not ANY:
        assert (data["handoff"] or {}).get("reason") == want.handoff, (data["handoff"], speak)


ITEMS: list[Any] = [
    # ---- item names, plurals, capitals, synonyms, quantities and filler
    ("a loaf of sourdough", None, Item(ref="menu/country-sourdough", says=("$9",), handoff=None)),
    ("pain au chocolat", None, Item(ref="menu/chocolate-croissant", says=("$4.75",))),
    ("almond croissants", ["price"], Item(ref="menu/almond-croissant", says=("$5.25",))),
    ("morning buns", None, Item(ref="menu/orange-cardamom-morning-bun", says=("$4.50",))),
    ("french bread", None, Item(ref="menu/baguette", says=("$4.50",))),
    ("olive bread", None, Item(ref="menu/olive-rosemary-loaf", says=("$10.50",))),
    ("rye bread", None, Item(ref="menu/seeded-rye", says=("$10",))),
    ("egg bread", None, Item(ref="menu/challah", says=("$11",))),
    ("chocolate chip cookies", None, Item(ref="menu/brown-butter-chocolate-chip", says=("$3.25",))),
    ("ginger cookies", ["price"], Item(ref="menu/ginger-molasses", says=("$3",))),
    ("three baguettes", None, Item(ref="menu/baguette", says=("$4.50",), never=("$13.50",))),
    ("a couple of croissants", None, Item(ref="menu/butter-croissant", says=("$4.25",))),
    ("scones", None, Item(ref="menu/cheddar-chive-scone", says=("$4",))),
    ("a fruit pie", ["price", "description"], Item(ref="menu/seasonal-fruit-hand-pie", says=("$5.50", "changes with the season"))),
    ("something with blueberries", None, Item(ref="menu/vegan-blueberry-muffin", says=("$3.75",))),
    ("slice of cake", ["price", "description"], Item(ref="menu/slice-of-cake", says=("$6.50 per slice", "changes daily"))),
    ("a dozen cookies", ["price", "description"], Item(ref="menu/cookie-dozen", says=("$33", "Any 12 cookies"))),
    # A half dozen is not on the menu: the dozen price is fine, an invented half-dozen price is not.
    ("half dozen cookies", None, Item(ref="menu/cookie-dozen", says=("$33 per dozen",), never=("$16.50", "half"))),
    ("a loaf of bread", None, Item(ask="Did you mean country sourdough, olive and rosemary loaf or gluten-free sandwich loaf?")),
    # ---- custom cake: flavors, sizes, head counts named in the item phrase
    ("funfetti cake", None, Item(ref="menu/custom-cake", focus={"flavor": ("birthday-sprinkle",)}, says=("birthday sprinkle",))),
    ("lemon cake", None, Item(ref="menu/custom-cake", focus={"flavor": ("lemon-poppyseed",)}, says=("lemon poppyseed",))),
    ("a vanilla cake for my son's birthday", None, Item(ref="menu/custom-cake", focus={"flavor": ("vanilla-bean",)})),
    ("an 8 inch birthday cake", ["price"], Item(ref="menu/custom-cake", focus={"size": ("8in-round",)}, says=("eight inch round for $84",))),
    ("two tier wedding cake", ["price"], Item(ref="menu/custom-cake", focus={"size": ("two-tier",)}, says=("two tier cake for $210",))),
    ("three tier cake", ["price"], Item(ref="menu/custom-cake", focus={"size": ("three-tier",)}, says=("three tier cake for $320",))),
    (
        "a sheet cake for 40 people", ["price"],
        Item(
            ref="menu/custom-cake", focus={"size": ("half-sheet", "two-tier", "three-tier")},
            says=("half sheet, which serves up to 48 for $135",), never=("40",),
        ),
    ),
    (
        "wedding cake for 150 guests", ["price"],
        Item(ref="menu/custom-cake", says=("three tier cake, which serves up to 72",), never=("150",)),
    ),
    ("cake for 12", None, Item(ask="Did you mean six inch case cake or custom cake?")),
    (
        "10 inch chocolate cake with ganache", ["price"],
        Item(
            ref="menu/custom-cake",
            focus={"size": ("10in-round",), "flavor": ("dark-chocolate",), "filling": ("chocolate-ganache",)},
            says=("ten inch round for $120",),
        ),
    ),
    # ---- dietary needs and allergens (owner-confirmed only, always with the kitchen statement)
    (
        "gluten free bread", ["dietary", "allergens"],
        Item(ref="menu/gluten-free-sandwich-loaf", says=("contains eggs", KITCHEN, "is gluten-free"), handoff=None),
    ),
    ("vegan muffin", None, Item(ref="menu/vegan-blueberry-muffin", says=("is vegan", "$3.75"))),
    ("a blueberry muffin", ["dietary"], Item(ref="menu/vegan-blueberry-muffin", says=("is vegan",))),
    ("is the sourdough vegan", ["dietary"], Item(ref="menu/country-sourdough", says=("The country sourdough is vegan",))),
    (
        "challah", ["dietary", "allergens"],
        # No dietary tags in the profile: the allergens are stated, the dietary label is a hand-off.
        Item(ref="menu/challah", says=("contains wheat, eggs and sesame", KITCHEN, ASK_ROSA), never=("is vegan",), handoff="unknown"),
    ),
    (
        "vegan birthday cake", ["price"],
        Item(ref="menu/custom-cake", focus={"flavor": VEGAN_FLAVORS}, says=("vegan cake flavors for $10 more per tier",)),
    ),
    (
        "vegetarian scone", None,
        Item(says=("I don't know whether the cheddar chive scone comes vegetarian", ASK_ROSA), never=("is vegetarian",), handoff="unknown"),
    ),
    ("cheese scone", ["allergens"], Item(ref="menu/cheddar-chive-scone", says=("contains wheat, milk and eggs", KITCHEN))),
    ("peanut butter cookies", ["allergens"], Item(ref="menu/peanut-butter-cookie", says=("wheat, milk, eggs and peanuts", KITCHEN))),
    ("a croissant with almonds", ["allergens"], Item(ref="menu/almond-croissant", says=("tree nuts", KITCHEN))),
    ("Chocolate Croissants", ["allergens"], Item(ref="menu/chocolate-croissant", says=("contains wheat, milk, eggs and soy", KITCHEN))),
    (
        "flourless chocolate cookie", ["allergens", "dietary"],
        Item(ref="menu/flourless-chocolate-crinkle", says=("contains eggs and soy", KITCHEN, "is gluten-free")),
    ),
    (
        "hazelnut cake", ["allergens"],
        Item(ref="menu/custom-cake", focus={"flavor": ("hazelnut-praline",)}, says=("hazelnut praline contains wheat, milk, eggs and tree nuts", KITCHEN)),
    ),
    (
        "carrot cake with cream cheese frosting", ["allergens"],
        Item(ref="menu/custom-cake", focus={"flavor": ("carrot-walnut",), "frosting": ("cream-cheese",)}, says=("tree nuts", KITCHEN)),
    ),
    # ---- availability notes
    ("challah", ["availability"], Item(ref="menu/challah", says=("Challah is baked on Fridays and Saturdays only.",))),
    ("baguettes", ["availability"], Item(ref="menu/baguette", says=("out of the oven by 10 am",))),
    ("case cake", ["availability"], Item(ref="menu/case-cake-6in", says=("They can't be reserved.",))),
    # ---- not on the menu: an honest "I don't see that", never a look-alike item
    ("focaccia", None, Item(not_offered=True)),
    ("cupcakes", None, Item(not_offered=True)),
    ("bagels", None, Item(not_offered=True)),
    ("brownies", None, Item(not_offered=True)),
    ("pumpkin pie", None, Item(not_offered=True)),
    ("cinnamon roll", None, Item(not_offered=True)),
    ("a latte", None, Item(not_offered=True)),
]

ITEM_XFAILS: list[Any] = [
    # ---- misspellings / ASR splits / colloquial words: the item exists, yet "I don't see that"
    pytest.param(
        "croisant", None, Item(ref="menu/butter-croissant"),
        marks=xfail("'croisant' is now 'Did you mean butter croissant?' (a corrected misspelling is confirmed, never silently picked); this case expects the item itself"),
        id="misspelled-croisant",
    ),
    pytest.param(
        "sour dough bread", None, Item(ref="menu/country-sourdough"),
        marks=xfail("ASR word split 'sour dough' is not joined: -> 'I don't see that' although sourdough is on the menu"),
        id="asr-split-sour-dough",
    ),
    pytest.param(
        "chalah", None, Item(ref="menu/challah"),
        marks=xfail("'chalah' is now 'Did you mean challah?' (a corrected misspelling is confirmed, never silently picked); this case expects the item itself"),
        id="misspelled-chalah",
    ),
    pytest.param(
        "choc chip cookies", None, Item(ref="menu/brown-butter-chocolate-chip"),
        marks=xfail(
            "colloquial 'choc chip' not understood: -> 'I don't see that' (false 'not offered'). 'choc' is no form "
            "of 'chocolate', and a word is only left over beside an option choice the phrase names (the cookie has none)"
        ),
        id="colloquial-choc-chip",
    ),
    pytest.param(
        "12 cookies", None, Item(ref="menu/cookie-dozen", says=("$33",)),
        id="asr-digits-12-cookies",  # '12' stands for 'twelve', so the synonym 'twelve cookies' matches
    ),
    pytest.param(
        "wheat free cookie", None, Item(ref="menu/flourless-chocolate-crinkle"),
        marks=xfail(
            "'wheat free' is not a dietary phrase: -> 'I don't see that' although the flourless chocolate crinkle "
            "is gluten-free and its confirmed allergens are only eggs and soy"
        ),
        id="wheat-free-cookie",
    ),
    # ---- head counts nobody (or only the custom cake) can serve
    pytest.param(
        "a cake for 100 people", ["price"],
        Item(ref="menu/custom-cake", says=("three tier cake, which serves up to 72",), never=("slice of cake",)),
        marks=xfail(
            "no item serves 100, so every cake ties and it asks 'Did you mean slice of cake, six inch case cake or "
            "custom cake?' (a slice for 100 people); 'wedding cake for 150 guests' correctly says the biggest serves 72"
        ),
        id="cake-for-100-people",
    ),
    pytest.param(
        "a big cake for a party of 60", ["price"], Item(ref="menu/custom-cake", never=("I don't see that",)),
        marks=xfail("'party of 60' is not read as a head count and 'big'/'party' are unexplained: -> 'I don't see that' (false)"),
        id="party-of-60",
    ),
    # ---- dietary needs the profile DOES answer (or must caveat), handled as unknown or unsafe
    pytest.param(
        "celiac safe bread", None, Item(ref="menu/gluten-free-sandwich-loaf", says=("celiac",)),
        marks=xfail(
            "SAFETY: 'celiac' maps to gluten_free and the answer is 'The gluten-free sandwich loaf is gluten-free. ... $12.' "
            "without the profile's 'we don't recommend them for people with celiac disease' (recommended_for_celiac=false)"
        ),
        id="celiac-safe-bread",
    ),
    pytest.param(
        "dairy free cookie", None, Item(never=MILK_COOKIES),
        marks=xfail(
            "offers cookies whose owner-confirmed allergens include milk as maybe dairy-free ('I don't know whether the "
            "brown butter chocolate chip cookie, oatmeal raisin cookie or peanut butter cookie comes dairy-free'); the "
            "vegan ginger molasses cookie is never mentioned"
        ),
        id="dairy-free-cookie",
    ),
    pytest.param(
        "egg free cookie", None, Item(never=MILK_COOKIES),
        marks=xfail(
            "offers cookies whose owner-confirmed allergens include eggs as maybe egg-free ('I don't know whether the "
            "brown butter chocolate chip cookie, oatmeal raisin cookie or peanut butter cookie comes egg-free')"
        ),
        id="egg-free-cookie",
    ),
    pytest.param(
        "sugar free cake", None, Item(says=("don't make sugar-free cakes",), never=("I don't know",)),
        marks=xfail("profile says 'We don't make sugar-free cakes' (info/not-offered) but find_item says 'I don't know whether ... comes sugar-free'"),
        id="sugar-free-cake",
    ),
    pytest.param(
        "gluten free vegan cake", None, Item(says=("both gluten-free and vegan",), never=("I don't know",)),
        marks=xfail(
            "profile says 'No, we can't make a cake that is both gluten-free and vegan' (info/gluten-free-and-vegan) but "
            "find_item says 'I don't know whether the slice of cake, six inch case cake or custom cake comes gluten-free and vegan'"
        ),
        id="gluten-free-vegan-cake",
    ),
    pytest.param(
        "nut free cookies", None, Item(says=("isn't nut-free",), never=("cookie dozen",)),
        marks=xfail(
            "profile says the kitchen isn't nut-free (info/nut-free) but find_item says 'I don't know whether the cookie "
            "dozen comes nut-free' (and narrows 'cookies' to the cookie dozen only)"
        ),
        id="nut-free-cookies",
    ),
    # ---- speech coherence
    pytest.param(
        "gluten free 8 inch cake", ["price"],
        Item(ref="menu/custom-cake", says=("gluten-free cake flavors for $12 more per tier. They are gluten-free vanilla bean",)),
        marks=xfail(
            "the flavor list is detached from its sentence: 'The custom cake has gluten-free cake flavors ... For the custom "
            "cake, the size can be eight inch round for $84. They are gluten-free vanilla bean, ...' ('They' = sizes?)"
        ),
        id="gluten-free-8-inch-they-are",
    ),
]


@pytest.mark.parametrize(
    ("phrase", "aspects", "want"),
    [pytest.param(*row, id=case_id(f"{row[0]}-{'-'.join(row[1] or [])}")) for row in ITEMS] + ITEM_XFAILS,
)
async def test_find_item_phrasing(client: Any, deps: Any, phrase: str, aspects: list[str] | None, want: Item) -> None:
    args: dict[str, Any] = {"item": phrase}
    if aspects:
        args["aspects"] = aspects
    data = await call(client, "find_item", args)
    check_item(data, want)
    assert_amounts_cited(data)
    await assert_grounded(deps, data)


# ============================================================================ ask_policy


HANDOFF = None
"""Expected entry for an honest hand-off (the profile does not say)."""

POLICIES: list[Any] = [
    # (question, expected entry ref, or HANDOFF, or a set of acceptable outcomes)
    ("can I cancel my order", "info/cancellation"),
    ("can I cancel", "info/cancellation"),
    ("what if I need to cancel", "info/cancellation"),
    ("do you take cards", "info/payment"),
    ("do you take credit cards", "info/payment"),
    ("do you accept checks", "info/payment"),
    ("do you charge sales tax", "info/payment"),
    ("is there parking", "info/parking"),
    ("where can I park my car", "info/parking"),
    ("is there somewhere to park", "info/parking"),
    ("can I bring my dog inside", "info/dogs"),
    ("is it wheelchair accessible", "info/accessibility"),
    ("do you deliver", "info/delivery"),
    ("can you deliver a cake to my office", "info/delivery"),
    ("how far in advance do I need to order a birthday cake", "info/custom-cake-notice"),
    ("can I get a cake by tomorrow", "info/same-day-cake"),
    ("do you sell day old bread", "info/freshness"),
    ("are you open on christmas", "info/holiday-closures"),
    ("do you use fondant", "info/not-offered"),
    ("can you put a picture on the cake", "info/not-offered"),
    ("do you make sugar free cakes", "info/not-offered"),
    ("can you make a cake that's gluten free and vegan", "info/gluten-free-and-vegan"),
    ("is your kitchen peanut free", "info/nut-free"),
    ("do you have nut free stuff", "info/nut-free"),
    ("do you have gluten free stuff", "info/gluten-free-options"),
    ("how much extra is a gluten free cake", "info/gluten-free-cakes"),
    ("what's your phone number", "info/contact"),
    ("how long can the message on the cake be", "info/whats-included"),
    ("what time can I pick up my cake", "info/pickup"),
    ("can I hold a loaf of sourdough for saturday", "info/bread-holds"),
    # Not named in the profile: the payment answer (which lists what is taken) or a hand-off are both honest.
    ("can I pay with apple pay", {"info/payment", HANDOFF}),
    ("do you take venmo", {"info/payment", HANDOFF}),
    # Out of scope: the profile is silent, so ask Rosa (never a neighbouring policy).
    ("do you have a bathroom", HANDOFF),
    ("do you have wifi", HANDOFF),
    ("do you sell coffee", HANDOFF),
    ("can you cater my wedding", HANDOFF),
    ("do you sell gift cards", HANDOFF),
]

POLICY_XFAILS: list[Any] = [
    # Former wrong answers and misses, kept as regression cases. An unrelated policy must not answer an
    # out-of-scope question on one non-distinctive word ('tomorrow', 'card') ...
    pytest.param("what's the weather like tomorrow", HANDOFF, id="weather-tomorrow"),
    pytest.param("do you have a loyalty card", HANDOFF, id="loyalty-card"),
    # ... the question's topic ('deposit') or kind of thing ('bread', not cake) picks the entry ...
    pytest.param("how much is the deposit on a big cake", "info/deposit", id="deposit-on-a-big-cake"),
    pytest.param("is the gluten free bread safe for celiacs", "info/gluten-free-options", id="gluten-free-bread-celiac"),
    # ... and the profile's answer is found through topic words, synonyms and idioms, not handed off.
    pytest.param("what forms of payment do you accept", "info/payment", id="forms-of-payment"),
    pytest.param("what's your refund policy", "info/cancellation", id="refund-policy"),
    pytest.param("do I need to put down a deposit", "info/deposit", id="put-down-a-deposit"),
    pytest.param("if I cancel my birthday cake do I get my money back", "info/cancellation", id="cancel-money-back"),
    pytest.param("who owns the bakery", {"info/team", "info/about"}, id="who-owns-the-bakery"),
    pytest.param(
        "can I change the flavor of my cake order", "info/cancellation",
        id="change-flavor-of-order",  # info/cancellation leaves one word over, info/how-to-order two
    ),
    pytest.param("can I park out front", "info/parking", id="park-out-front"),
]


@pytest.mark.parametrize(
    ("question", "expected"), [pytest.param(*row, id=case_id(row[0])) for row in POLICIES] + POLICY_XFAILS
)
async def test_ask_policy_phrasing(client: Any, deps: Any, question: str, expected: Any) -> None:
    data = await call(client, "ask_policy", {"question": question})
    allowed = expected if isinstance(expected, set) else {expected}
    got = data.get("entry_ref")
    assert got in allowed, (got, data["speak"])
    if got is None:
        assert data["speak"] == DONT_KNOW and data["handoff"]["reason"] == "unknown", data
    else:
        assert data["speak"] == ANSWERS[got], data["speak"]  # verbatim from the profile
        assert data["handoff"] is None
    await assert_grounded(deps, data)


# ============================================================================ hours, location, contact


@pytest.mark.parametrize(
    ("args", "says", "never"),
    [
        # FIXED_NOW is Wednesday 14 Oct 2026, 9:00 am in Portland.
        pytest.param({"topics": ["hours"]}, ("is open now, until 3 pm",), (), id="are-you-open-now"),
        pytest.param({"topics": ["hours"], "on_date": "2026-10-19"}, ("closed on Monday",), ("open",), id="open-on-monday"),
        # The owner settled Saturday at 6 pm (the About page's 4 pm is stale).
        pytest.param({"topics": ["hours"], "on_date": "2026-10-17"}, ("8 am to 6 pm",), ("4 pm",), id="saturday-closing-time"),
        pytest.param({"topics": ["hours"], "on_date": "2026-11-26"}, ("closed", "Thanksgiving Day"), (), id="open-on-thanksgiving"),
        pytest.param({"topics": ["location"]}, ("Thimbleberry Street and 32nd Avenue", "Portland"), (), id="where-are-you"),
        pytest.param({"topics": ["contact"]}, ("503-555-0147",), (), id="phone-number"),
    ],
)
async def test_business_info_phrasing(client: Any, deps: Any, args: dict[str, Any], says: tuple[str, ...], never: tuple[str, ...]) -> None:
    data = await call(client, "get_business_info", args)
    for text in says:
        assert text in data["speak"], data["speak"]
    for text in never:
        assert text not in data["speak"], data["speak"]
    assert data["handoff"] is None
    await assert_grounded(deps, data)


# ============================================================================ browsing


VEGAN_ITEMS = [f"menu/{ref.split('/')[-1]}" for ref in next(e for e in GOLD["modules"]["info"]["entries"] if e["id"] == "vegan-options")["applies_to"]]


@pytest.mark.parametrize(
    ("args", "refs"),
    [
        pytest.param(
            {"category": "pastries", "limit": 25},
            ["menu/butter-croissant", "menu/chocolate-croissant", "menu/almond-croissant", "menu/orange-cardamom-morning-bun",
             "menu/seasonal-fruit-hand-pie", "menu/cheddar-chive-scone", "menu/vegan-blueberry-muffin"],
            id="what-pastries-do-you-have",
        ),
        pytest.param(
            {"category": "bread", "limit": 25},
            ["menu/country-sourdough", "menu/seeded-rye", "menu/olive-rosemary-loaf", "menu/baguette", "menu/challah",
             "menu/gluten-free-sandwich-loaf"],
            id="what-bread-do-you-have",
        ),
        pytest.param({"query": "vegan", "limit": 25}, VEGAN_ITEMS, id="whats-vegan"),
        pytest.param(
            {"query": "cookies", "limit": 25},
            ["menu/brown-butter-chocolate-chip", "menu/oatmeal-raisin", "menu/hazelnut-shortbread", "menu/peanut-butter-cookie",
             "menu/ginger-molasses", "menu/flourless-chocolate-crinkle", "menu/cookie-dozen"],
            id="what-cookies",
        ),
        pytest.param({"category": "cakes", "limit": 25}, ["menu/slice-of-cake", "menu/case-cake-6in", "menu/custom-cake"], id="what-cakes"),
        pytest.param({"query": "pizza"}, [], id="do-you-have-pizza"),
    ],
)
async def test_catalog_phrasing(client: Any, deps: Any, args: dict[str, Any], refs: list[str]) -> None:
    data = await call(client, "get_catalog", args)
    assert [i["item_ref"] for i in data["items"]] == refs, data["speak"]
    if not refs:
        assert data["handoff"]["reason"] == "not_offered" and "couldn't find that" in data["speak"]
    assert_amounts_cited(data)
    await assert_grounded(deps, data)


# ============================================================================ availability


@pytest.mark.parametrize(
    ("args", "item_ref", "days", "handoff"),
    [
        # 72 hours' notice from Wednesday 9 am: Saturday is the first pickup day.
        pytest.param({"item": "birthday cake"}, "menu/custom-cake", ["2026-10-17", "2026-10-18", "2026-10-20"], None, id="birthday-cake-when"),
        pytest.param({"item": "a cake for 30 people"}, "menu/custom-cake", ["2026-10-17", "2026-10-18", "2026-10-20"], None, id="cake-for-30-when"),
        pytest.param(
            {"item": "wedding cake", "date_from": "2026-11-26", "date_to": "2026-11-28"}, "menu/custom-cake",
            ["2026-11-27", "2026-11-28"], None, id="wedding-cake-thanksgiving-week",
        ),
        pytest.param(
            {"item": "birthday cake", "date_from": "2026-10-15", "date_to": "2026-10-16"}, "menu/custom-cake", [], "needs_owner",
            id="birthday-cake-this-week",
        ),
        # Bread and case cakes cannot be ordered through the assistant (case cakes can't be reserved).
        pytest.param({"item": "sourdough"}, "menu/country-sourdough", [], "not_offered", id="reserve-sourdough"),
        pytest.param({"item": "case cake"}, "menu/case-cake-6in", [], "not_offered", id="reserve-case-cake"),
    ],
)
async def test_availability_phrasing(
    client: Any, deps: Any, args: dict[str, Any], item_ref: str, days: list[str], handoff: str | None
) -> None:
    data = await call(client, "check_availability", args)
    assert data["item_ref"] == item_ref, data["speak"]
    assert [s["date"] for s in data["slots"]][: len(days) or None] == days, data["speak"]
    assert (data["handoff"] or {}).get("reason") == handoff, data["speak"]
    if handoff:
        assert data["speak"].endswith(ASK_ROSA)
    await assert_grounded(deps, data)


# ============================================================================ discovery


@pytest.mark.parametrize(
    ("args", "first"),
    [
        pytest.param({"need": "birthday cake", "locality": "Portland"}, BAKERY, id="birthday-cake-in-portland"),
        pytest.param({"need": "sourdough bread near me", "locality": "near me"}, BAKERY, id="sourdough-near-me"),
        pytest.param({"need": "gluten free bakery"}, BAKERY, id="gluten-free-bakery"),
        pytest.param({"need": "croissants in portland"}, BAKERY, id="croissants-in-portland"),
        pytest.param({"need": "pizza delivery"}, None, id="pizza-delivery"),
        pytest.param({"need": "birthday cake", "locality": "Seattle"}, None, id="birthday-cake-in-seattle"),
    ],
)
async def test_find_business_phrasing(client: Any, deps: Any, args: dict[str, Any], first: str | None) -> None:
    data = await call(client, "find_business", args)
    ids = [b["business_id"] for b in data["businesses"]]
    if first is None:
        speak = data["speak"]
        assert ids == [] and ("couldn't find" in speak or "don't have a certified business" in speak), (ids, speak)
    else:
        assert ids and ids[0] == first, (ids, data["speak"])
    await assert_grounded(deps, data)


# ============================================================================ custom-cake orders


WHO = {"customer_name": "Sam Lee", "customer_phone": "+12175550100"}


@dataclass(frozen=True)
class Order:
    item: str
    choices: dict[str, str]
    date: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Outcome:
    status: str
    cents: int | None = None
    choices: dict[str, str] | None = None
    says: tuple[str, ...] = ()
    missing: tuple[str, ...] | None = None


ORDERS: list[Any] = [
    pytest.param(
        Order("birthday cake", {"size": "8 inch", "flavor": "funfetti", "filling": "jam", "frosting": "cream cheese"}, "2026-10-18", {"servings": 20}),
        Outcome(
            "confirmed", 8400,
            {"size": "eight inch round", "flavor": "birthday sprinkle", "filling": "marionberry jam", "frosting": "cream cheese frosting"},
            says=("$84",),
        ),
        id="8-inch-funfetti-jam-cream-cheese",
    ),
    pytest.param(
        Order("gluten free chocolate cake", {"size": "6 inch", "flavor": "gluten free chocolate", "filling": "salted caramel", "frosting": "chocolate frosting"}, "2026-10-18", {"servings": 10}),
        Outcome("confirmed", 6800, {"size": "six inch round", "flavor": "gluten-free dark chocolate", "filling": "salted caramel", "frosting": "chocolate buttercream"}, says=("$68",)),
        id="gluten-free-chocolate-6-inch",
    ),
    pytest.param(
        Order("birthday cake", {"size": "6 inch", "flavor": "chocolate", "filling": "ganache", "frosting": "chocolate buttercream", "extras": "flowers"}, "2026-10-18", {"servings": 12}),
        Outcome("confirmed", 7100, {"size": "six inch round", "flavor": "dark chocolate", "filling": "chocolate ganache", "frosting": "chocolate buttercream", "extras": "buttercream flowers"}, says=("$71",)),
        id="chocolate-with-piped-flowers",
    ),
    pytest.param(
        Order("two tier wedding cake", {"size": "two tier", "flavor": "vanilla", "filling": "lemon curd", "frosting": "vanilla buttercream"}, "2026-10-24", {"servings": 48}),
        Outcome("pending_owner", 21000, {"size": "two tier cake", "flavor": "vanilla bean", "filling": "lemon curd", "frosting": "vanilla buttercream"}, says=("Rosa",)),
        id="two-tier-wedding-needs-rosa",
    ),
    pytest.param(
        # $210 + $12 gluten-free surcharge per tier x 2 tiers.
        Order("custom cake", {"size": "two tier", "flavor": "gluten free vanilla", "filling": "lemon curd", "frosting": "vanilla buttercream"}, "2026-10-24", {"servings": 48}),
        Outcome("pending_owner", 23400, {"size": "two tier cake", "flavor": "gluten-free vanilla bean", "filling": "lemon curd", "frosting": "vanilla buttercream"}),
        id="gluten-free-two-tier-surcharge-per-tier",
    ),
    pytest.param(
        Order("cake for 30 people", {"size": "8 inch", "flavor": "lemon", "filling": "lemon curd", "frosting": "vanilla buttercream"}, "2026-10-18", {"servings": 30}),
        Outcome("pending_owner", 8400, says=("Rosa",)),
        id="30-servings-needs-rosa",
    ),
    pytest.param(
        Order("birthday cake", {"size": "6 inch", "flavor": "vanilla", "filling": "jam", "frosting": "vanilla buttercream"}, "2026-10-19", {"servings": 10}),
        Outcome("unavailable", says=("closed",)),
        id="pickup-on-a-monday",
    ),
    pytest.param(
        Order("custom cake", {"size": "six inch round", "flavor": "almond", "filling": "lemon curd", "frosting": "vanilla buttercream"}, "2026-10-18", {"servings": 10}),
        Outcome("needs_info", says=("isn't one of the options",), missing=("flavor",)),
        id="almond-flavor-not-offered",
    ),
    pytest.param(
        Order("birthday cake", {"size": "6 inch", "flavor": "vanilla", "filling": "jam", "frosting": "fondant"}, "2026-10-18", {"servings": 10}),
        Outcome("needs_info", says=("isn't one of the options",), missing=("frosting",)),
        id="fondant-not-offered",
    ),
    pytest.param(
        Order("case cake", {"flavor": "chocolate"}, "2026-10-18", {"servings": 10}),
        Outcome("not_offered", says=(ASK_ROSA,)),
        id="reserve-a-case-cake",
    ),
    pytest.param(
        Order("sourdough", {}, "2026-10-18"),
        Outcome("not_offered", says=(ASK_ROSA,)),
        id="order-a-sourdough-loaf",
    ),
]


@pytest.mark.parametrize(("order", "want"), ORDERS)
async def test_custom_order_phrasing(plain_client: Any, deps: Any, elicitation: ElicitationScript, order: Order, want: Outcome) -> None:
    data = await call(
        plain_client,
        "place_custom_order",
        {"item": order.item, "choices": order.choices, "date": order.date, "details": {**WHO, **order.details}},
    )
    assert data["status"] == want.status, data["speak"]
    commitment = data.get("commitment") or {}
    if want.cents is not None:
        assert commitment["price"]["amount_cents"] == want.cents, commitment.get("price")
    if want.choices is not None:
        assert commitment["choices"] == want.choices
    for text in want.says:
        assert text in data["speak"], data["speak"]
    if want.missing is not None:
        assert tuple(data["missing_fields"]) == want.missing
    if want.status in ("not_offered", "needs_info", "unavailable"):
        assert data["reference"] is None and not commitment
        stored, _cursor = await deps.commitments.list_for_business(BAKERY)
        assert stored == []
    await assert_grounded(deps, data)


# ---- known failures: what the item phrase said is lost, or a dietary request is silently contradicted


async def _order(client: Any, item: str, choices: dict[str, str], details: dict[str, Any]) -> dict[str, Any]:
    return await call(
        client, "place_custom_order", {"item": item, "choices": choices, "date": "2026-10-18", "details": {**WHO, **details}}
    )


@xfail(
    "SAFETY: item 'gluten-free birthday cake' with flavor 'chocolate' is confirmed as plain 'dark chocolate' "
    "(contains wheat) for $56; the gluten-free need in the item phrase is dropped (should be gluten-free dark "
    "chocolate, $68, or a question naming only gluten-free flavors)"
)
async def test_gluten_free_cake_order_keeps_the_gluten_free_flavor(plain_client: Any) -> None:
    data = await _order(
        plain_client, "gluten-free birthday cake",
        {"size": "6 inch", "flavor": "chocolate", "filling": "salted caramel", "frosting": "chocolate buttercream"},
        {"servings": 10},
    )
    flavor = (data.get("commitment") or {}).get("choices", {}).get("flavor")
    assert flavor in (None, "gluten-free dark chocolate"), (data["status"], flavor, data["speak"])


@xfail(
    "SAFETY: item 'vegan cake' with lemon curd filling (owner-confirmed milk and eggs) is confirmed silently; the "
    "profile says vegan cakes come with marionberry jam filling and vegan vanilla buttercream (info/vegan-cakes)"
)
async def test_vegan_cake_order_never_silently_takes_a_dairy_filling(plain_client: Any, elicitation: ElicitationScript) -> None:
    data = await _order(
        plain_client, "vegan cake",
        {"size": "8 inch", "flavor": "vegan vanilla", "filling": "lemon curd", "frosting": "vegan frosting"},
        {"servings": 20, "dietary_needs": "vegan"},
    )
    warned = any(word in message for message, _props in elicitation.seen for word in ("milk", "marionberry", "not vegan"))
    assert data["status"] != "confirmed" or warned, (data["status"], data["speak"], elicitation.seen)


@xfail(
    "item 'gluten free chocolate cake' without a flavor choice re-asks 'Which flavor: vanilla bean, dark chocolate, ...' "
    "(the non-gluten-free list); answering 'dark chocolate' would order a wheat cake. find_item resolves the same "
    "phrase to the gluten-free dark chocolate flavor"
)
async def test_order_reads_the_flavor_from_the_item_phrase(plain_client: Any) -> None:
    data = await _order(
        plain_client, "gluten free chocolate cake", {"size": "6 inch", "filling": "salted caramel", "frosting": "chocolate frosting"}, {"servings": 10}
    )
    assert "flavor" not in data["missing_fields"] or "gluten-free" in data["speak"], data["speak"]


@pytest.mark.parametrize(
    ("item", "choices", "group"),
    [
        pytest.param(
            "half sheet cake", {"flavor": "lemon", "filling": "lemon curd", "frosting": "vanilla buttercream"}, "size",
            marks=xfail("size named in the item phrase ('half sheet cake') is dropped: the order asks 'Which size?'"),
            id="half-sheet-cake",
        ),
        pytest.param(
            "carrot cake", {"size": "6 inch", "filling": "caramel", "frosting": "cream cheese"}, "flavor",
            marks=xfail("flavor named in the item phrase ('carrot cake') is dropped: the order asks 'Which flavor?'"),
            id="carrot-cake",
        ),
    ],
)
async def test_order_keeps_choices_named_in_the_item_phrase(plain_client: Any, item: str, choices: dict[str, str], group: str) -> None:
    data = await _order(plain_client, item, choices, {"servings": 10})
    assert group not in data["missing_fields"], data["speak"]


@xfail(
    "a 40-character cake message gets 'I couldn't use the message on the cake. Could you say it again?' without the "
    "30-character limit (custom-cake message_max_characters=30), so repeating it fails again"
)
async def test_too_long_message_says_the_limit(plain_client: Any) -> None:
    data = await _order(
        plain_client, "birthday cake",
        {"size": "6 inch", "flavor": "vanilla", "filling": "jam", "frosting": "vanilla buttercream"},
        {"servings": 10, "message": "Happy 40th Birthday to the best dad ever"},
    )
    assert data["status"] == "needs_info" and "30" in data["speak"], data["speak"]
