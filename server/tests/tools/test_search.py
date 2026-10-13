"""`find_business` matching (`tools.search`): normalisation, scoring signals, filters and ordering."""

from __future__ import annotations

from typing import Any

import pytest
from support import SCHEMA_PATH
from tools_support import BAKERY, HOME, SALON

from shopfront_server.profile.directory import DirectoryEntry, build_directory_entry
from shopfront_server.profile.loader import load_profile
from shopfront_server.tools import search


@pytest.fixture
def entries(live_raw_profiles: dict[str, dict[str, Any]]) -> list[DirectoryEntry]:
    return [
        build_directory_entry(load_profile(doc, schema_path=SCHEMA_PATH, strict=False).profile)
        for doc in live_raw_profiles.values()
    ]


def entry(business_id: str, name: str, **fields: Any) -> DirectoryEntry:
    fields.setdefault("categories", [])
    return DirectoryEntry(business_id=business_id, revision=1, name=name, spoken_name=name, **fields)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Gluten-Free  Birthday CAKES!", "gluten free birthday cake"),
        ("Thimbleberry's bakeries", "thimbleberry bakery"),  # both sides are normalised the same way
        ("hair_salon", "hair salon"),
        ("glasses & dishes", "glass dish"),
        ("bus", "bus"),
        ("Café", "café"),
        ("  ", ""),
    ],
)
def test_normalize(text: str, expected: str) -> None:
    assert search.normalize(text) == expected


def test_contains_phrase_matches_whole_words_only() -> None:
    assert search.contains_phrase("a hair salon nearby", "hair salon")
    assert not search.contains_phrase("a comfy chair", "hair")
    assert not search.contains_phrase("anything", "")


@pytest.mark.parametrize(
    ("need", "winner"),
    [
        ("gluten-free birthday cake", BAKERY),
        ("leaking water heater", HOME),
        ("haircut", SALON),
        ("vegan blueberry muffin", BAKERY),
        ("someone to mount my TV", HOME),
        ("balayage", SALON),
    ],
)
def test_gold_ranking(entries: list[DirectoryEntry], need: str, winner: str) -> None:
    ranked = search.rank(entries, need=need)
    assert ranked and ranked[0].entry.business_id == winner
    assert ranked[0].score > 0 and ranked[0].matched_on


def test_signals_are_reported(entries: list[DirectoryEntry]) -> None:
    top = search.rank(entries, need="gluten-free birthday cake")[0]
    assert {"category", "item", "dietary"} <= set(top.matched_on)
    assert top.score >= search.SCORE_CATEGORY + search.SCORE_ITEM + search.SCORE_DIETARY


def test_zero_scores_are_never_returned(entries: list[DirectoryEntry]) -> None:
    assert search.rank(entries, need="car wash and detailing") == []
    assert search.rank(entries, need="") == []
    assert search.rank(entries, need=None) == []


def test_category_argument_counts_like_the_need(entries: list[DirectoryEntry]) -> None:
    ranked = search.rank(entries, need="something nice", category="bakery")
    assert [m.entry.business_id for m in ranked] == [BAKERY]


def test_locality_is_always_a_filter(entries: list[DirectoryEntry]) -> None:
    served = search.rank(entries, need="leak repair", locality="80211")
    assert [m.entry.business_id for m in served] == [HOME]
    assert "locality" in served[0].matched_on
    # A place nobody serves finds nobody (never a business in another city), and "near me" is no place.
    assert search.rank(entries, need="cake", locality="Atlantis") == []
    assert [m.entry.business_id for m in search.rank(entries, need="cake", locality="near me")] == [BAKERY]


def test_locality_is_a_hard_filter_when_it_matches_a_business() -> None:
    a = entry("a", "Alpha Bakery", categories=["bakery"], localities=["springfield"])
    b = entry("b", "Beta Bakery", categories=["bakery"], localities=["shelbyville"])
    ranked = search.rank([a, b], need="bakery", locality="Springfield")
    assert [m.entry.business_id for m in ranked] == ["a"]


def test_ties_break_by_name_and_limit_applies() -> None:
    a = entry("z", "Zeta Bakery", categories=["bakery"])
    b = entry("a", "Alpha Bakery", categories=["bakery"])
    c = entry("m", "Mid Bakery", categories=["bakery"])
    ranked = search.rank([a, b, c], need="bakery")
    assert [m.entry.name for m in ranked] == ["Alpha Bakery", "Mid Bakery", "Zeta Bakery"]
    assert len(search.rank([a, b, c], need="bakery", limit=2)) == 2


def test_each_signal_counts_once() -> None:
    many = entry(
        "x",
        "Cake Place",
        categories=["bakery", "custom_cakes"],
        keywords=["cake", "cakes", "birthday cake"],
        item_terms=["cake", "birthday cake", "layer cake"],
    )
    top = search.rank([many], need="birthday cake")[0]
    assert top.score == search.SCORE_CATEGORY + search.SCORE_ITEM + search.SCORE_KEYWORD
    assert top.matched_on == ["category", "item", "keyword"]


def test_dietary_needs_an_offered_tag() -> None:
    with_tag = entry("v", "Vegan Place", categories=["bakery"], dietary_tags=["vegan"])
    without = entry("w", "Plain Place", categories=["bakery"])
    ranked = search.rank([with_tag, without], need="vegan bakery")
    assert ranked[0].entry.business_id == "v"
    assert "dietary" in ranked[0].matched_on and "dietary" not in ranked[1].matched_on


def test_one_items_words_can_explain_a_need() -> None:
    """'put together ikea furniture': no item term is contained in the need, but one item's words
    (its synonyms and its option choices') explain most of it, its last word included."""
    handy = entry(
        "h",
        "Handy Co",
        categories=["handyman"],
        item_terms=["furniture assembly", "put together a dresser"],
        item_term_groups=[["furniture assembly", "put together a dresser", "chair", "dresser"]],
    )
    bakery = entry("b", "Cookie Co", categories=["bakery"], item_term_groups=[["chocolate chip cookie", "chocolate chip"]])
    ranked = search.rank([handy, bakery], need="someone to put together ikea furniture")
    assert [m.entry.business_id for m in ranked] == ["h"] and ranked[0].matched_on == ["item"]
    assert search.rank([handy, bakery], need="chocolate chip pancakes") == []  # the head is not explained
    assert search.rank([handy, bakery], need="ikea furniture") == []  # one word is not most of two
