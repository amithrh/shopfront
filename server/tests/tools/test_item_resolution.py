"""Natural item phrasings resolve to the right item, or to an honest question (regression for
`find_item(item="gluten-free cake")` answering "I don't see that at Thimbleberry Bakehouse").

The shared resolver (`tools.matching`, used by find_item, get_catalog, check_availability, book,
place_custom_order and request_quote) reads modifiers in what the customer said: dietary needs
("gluten-free", "no nuts"), head counts ("for 30"), option choices ("10 inch", "carrot cake", "long
hair"), plurals and filler ("a trim", "fix a leaky faucet"). It never resolves to the wrong item
silently: several equally good items are a "Did you mean ...?", and a dietary need no item is
confirmed to meet is a hand-off, never the plain item.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest
from support import ElicitationScript, add_dispute, make_live
from tools_support import BAKERY, HOME, SALON, Stack, assert_grounded, build_stack, loaded, structured

from shopfront_server.profile.model import Profile
from shopfront_server.tools import catalog, matching

pytestmark = pytest.mark.anyio

KITCHEN = "The kitchen also handles wheat, milk, eggs, soy, sesame, tree nuts and peanuts."
GF_FLAVORS = ("vanilla-bean-gf", "dark-chocolate-gf", "lemon-poppyseed-gf")


async def call(client: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return structured(await client.call_tool(name, args))


@pytest.fixture
def by_id(profiles: list[Profile]) -> dict[str, Profile]:
    return {p.profile_id: p for p in profiles}


def variant_stack(tmp_path: Any, raw_profiles: dict[str, dict[str, Any]], change: Callable[[dict[str, Any]], dict[str, Any]]) -> Stack:
    doc = change(make_live(copy.deepcopy(raw_profiles[BAKERY])))
    others = [loaded(make_live(d)) for pid, d in raw_profiles.items() if pid != BAKERY]
    return build_stack(tmp_path, [Profile.unchecked(doc), *others])


# ---------------------------------------------------------------------------- phrase parsing


@pytest.mark.parametrize(
    ("text", "dietary", "servings", "content"),
    [
        ("gluten-free cake", {"gluten_free"}, None, ("cake",)),
        ("Gluten Free cakes", {"gluten_free"}, None, ("cake",)),
        ("a GF bread", {"gluten_free"}, None, ("bread",)),
        ("celiac-safe cake", {"gluten_free"}, None, ("cake",)),
        ("a cake with no nuts please", {"nut_free"}, None, ("cake",)),
        ("vegan, dairy-free muffin", {"vegan", "dairy_free"}, None, ("muffin",)),
        ("a cake for 30", set(), 30, ("cake",)),
        ("cake for thirty people", set(), 30, ("cake",)),
        ("something that feeds twenty five guests", set(), 25, ()),
        ("serves 12", set(), 12, ()),
        ("a cake for two dozen people", set(), 24, ("cake",)),
        ("a 10 inch cake", set(), None, ("10", "inch", "cake")),
        ("a tv for 65 inches", set(), None, ("tv", "65", "inch")),
        ("a dozen cookies", set(), None, ("dozen", "cooky")),
        ("fix a leaky faucet", set(), None, ("leaky", "faucet")),
        ("a trim", set(), None, ("trim",)),
    ],
)
def test_phrases_split_into_modifiers_filler_and_content(
    text: str, dietary: set[str], servings: int | None, content: tuple[str, ...]
) -> None:
    phrase = matching.parse(text)
    assert set(phrase.dietary) == dietary
    assert phrase.servings == servings
    assert phrase.content == content


# ---------------------------------------------------------------------------- the shared resolver


FOUND: list[tuple[str, str, str, dict[str, tuple[str, ...]]]] = [
    # (business, what the customer said, item, {option group: choices the words point at})
    (BAKERY, "gluten-free cake", "menu/custom-cake", {"flavor": GF_FLAVORS}),
    (BAKERY, "gluten free cakes", "menu/custom-cake", {"flavor": GF_FLAVORS}),
    (BAKERY, "celiac-safe cake", "menu/custom-cake", {"flavor": GF_FLAVORS}),
    (BAKERY, "vegan cake", "menu/custom-cake", {"flavor": ("vanilla-bean-vegan", "dark-chocolate-vegan")}),
    (BAKERY, "gluten-free vanilla cake", "menu/custom-cake", {"flavor": ("vanilla-bean-gf",)}),
    (BAKERY, "gluten-free chocolate cake", "menu/custom-cake", {"flavor": ("dark-chocolate-gf",)}),
    (BAKERY, "a cake for 30", "menu/custom-cake", {"size": ("10in-round", "half-sheet", "two-tier", "three-tier")}),
    (BAKERY, "a 10 inch cake", "menu/custom-cake", {"size": ("10in-round",)}),
    (BAKERY, "carrot cake", "menu/custom-cake", {"flavor": ("carrot-walnut",)}),
    (BAKERY, "croissants", "menu/butter-croissant", {}),
    (BAKERY, "sourdough bread", "menu/country-sourdough", {}),
    (BAKERY, "a gluten-free cookie", "menu/flourless-chocolate-crinkle", {}),
    (BAKERY, "vegan cookie", "menu/ginger-molasses", {}),
    (BAKERY, "a dozen cookies", "menu/cookie-dozen", {}),
    (SALON, "balayage for long hair", "services/balayage", {"length": ("past-shoulder-blades",)}),
    (SALON, "men's cut with a beard trim", "services/mens-cut-beard", {}),
    (SALON, "kids haircut", "services/kids-cut", {}),
    (HOME, "fix a leaky faucet", "services/fixture-install", {"job": ("faucet",)}),
    (HOME, "mount a 65 inch tv", "services/tv-mounting", {"size": ("56-to-85-in",)}),
    (HOME, "hang my tv over the fireplace", "services/tv-mounting", {"wall": ("brick-stone-fireplace",)}),
    (HOME, "leak under the sink", "services/leak-repair", {"job": ("under-sink",)}),
    (HOME, "clogged toilet", "services/drain-cleaning", {"job": ("toilet-clog",)}),
    (HOME, "my water heater is leaking", "services/water-heater", {}),
    # word forms, words outside the catalog, counts and sizes
    (HOME, "clogged kitchen sink", "services/drain-cleaning", {"job": ("sink-tub-shower",)}),
    (HOME, "toilets keep clogging", "services/drain-cleaning", {"job": ("toilet-clog",)}),
    (HOME, "slow draining bathtub", "services/drain-cleaning", {}),
    (HOME, "leaky pipe under my kitchen sink", "services/leak-repair", {"job": ("under-sink",)}),
    (HOME, "assemble two ikea dressers", "services/furniture-assembly", {"job": ("medium-item",)}),
    (HOME, "assemble 3 chairs", "services/furniture-assembly", {"job": ("small-item",)}),
    (HOME, "2 by 2 foot drywall patch", "services/drywall-patch", {"size": ("medium",)}),
    (HOME, "mount a 75 inch tv on brick", "services/tv-mounting", {"size": ("56-to-85-in",), "wall": ("brick-stone-fireplace",)}),
    (HOME, "mount a 90 inch tv", "services/tv-mounting", {}),  # no size range holds 90 inches
    (HOME, "replace my 50 gallon gas water heater", "services/water-heater", {"job": ("gas-tank",)}),
    (BAKERY, "12 cookies", "menu/cookie-dozen", {}),
    (SALON, "two men's haircuts", "services/mens-cut", {}),
]


@pytest.mark.parametrize(("business", "text", "ref", "focus"), FOUND)
def test_natural_phrasings_resolve_to_one_item(
    by_id: dict[str, Profile], business: str, text: str, ref: str, focus: dict[str, tuple[str, ...]]
) -> None:
    lookup = catalog.resolve_item(by_id[business], text)
    assert lookup.found is not None and lookup.found.ref == ref, (lookup.candidates, lookup.unmet_dietary)
    assert {f.group_id: f.choice_ids for f in lookup.focus} == focus


@pytest.mark.parametrize(
    ("business", "text", "candidates"),
    [
        (BAKERY, "cake", ["menu/slice-of-cake", "menu/case-cake-6in", "menu/custom-cake"]),
        (BAKERY, "chocolate cake", ["menu/case-cake-6in", "menu/custom-cake"]),  # both come in dark chocolate
        (BAKERY, "vanilla", ["menu/case-cake-6in", "menu/custom-cake"]),
        (BAKERY, "almond croissant with chocolate", ["menu/chocolate-croissant", "menu/almond-croissant"]),
        (BAKERY, "gluten-free", ["menu/gluten-free-sandwich-loaf", "menu/flourless-chocolate-crinkle", "menu/custom-cake"]),
        (SALON, "a trim", ["services/mens-cut-beard", "services/bang-trim"]),
        (SALON, "highlights", ["services/partial-highlights", "services/full-highlights", "services/balayage"]),
        (SALON, "color", ["services/color-consult", "services/root-touch-up", "services/all-over-color"]),
    ],
)
def test_equally_good_matches_are_a_question_never_a_guess(
    by_id: dict[str, Profile], business: str, text: str, candidates: list[str]
) -> None:
    lookup = catalog.resolve_item(by_id[business], text)
    assert lookup.found is None
    assert [c.ref for c in lookup.candidates] == candidates


@pytest.mark.parametrize(
    ("text", "tags", "candidates"),
    [
        ("nut-free cake", ("nut_free",), ["menu/slice-of-cake", "menu/case-cake-6in", "menu/custom-cake"]),
        ("a dairy free cake", ("dairy_free",), ["menu/slice-of-cake", "menu/case-cake-6in", "menu/custom-cake"]),
        ("vegan croissant", ("vegan",), ["menu/butter-croissant"]),
    ],
)
def test_an_unmet_dietary_need_never_resolves_to_the_plain_item(
    by_id: dict[str, Profile], text: str, tags: tuple[str, ...], candidates: list[str]
) -> None:
    lookup = catalog.resolve_item(by_id[BAKERY], text)
    assert lookup.found is None and lookup.unmet_dietary == tags
    assert [c.ref for c in lookup.candidates] == candidates
    assert catalog.commit_item(by_id[BAKERY], text, "custom_order") == text  # the domain never gets an item ref


@pytest.mark.parametrize("text", ["almond cake", "lemon tart", "pizza", "a haircut for my dog", "pumpkin pie", "brownies", "chocolate chip pancakes"])
def test_unexplained_words_do_not_borrow_an_item(by_id: dict[str, Profile], text: str) -> None:
    business = SALON if "haircut" in text else BAKERY
    lookup = catalog.resolve_item(by_id[business], text)
    assert lookup.found is None and lookup.candidates == []


@pytest.mark.parametrize(
    ("business", "text", "ref"),
    [
        (HOME, "watter heater", "services/water-heater"),
        (HOME, "drywal patch", "services/drywall-patch"),
        (HOME, "furnature assembly", "services/furniture-assembly"),
        (BAKERY, "croisant", "menu/butter-croissant"),
    ],
)
def test_a_misspelling_is_a_question_never_a_pick(by_id: dict[str, Profile], business: str, text: str, ref: str) -> None:
    lookup = catalog.resolve_item(by_id[business], text)
    assert lookup.found is None and lookup.corrected and [c.ref for c in lookup.candidates] == [ref]
    assert catalog.commit_item(by_id[business], text, "transactional") == text  # the domain never gets an item ref


def test_what_the_business_says_it_does_not_do_is_never_a_look_alike(by_id: dict[str, Profile]) -> None:
    """policies/not-offered: no sewer line replacement, so it is not the drain cleaning's main line."""
    lookup = catalog.resolve_item(by_id[HOME], "replace my sewer line")
    assert lookup.found is None and lookup.candidates == []
    assert catalog.resolve_item(by_id[HOME], "snake my main sewer line").found.ref == "services/drain-cleaning"  # type: ignore[union-attr]


def test_word_forms_counts_and_sizes_are_read_against_the_catalog(by_id: dict[str, Profile]) -> None:
    home = by_id[HOME]
    vocabulary = matching.catalog_vocabulary(home, catalog.catalog_entries(home))
    phrase = matching.parse("toilets keep clogging", vocabulary)
    assert {"clog", "clogged"} <= phrase.forms("clogging") and phrase.head == "toilet"
    assert matching.parse("replace my sewer line", vocabulary).forms("replace") >= {"replacement"}
    assert matching.parse("assemble 3 chairs", vocabulary).quantities == {"3"}
    measure = matching.parse("2 by 2 foot drywall patch", vocabulary).measure
    assert measure is not None and measure.inches == 24
    assert matching.parse("drywal patch", vocabulary).typos == {"drywal"}
    assert matching.parse("hatch repair", vocabulary).typos == frozenset()  # 'patch' is too short to swap a letter


def test_only_owner_confirmed_tags_satisfy_a_dietary_need(raw_profiles: dict[str, dict[str, Any]]) -> None:
    targets = [f"menu/custom-cake/flavor/{choice}#dietary" for choice in GF_FLAVORS]
    profile = Profile.unchecked(add_dispute(make_live(copy.deepcopy(raw_profiles[BAKERY])), *targets))
    lookup = catalog.resolve_item(profile, "gluten-free cake")
    assert lookup.found is None and lookup.unmet_dietary == ("gluten_free",)


# ---------------------------------------------------------------------------- find_item over MCP


async def test_gluten_free_cake_answers_about_the_gluten_free_option(client: Any, deps: Any) -> None:
    """The reported bug, end to end: the custom cake's gluten-free flavours, their owner-confirmed
    allergen and dietary facts, their price delta and the kitchen statement, each with a fact ref."""
    data = await call(
        client, "find_item", {"business_id": BAKERY, "item": "gluten-free cake", "aspects": ["allergens", "dietary", "price"]}
    )
    assert data["item"]["item_ref"] == "menu/custom-cake"
    assert data["speak"] == (
        "The custom cake has gluten-free cake flavors for $12 more per tier. Depending on what you choose, a "
        f"gluten-free one can contain milk, eggs and soy. {KITCHEN}"
    )
    assert data["handoff"] is None and "don't see" not in data["speak"]
    facts = {f["fact_ref"]: f for f in data["facts"]}
    for choice in GF_FLAVORS:
        ref = f"menu/custom-cake/flavor/{choice}"
        assert facts[f"{ref}#dietary.tags"]["value"] == ["gluten_free"]
        assert facts[f"{ref}#dietary.tags"]["owner_confirmed"] is True
        assert facts[f"{ref}#allergens.contains"]["owner_confirmed"] is True
        assert facts[f"{ref}#price_delta"]["value"]["amount_cents"] == 1200
    assert facts["menu#facility_may_contain.allergens"]["owner_confirmed"] is True
    # Fillings and frostings are part of the answer ("can contain soy"), so their facts are cited too.
    assert "menu/custom-cake/filling/chocolate-ganache#allergens.contains" in facts
    [flavors] = data["matched_options"]
    assert flavors["group_id"] == "flavor"
    assert [c["choice_id"] for c in flavors["choices"]] == list(GF_FLAVORS)
    assert flavors["choices"][1]["allergens"] == ["milk", "eggs", "soybeans"]
    assert flavors["choices"][1]["dietary"] == ["gluten_free"]
    assert flavors["choices"][1]["price_delta"]["amount_cents"] == 1200
    assert data["allergens"] is None and data["dietary"] is None  # item-level values stay unknown
    lines = {line["label"]: line["value"] for line in data["card"]["lines"]}
    assert lines["Contains (gluten-free dark chocolate)"] == "milk, eggs and soy"
    assert lines["Filling and frosting can add"] == "milk, eggs and soy"
    await assert_grounded(deps, data)


async def test_gluten_free_cake_default_aspects_name_the_flavors(client: Any, deps: Any) -> None:
    data = await call(client, "find_item", {"business_id": BAKERY, "item": "gluten free cake"})
    assert data["speak"] == (
        "The custom cake has gluten-free cake flavors for $12 more per tier. They are gluten-free vanilla bean, "
        "gluten-free dark chocolate and gluten-free lemon poppyseed. The custom cake starts at $56."
    )
    assert [c["choice_id"] for c in data["matched_options"][0]["choices"]] == list(GF_FLAVORS)
    await assert_grounded(deps, data)


async def test_unconfirmed_gluten_free_option_is_a_handoff(tmp_path: Any, raw_profiles: dict[str, dict[str, Any]]) -> None:
    targets = [f"menu/custom-cake/flavor/{choice}#dietary" for choice in GF_FLAVORS]
    stack = variant_stack(tmp_path, raw_profiles, lambda doc: add_dispute(doc, *targets))
    async with stack.client(ElicitationScript()) as client:
        data = await call(client, "find_item", {"business_id": BAKERY, "item": "gluten-free cake", "aspects": ["dietary"]})
    assert data["speak"] == (
        "I don't know whether the slice of cake, six inch case cake or custom cake comes gluten-free. Want me to ask Rosa?"
    )
    assert data["handoff"]["reason"] == "unknown" and data["handoff"]["suggested_question"] == "Do you have gluten-free cake?"
    assert not any("dietary" in f["fact_ref"] for f in data["facts"])
    assert data["item"] is None


def _drop_choice_allergens(doc: dict[str, Any]) -> dict[str, Any]:
    cake = next(i for i in doc["modules"]["menu"]["items"] if i["id"] == "custom-cake")
    flavor = next(g for g in cake["options"] if g["id"] == "flavor")
    next(c for c in flavor["choices"] if c["id"] == "vanilla-bean-gf").pop("allergens")
    return doc


@pytest.mark.parametrize(
    ("change", "reason", "sentence"),
    [
        # Absent on the matched choice: unknown, never "none".
        (_drop_choice_allergens, "unknown", "I don't know the allergens in the gluten-free vanilla bean"),
        # Disputed: the dispute overlaps the cake's own allergen ref, so the whole answer is withheld.
        (
            lambda doc: add_dispute(doc, "menu/custom-cake/flavor/vanilla-bean-gf#allergens.contains"),
            "disputed",
            "Rosa is double-checking the allergens in the custom cake",
        ),
    ],
)
async def test_unstated_allergens_of_the_matched_option_are_a_handoff(
    tmp_path: Any, raw_profiles: dict[str, dict[str, Any]], change: Callable[[dict[str, Any]], dict[str, Any]], reason: str, sentence: str
) -> None:
    stack = variant_stack(tmp_path, raw_profiles, change)
    async with stack.client(ElicitationScript()) as client:
        data = await call(client, "find_item", {"business_id": BAKERY, "item": "gluten-free vanilla cake", "aspects": ["allergens"]})
    assert data["handoff"]["reason"] == reason
    assert data["speak"].startswith("The custom cake has a gluten-free cake flavor")
    assert sentence in data["speak"] and data["speak"].endswith("Want me to ask Rosa?")
    assert KITCHEN.rstrip(".") in data["speak"] and "none" not in data["speak"].lower()
    assert "menu/custom-cake/flavor/vanilla-bean-gf#allergens.contains" not in [f["fact_ref"] for f in data["facts"]]
    await assert_grounded(stack.deps, data)


async def test_a_cake_for_30_names_the_sizes_that_serve_that_many(client: Any, deps: Any) -> None:
    data = await call(client, "find_item", {"business_id": BAKERY, "item": "a cake for 30"})
    assert data["item"]["item_ref"] == "menu/custom-cake"
    assert data["speak"].startswith(
        "The smallest custom cake for that many is the ten inch round, which serves up to 36 for $120. "
        "The half sheet, two tier cake and three tier cake also serve that many."
    )
    assert "30" not in data["speak"]  # the customer's number is never spoken back as a business fact
    refs = [f["fact_ref"] for f in data["facts"]]
    assert "menu/custom-cake/size/10in-round#attributes.serves" in refs and "menu/custom-cake/size/10in-round#price" in refs
    sizes = data["matched_options"][0]["choices"]
    assert [(c["choice_id"], c["serves"]) for c in sizes] == [("10in-round", 36), ("half-sheet", 48), ("two-tier", 48), ("three-tier", 72)]
    await assert_grounded(deps, data)


async def test_a_head_count_the_item_cannot_serve_is_said(client: Any) -> None:
    data = await call(client, "find_item", {"business_id": BAKERY, "item": "case cake for 30", "aspects": ["price"]})
    assert data["speak"] == "The six inch case cake serves up to 12. The six inch case cake is $42."


@pytest.mark.parametrize(
    ("business", "item", "aspects", "speak"),
    [
        (
            BAKERY, "carrot cake", ["allergens"],
            f"That's the custom cake. The carrot with walnuts contains wheat, milk, eggs and tree nuts. The filling and frosting can add soy. {KITCHEN}",
        ),
        (BAKERY, "a 10 inch cake", ["price"], "For the custom cake, the size can be ten inch round for $120."),
        (BAKERY, "a gluten-free cookie", ["price"], "The flourless chocolate crinkle cookie is gluten-free. The flourless chocolate crinkle cookie is $3.50."),
        (SALON, "balayage for long hair", ["price"], "For the balayage, the hair length can be past the shoulder blades for $285."),
        (HOME, "fix a leaky faucet", ["price"], "For the faucet and fixture installs, the job can be kitchen or bathroom faucet for $145 to $245."),
    ],
)
async def test_modifiers_focus_the_answer(client: Any, deps: Any, business: str, item: str, aspects: list[str], speak: str) -> None:
    data = await call(client, "find_item", {"business_id": business, "item": item, "aspects": aspects})
    assert data["speak"] == speak
    await assert_grounded(deps, data)


@pytest.mark.parametrize(
    ("business", "item", "speak"),
    [
        (BAKERY, "chocolate cake", "Did you mean six inch case cake or custom cake?"),
        (SALON, "a trim", "Did you mean men's cut and beard trim or bang trim?"),
        (SALON, "trim", "Did you mean men's cut and beard trim or bang trim?"),
        (BAKERY, "almond croissant with chocolate", "Did you mean chocolate croissant or almond croissant?"),
    ],
)
async def test_find_item_asks_when_ambiguous(client: Any, business: str, item: str, speak: str) -> None:
    data = await call(client, "find_item", {"business_id": business, "item": item})
    assert data["speak"] == speak and data["item"] is None
    assert all(a["tool"] == "find_item" for a in data["card"]["actions"])


async def test_a_nut_free_cake_is_a_handoff_not_the_plain_cake(client: Any, deps: Any) -> None:
    data = await call(client, "find_item", {"business_id": BAKERY, "item": "nut-free cake", "aspects": ["allergens"]})
    assert data["speak"] == (
        "I don't know whether the slice of cake, six inch case cake or custom cake comes nut-free. Want me to ask Rosa?"
    )
    assert data["handoff"]["reason"] == "unknown" and data["item"] is None
    assert [f["fact_ref"] for f in data["facts"]] == [
        "menu/slice-of-cake#spoken_name", "menu/case-cake-6in#spoken_name", "menu/custom-cake#spoken_name"
    ]
    await assert_grounded(deps, data)
    single = await call(client, "find_item", {"business_id": BAKERY, "item": "vegan croissant"})
    assert single["speak"] == "I don't know whether the butter croissant comes vegan. Want me to ask Rosa?"
    assert single["item"]["item_ref"] == "menu/butter-croissant"
    assert single["handoff"]["fact_ref"] == "menu/butter-croissant#dietary.tags"


async def test_unknown_items_still_say_so(client: Any) -> None:
    data = await call(client, "find_item", {"business_id": BAKERY, "item": "almond cake"})
    assert data["speak"] == "I don't see that at Thimbleberry Bakehouse. Want me to ask Rosa?"


# ---------------------------------------------------------------------------- get_catalog and the commit tools


@pytest.mark.parametrize(
    ("query", "refs"),
    [
        ("gluten-free cake", ["menu/custom-cake"]),  # was the gluten-free loaf and the crinkle cookie
        ("gluten-free", ["menu/gluten-free-sandwich-loaf", "menu/flourless-chocolate-crinkle", "menu/custom-cake"]),
        ("a cake for 30", ["menu/custom-cake"]),
        ("cakes", ["menu/slice-of-cake", "menu/case-cake-6in", "menu/custom-cake"]),
        ("chocolate croissant", ["menu/chocolate-croissant"]),
    ],
)
async def test_catalog_queries_read_modifiers(client: Any, query: str, refs: list[str]) -> None:
    data = await call(client, "get_catalog", {"business_id": BAKERY, "query": query, "limit": 25})
    assert [i["item_ref"] for i in data["items"]] == refs


async def test_availability_and_orders_resolve_the_same_way(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        days = await call(client, "check_availability", {"business_id": BAKERY, "item": "gluten-free cake"})
        assert days["item_ref"] == "menu/custom-cake" and days["slots"]
        order = await call(client, "place_custom_order", {"business_id": BAKERY, "item": "gluten-free cake", "date": "2026-10-18"})
        assert order["status"] == "needs_info" and "flavor" in order["missing_fields"], order["speak"]
        quote = await call(client, "request_quote", {"business_id": HOME, "item": "fix a leaky faucet"})
        assert quote["status"] != "not_offered", quote["speak"]
        looked = await call(client, "check_availability", {"business_id": SALON, "item": "a trim"})
        booked = await call(client, "book", {"business_id": SALON, "item": "a trim", "start": "2026-10-16T10:00"})
        assert booked["speak"] == looked["speak"] == "Did you mean men's cut and beard trim or bang trim?"
        assert booked["missing_fields"] == ["item"]


async def test_an_unmet_dietary_need_is_never_ordered_as_the_plain_item(stack: Stack, elicit: ElicitationScript) -> None:
    async with stack.client(elicit) as client:
        days = await call(client, "check_availability", {"business_id": BAKERY, "item": "nut-free cake"})
        order = await call(
            client,
            "place_custom_order",
            {
                "business_id": BAKERY,
                "item": "nut-free cake",
                "choices": {"size": "six inch round", "flavor": "vanilla bean", "filling": "lemon curd", "frosting": "vanilla buttercream"},
                "date": "2026-10-18",
                "details": {"servings": 10, "customer_name": "Sam Lee", "customer_phone": "+12175550100"},
            },
        )
    expected = "I don't know whether the custom cake comes nut-free. Want me to ask Rosa?"
    assert days["speak"] == expected and days["slots"] == [] and days["handoff"]["reason"] == "unknown"
    assert order["speak"] == expected and order["status"] == "not_offered"
    assert order["reference"] is None and order["commitment"] is None
    assert elicit.seen == []  # no read-back was ever offered for confirmation
    stored, _cursor = await stack.deps.commitments.list_for_business(BAKERY)
    assert stored == []
