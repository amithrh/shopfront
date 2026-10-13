"""Price arithmetic over the gold catalogs: absolute choice prices, deltas, per-unit multipliers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from shopfront_server.domain.pricing import price_for, selected_choices
from shopfront_server.errors import UnknownItem
from shopfront_server.profile.factref import FACT_REF_PATTERN
from shopfront_server.profile.model import Profile

CAKE = "menu/custom-cake"


@pytest.mark.parametrize(
    ("size", "cents"),
    [
        ("6in-round", 5600),
        ("8in-round", 8400),
        ("10in-round", 12000),
        ("quarter-sheet", 7800),
        ("half-sheet", 13500),
        ("two-tier", 21000),
        ("three-tier", 32000),
    ],
)
def test_bakery_size_price_replaces_the_from_price(bakery: Profile, size: str, cents: int) -> None:
    quote = price_for(bakery, CAKE, choices={"size": size})
    assert quote is not None
    assert (quote.kind, quote.amount_cents, quote.currency) == ("fixed", cents, "USD")
    assert [line.fact_ref for line in quote.lines] == [f"{CAKE}/size/{size}#price"]


def test_bakery_without_choices_is_a_from_price(bakery: Profile) -> None:
    quote = price_for(bakery, CAKE)
    assert quote is not None
    assert (quote.kind, quote.amount_cents, quote.per) == ("from", 5600, "each")
    assert quote.fact_refs == (f"{CAKE}#price",)


@pytest.mark.parametrize(
    ("size", "tiers", "total"),
    [("10in-round", 1, 12000 + 1200), ("two-tier", 2, 21000 + 2 * 1200), ("three-tier", 3, 32000 + 3 * 1200)],
)
def test_gluten_free_delta_is_per_tier(bakery: Profile, size: str, tiers: int, total: int) -> None:
    quote = price_for(bakery, CAKE, choices={"size": size, "flavor": "vanilla-bean-gf"})
    assert quote is not None and quote.kind == "fixed"
    assert quote.amount_cents == total
    delta = quote.lines[1]
    assert delta.amount_cents == 1200 * tiers
    assert delta.fact_ref == f"{CAKE}/flavor/vanilla-bean-gf#price_delta"
    assert f"{CAKE}/size/{size}#attributes.tiers" in quote.fact_refs


def test_multi_select_extras_each_add_a_line(bakery: Profile) -> None:
    quote = price_for(
        bakery,
        CAKE,
        choices={"size": "8in-round", "flavor": "vanilla-bean-vegan", "extras": "buttercream-flowers,fresh-fruit"},
    )
    assert quote is not None
    assert quote.amount_cents == 8400 + 1000 + 1500 + 1200
    assert [line.amount_cents for line in quote.lines] == [8400, 1000, 1500, 1200]
    assert sum(line.amount_cents for line in quote.lines) == quote.amount_cents


def test_a_delta_whose_unit_cannot_be_counted_is_never_estimated(bakery: Profile) -> None:
    # "+$12 per tier" with no size chosen: the tier count is unknown, so no number may be stated.
    quote = price_for(bakery, CAKE, choices={"flavor": "vanilla-bean-gf"})
    assert quote is not None
    assert quote.kind == "quote_only" and quote.amount_cents is None


def test_every_price_fact_ref_is_well_formed(bakery: Profile) -> None:
    quote = price_for(bakery, CAKE, choices={"size": "two-tier", "flavor": "dark-chocolate-gf", "extras": "fresh-fruit"})
    assert quote is not None
    for ref in (*quote.fact_refs, *(line.fact_ref for line in quote.lines)):
        assert FACT_REF_PATTERN.fullmatch(ref), ref


def test_salon_choice_price_and_per_hour(salon: Profile) -> None:
    balayage = price_for(salon, "services/balayage", choices={"length": "past-shoulder-blades"})
    assert balayage is not None and (balayage.kind, balayage.amount_cents) == ("fixed", 28500)
    unconfigured = price_for(salon, "services/balayage")
    assert unconfigured is not None and (unconfigured.kind, unconfigured.amount_cents) == ("from", 24500)
    hourly = price_for(salon, "services/color-correction")
    assert hourly is not None and (hourly.kind, hourly.amount_cents, hourly.per) == ("fixed", 9000, "hour")
    free = price_for(salon, "services/color-consult")
    assert free is not None and free.kind == "free" and free.amount_cents is None


def test_home_services_quote_only_range_and_missing(home: Profile) -> None:
    other = price_for(home, "services/other-job")
    assert other is not None and other.kind == "quote_only" and other.amount_cents is None
    labor = price_for(home, "services/plumbing-labor")
    assert labor is not None and (labor.kind, labor.amount_cents, labor.max_cents, labor.per) == ("range", 12500, 14500, "hour")
    assert price_for(home, "services/travel-fee") is None


def test_per_person_multiplies_by_party_size(raw: Callable[[str], dict[str, Any]], build_profile: Callable[..., Profile]) -> None:
    doc = raw("quillfern-salon")
    item = next(i for i in doc["modules"]["services"]["items"] if i["id"] == "updo")
    item["price"] = {"kind": "fixed", "amount_cents": 11000, "currency": "USD", "per": "person"}
    profile = build_profile(doc)
    group = price_for(profile, "services/updo", party_size=3)
    assert group is not None and (group.amount_cents, group.per) == (33000, None)
    single = price_for(profile, "services/updo")
    assert single is not None and (single.amount_cents, single.per) == (11000, "person")


def test_unknown_item_raises(bakery: Profile) -> None:
    with pytest.raises(UnknownItem):
        price_for(bakery, "menu/moon-cake")


def test_selected_choices_follow_option_group_order(bakery: Profile) -> None:
    _, item = bakery.item(CAKE)
    refs = [s.ref for s in selected_choices(CAKE, item, {"frosting": "cream-cheese", "size": "6in-round", "bogus": "x"})]
    assert refs == [f"{CAKE}/size/6in-round", f"{CAKE}/frosting/cream-cheese"]
