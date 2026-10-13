"""Phrasing stress test for kettlewick-home (Kettlewick Home Repair, Denver plumbing and handyman).

Drives the real tools in-process (the shared `client` / `plain_client` / `deps` fixtures from
tests/conftest.py, over live copies of the gold profiles built by `support.make_live`) with the kind of
phrasing a voice assistant passes straight through: item names with modifiers, plurals, colloquial
words, sizes and quantities, misspellings, a dietary modifier that makes no sense for a plumber, price
questions, policy questions, availability and service-area questions, and out-of-scope asks.

Every case asserts the RIGHT behaviour according to profiles/kettlewick-home.json:
  * the correct catalog item (and, where the words name one, the correct option choice);
  * the correct grounded fact or policy entry, spoken verbatim and statable (`assert_grounded`);
  * an honest hand-off when the profile truly says nothing;
  * never a wrong item, a wrong policy, a false "we don't do that" for an offered service, or an
    invented fact (the site's injection canaries, "$0" jobs and guaranteed same-day service, included).

Known failures are marked `xfail(strict=True)` with the observed output in the reason, so the suite
stays green until the product code is fixed (and turns red, prompting removal of the mark, once it is).
Severity tags in the reasons: WRONG (a wrong item, entry or fact is stated), DENIAL (an offered service
or served area is denied), GAP (a safe hand-off or a weaker answer where the profile has the answer).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from support import ElicitationScript

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))  # same pattern as tests/tools/conftest.py

from tools_support import assert_grounded, structured  # noqa: E402

pytestmark = pytest.mark.anyio

HOME = "kettlewick-home"

CANARY_PHRASES = ("$0", "all jobs are free", "same-day service is guaranteed", "guaranteed same-day")
"""The gold site's injection canaries (sources.*.injection_canaries): never spoken as the business's words."""

HOME_INTAKE = {
    "name": "Sam Lee",
    "contact_by": "text",
    "phone": "+13035550100",
    "time_window": "weekday-morning",
    "description": "Customer described the job on the phone.",
}
"""Answers for kettlewick-home's required quote intake fields (zip and description come per case)."""


def xfail(reason: str) -> pytest.MarkDecorator:
    return pytest.mark.xfail(strict=True, reason=reason)


@pytest.fixture
def elicitation() -> ElicitationScript:
    """Overrides the conftest fixture: answers kettlewick-home's quote intake and confirms."""
    return ElicitationScript(intake=dict(HOME_INTAKE))


async def call(client: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    data = structured(await client.call_tool(tool, {"business_id": HOME, **args} if tool != "find_business" else args))
    spoken = data["speak"].lower()
    for canary in CANARY_PHRASES:
        assert canary.lower() not in spoken, f"canary {canary!r} spoken: {data['speak']}"
    return data


def focus_of(data: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    return {g["group_id"]: tuple(c["choice_id"] for c in g["choices"]) for g in data.get("matched_options") or ()}


def says(data: dict[str, Any], *parts: str) -> None:
    for part in parts:
        assert part in data["speak"], f"expected {part!r} in: {data['speak']}"


def never(data: dict[str, Any], *parts: str) -> None:
    for part in parts:
        assert part not in data["speak"], f"did not expect {part!r} in: {data['speak']}"


# ============================================================================ the truth these cases rely on


def test_gold_profile_facts_used_below(raw_profiles: dict[str, dict[str, Any]]) -> None:
    """The hard-coded expectations below come from the gold profile; fail loudly if it changes."""
    doc = raw_profiles[HOME]
    items = {i["id"]: i for i in doc["modules"]["services"]["items"]}
    choices = {
        (item_id, g["id"], c["id"]): c for item_id, i in items.items() for g in i.get("options") or () for c in g["choices"]
    }
    assert choices[("tv-mounting", "size", "56-to-85-in")]["price"]["amount_cents"] == 19900
    assert choices[("tv-mounting", "wall", "brick-stone-fireplace")]["price_delta"]["amount_cents"] == 10000
    assert choices[("drywall-patch", "size", "medium")]["price"] == {
        "kind": "range", "amount_cents": 22500, "max_cents": 39500, "currency": "USD"
    }
    assert items["travel-fee"]["description_spoken"] == "No travel fee in our core area, and $35 per visit in the extended area."
    assert items["tv-mounting"]["attributes"]["max_tv_size_in"] == 85
    entries = {e["id"]: e for e in doc["modules"]["policies"]["entries"]}
    assert "parking" not in " ".join(str(v) for e in entries.values() for v in e["question_variants"]).lower()
    assert "sewer line excavation or replacement" in entries["not-offered"]["answer_card"]
    assert entries["unlisted-zip"]["answer_spoken"].startswith("If your ZIP code isn't listed, send a quote request anyway")
    areas = {loc["id"]: loc for loc in doc["business"]["locations"]}
    assert "80239" not in areas["core-area"]["area"]["postal_codes"] + areas["extended-area"]["area"]["postal_codes"]
    assert "80239" not in entries["outside-area"]["facts"]["referred_out_postal_codes"]
    assert "Lakewood" in areas["extended-area"]["area"]["localities"]
    assert {"Capitol Hill", "Washington Park", "Highland", "Central Park"} <= set(areas["core-area"]["area"]["localities"])


# ============================================================================ find_item: what the customer calls the job


@dataclass(frozen=True)
class Item:
    text: str
    ref: str | None
    """Expected item ref; None = nothing resolved (an honest 'I don't see that' hand-off)."""
    focus: dict[str, tuple[str, ...]] | None = None
    """Expected matched option choices (exact); None = not checked."""
    says: tuple[str, ...] = ()
    never: tuple[str, ...] = ()
    aspects: tuple[str, ...] | None = None
    suggest_ok: bool = False
    """A 'Did you mean ...?' that offers `ref` is also right (misspellings)."""


ITEMS = [
    # modifiers, plurals and colloquial words
    pytest.param(Item("leak under the bathroom sink", "services/leak-repair", {"job": ("under-sink",)}, ("$150 to $275",)), id="leak-under-bathroom-sink"),
    pytest.param(Item("hot water heater", "services/water-heater"), id="hot-water-heater"),
    pytest.param(Item("my water heater is busted", "services/water-heater"), id="water-heater-busted"),
    pytest.param(Item("no hot water", "services/water-heater"), id="no-hot-water"),
    pytest.param(Item("tankless water heater", "services/water-heater", {"job": ("tankless",)}, ("from $3,800",)), id="tankless"),
    pytest.param(Item("replace my 50 gallon gas water heater", "services/water-heater", None, ("$1,650 to $2,400",)), id="50-gallon-gas-heater"),
    pytest.param(Item("clogged toilets", "services/drain-cleaning", {"job": ("toilet-clog",)}, ("$139 to $199",)), id="clogged-toilets-plural"),
    pytest.param(Item("snake my main sewer line", "services/drain-cleaning", {"job": ("main-line",)}, ("$249 to $395",)), id="snake-main-line"),
    pytest.param(Item("new garbage disposal", "services/fixture-install", {"job": ("disposal",)}, ("$195 to $325",)), id="garbage-disposal"),
    pytest.param(Item("garburator", "services/fixture-install", {"job": ("disposal",)}), id="garburator-colloquial"),
    pytest.param(Item("install a new toilet", "services/fixture-install", {"job": ("toilet",)}, ("$225 to $375",), ("toilet clog",)), id="install-new-toilet"),
    pytest.param(Item("dripping shower", "services/fixture-install", {"job": ("shower-cartridge",)}, ("$185 to $295",)), id="dripping-shower"),
    pytest.param(Item("new kitchen faucets", "services/fixture-install", {"job": ("faucet",)}, ("$145 to $245",)), id="kitchen-faucets-plural"),
    pytest.param(Item("hole in my drywall from a doorknob", "services/drywall-patch"), id="doorknob-hole"),
    pytest.param(Item("pipe leak in the wall", "services/leak-repair", {"job": ("in-wall",)}, (), ("$",), ("price",)), id="leak-in-wall-quote-only"),
    pytest.param(Item("ceiling patch", "services/drywall-patch", {"size": ("large",)}, (), ("$",)), id="ceiling-patch-quote-only"),
    pytest.param(Item("put together a bed frame", "services/furniture-assembly", {"job": ("medium-item",)}, ("$120 to $220 per item",)), id="bed-frame"),
    pytest.param(Item("flatpack wardrobe", "services/furniture-assembly", {"job": ("wardrobe-closet",)}, ("$85 to $105 per hour",)), id="flatpack-wardrobe"),
    pytest.param(Item("shutoff valves", "services/leak-repair", {"job": ("shutoff-valve",)}, ("$165 to $240 per valve",)), id="shutoff-valves-plural"),
    pytest.param(Item("sewer camera", "services/drain-cleaning", {"job": ("camera-only",)}, ("$295",), (), ("price",)), id="sewer-camera"),
    # sizes and quantities
    pytest.param(Item("mount a 50 inch tv", "services/tv-mounting", {"size": ("up-to-55-in",)}, ("$149",)), id="tv-50-inch"),
    pytest.param(Item("mount a 90 inch tv", "services/tv-mounting", {}, (), ("90",)), id="tv-90-inch-no-size-claimed"),
    pytest.param(Item("assemble a nightstand", "services/furniture-assembly", {"job": ("small-item",)}, ("$65 per item",), (), ("price",)), id="nightstand"),
    # price questions
    pytest.param(Item("how much to mount a 65 inch tv", "services/tv-mounting", {"size": ("56-to-85-in",)}, ("$199",), (), ("price",)), id="price-tv-65-inch"),
    pytest.param(Item("hang a tv over the fireplace", "services/tv-mounting", {"wall": ("brick-stone-fireplace",)}, ("$100 more",), (), ("price",)), id="price-tv-fireplace"),
    pytest.param(Item("full motion tv mount", "services/tv-mounting", {"mount": ("full-motion-mount",)}, ("$119 more",), (), ("price",)), id="price-full-motion-mount"),
    pytest.param(Item("what does a new toilet cost", "services/fixture-install", {"job": ("toilet",)}, ("$225 to $375",), (), ("price",)), id="price-new-toilet"),
    pytest.param(Item("plumber hourly rate", "services/plumbing-labor", None, ("$125 to $145 per hour",)), id="price-plumber-hourly"),
    pytest.param(Item("how much per hour for a handyman", "services/handyman-labor", None, ("$85 to $105 per hour",)), id="price-handyman-hourly"),
    pytest.param(Item("service call fee", "services/diagnostic-visit", None, ("$79",)), id="price-service-call"),
    # out of scope: profile lists none of these (furnaces, outlets, hydro-jetting, appliances are explicitly not offered)
    pytest.param(Item("furnace repair", None), id="oos-furnace"),
    pytest.param(Item("install a new electrical outlet", None), id="oos-outlet"),
    pytest.param(Item("hydro jetting", None), id="oos-hydro-jetting"),
    pytest.param(Item("dishwasher repair", None), id="oos-appliance"),
    pytest.param(Item("a large pepperoni pizza", None), id="oos-pizza"),
    # words outside the item vocabulary, word forms, quantities and sizes
    pytest.param(Item("leaky pipe under my kitchen sink", "services/leak-repair", {"job": ("under-sink",)}), id="leaky-pipe-kitchen-sink"),
    pytest.param(Item("clogged kitchen sink", "services/drain-cleaning", {"job": ("sink-tub-shower",)}, ("$139 to $229",)), id="clogged-kitchen-sink"),
    pytest.param(Item("slow draining bathtub", "services/drain-cleaning"), id="slow-draining-bathtub"),
    pytest.param(Item("toilets keep clogging", "services/drain-cleaning", {"job": ("toilet-clog",)}), id="toilets-keep-clogging"),
    pytest.param(Item("assemble two ikea dressers", "services/furniture-assembly", {"job": ("medium-item",)}), id="two-ikea-dressers"),
    pytest.param(Item("assemble 3 chairs", "services/furniture-assembly", {"job": ("small-item",)}, ("$65 per item",)), id="three-chairs"),
    # the profile says Kettlewick does not replace sewer lines (policies/not-offered): never the drain cleaning
    pytest.param(Item("replace my sewer line", None, never=("$249",)), id="replace-sewer-line-not-offered"),
    # misspellings: a "Did you mean ...?" that offers the intended item
    pytest.param(Item("watter heater", "services/water-heater", suggest_ok=True), id="misspelled-watter-heater"),
    pytest.param(Item("garbage dispossal", "services/fixture-install", suggest_ok=True), id="misspelled-dispossal"),
    pytest.param(Item("furnature assembly", "services/furniture-assembly", suggest_ok=True), id="misspelled-furnature"),
    pytest.param(Item("drywal patch", "services/drywall-patch", suggest_ok=True), id="misspelled-drywal"),
    pytest.param(Item("tv mountng", "services/tv-mounting", suggest_ok=True), id="misspelled-mountng"),
    # prices on option choices
    pytest.param(Item("travel fee", "services/travel-fee", None, ("$35", "no charge"), ("I don't know",)), id="travel-fee"),
    pytest.param(
        Item("mount a 75 inch TV on brick", "services/tv-mounting", {"size": ("56-to-85-in",), "wall": ("brick-stone-fireplace",)}, ("$199", "$100"), (), ("price",)),
        id="price-tv-75-inch-on-brick",
    ),
    pytest.param(Item("2 by 2 foot drywall patch", "services/drywall-patch", {"size": ("medium",)}, ("$225 to $395",), (), ("price",)), id="price-drywall-2-by-2"),
    # ---------------------------------------------------------------- known failures
    pytest.param(
        Item("burst pipe", "services/leak-repair", suggest_ok=True),
        id="burst-pipe",
        marks=xfail(
            "DENIAL: 'I don't see that at Kettlewick Home Repair.' A burst pipe is the profile's first-named plumbing "
            "emergency (policies/emergencies) and leak repair covers visible pipe leaks, but no item term or option "
            "names 'burst'. Structurally it is 'pumpkin pie' at the bakery (a known head noun, 'pipe', with an unknown "
            "modifier), which must stay 'I don't see that'; telling them apart needs word meanings the profile does "
            "not publish (a 'burst pipe' synonym on leak-repair would fix it in the data)."
        ),
    ),
]


@pytest.mark.parametrize("case", ITEMS)
async def test_item_phrasing(client: Any, deps: Any, case: Item) -> None:
    args: dict[str, Any] = {"item": case.text}
    if case.aspects is not None:
        args["aspects"] = list(case.aspects)
    data = await call(client, "find_item", args)
    await assert_grounded(deps, data)
    never(data, *case.never)
    got = (data.get("item") or {}).get("item_ref")
    if case.ref is None:
        assert got is None, f"resolved {got}: {data['speak']}"
        assert data["handoff"] is not None and data["facts"] == [], data["speak"]
        return
    if case.suggest_ok and got is None and data["speak"].startswith("Did you mean"):
        assert case.ref in [a["arguments"]["item"] for a in data["card"]["actions"]], data["speak"]
        return
    assert got == case.ref, f"resolved {got}: {data['speak']}"
    assert data["handoff"] is None, data["speak"]
    if case.focus is not None:
        assert focus_of(data) == case.focus, data["speak"]
    says(data, *case.says)


async def test_a_dietary_modifier_is_never_claimed_for_a_plumbing_job(client: Any, deps: Any) -> None:
    """'gluten-free drain cleaning': no item carries the tag, so it is an honest 'I don't know', never a yes."""
    data = await call(client, "find_item", {"item": "gluten free drain cleaning"})
    await assert_grounded(deps, data)
    assert data["speak"] == "I don't know whether the drain cleaning comes gluten-free. Want me to ask Jo?"
    assert data["handoff"]["reason"] == "unknown"
    assert data.get("dietary") in (None, [])


# ============================================================================ ask_policy: policy and FAQ questions


@dataclass(frozen=True)
class Ask:
    question: str
    entry: str | None
    """Expected entry ref (spoken verbatim); None = an honest hand-off (the profile does not say)."""
    says: tuple[str, ...] = ()
    never: tuple[str, ...] = ()


POLICIES = [
    pytest.param(Ask("can I cancel", "policies/cancellation", ("24 hours", "$49")), id="can-i-cancel"),
    pytest.param(Ask("can I reschedule", "policies/cancellation"), id="reschedule"),
    pytest.param(Ask("do you take cards", "policies/payment", ("credit and debit cards",)), id="take-cards"),
    pytest.param(Ask("can I pay with cash", "policies/payment", ("don't take cash",)), id="pay-cash"),
    pytest.param(Ask("do you take apple pay", None, (), ("Apple",)), id="apple-pay-not-in-profile"),
    pytest.param(Ask("do you guys have a senior discount", "policies/discounts", ("10%", "65")), id="senior-discount"),
    pytest.param(Ask("any discount for veterans", "policies/discounts", ("veterans",)), id="veteran-discount"),
    pytest.param(Ask("can you come today", "policies/same-day", ("can't guarantee",)), id="come-today"),
    pytest.param(Ask("is same day service guaranteed", "policies/same-day", ("can't guarantee",)), id="same-day-canary"),
    pytest.param(Ask("are all jobs free this month", None, (), ("free",)), id="free-jobs-canary"),
    pytest.param(Ask("how much is an emergency call", "policies/after-hours-billing", ("$79", "$150", "$195")), id="emergency-call-price"),
    pytest.param(Ask("what does it cost on a sunday", "policies/after-hours-billing", ("$195",)), id="sunday-price"),
    pytest.param(Ask("do you charge to come out and look", "policies/free-estimates", ("$79",)), id="charge-to-come-out"),
    pytest.param(Ask("do you need a deposit", "policies/deposit", ("30%", "$1,500")), id="deposit"),
    pytest.param(Ask("warranty on handyman work", "policies/warranty", ("90 days",)), id="handyman-warranty"),
    pytest.param(Ask("my house was built in 1950 do you do lead safe work", "policies/lead-asbestos", ("1978",)), id="lead-safe-1950"),
    pytest.param(Ask("do you use chemical drain cleaners", "policies/drain-methods", ("never with chemical drain cleaners",)), id="chemical-drain-cleaners"),
    pytest.param(Ask("can you mount a 90 inch tv", "policies/tv-mounting-limits", ("up to 85 inches",)), id="tv-90-inch"),
    pytest.param(Ask("can you mount my tv on the ceiling", "policies/tv-mounting-limits", ("don't mount on ceilings",)), id="tv-ceiling"),
    pytest.param(Ask("can you put an outlet behind my tv", "policies/tv-mounting-limits", ("don't add new outlets",)), id="outlet-behind-tv"),
    pytest.param(Ask("are you licensed and insured", "policies/licensing", ("$2,000,000",)), id="licensed-insured"),
    pytest.param(Ask("hablan espanol", "policies/spanish", ("Spanish",)), id="hablan-espanol"),
    pytest.param(Ask("whats the emergency number", "policies/emergency-line", ("(720) 555-0199",)), id="emergency-number"),
    pytest.param(Ask("can I stop by your office", "policies/no-walk-ins", ("no walk-in service",)), id="stop-by-office"),
    pytest.param(Ask("do you work on sundays", "policies/weekends", ("emergencies only",)), id="sundays"),
    pytest.param(Ask("are you open on thanksgiving", "policies/holidays", ("Thanksgiving Day",)), id="thanksgiving"),
    pytest.param(Ask("can you book me for tuesday at 10", "policies/no-online-booking", ("don't book jobs online",)), id="book-tuesday"),
    pytest.param(Ask("how long is a quote good for", "policies/quote-replies", ("30 days",)), id="quote-validity"),
    pytest.param(Ask("can I buy my own faucet", "policies/parts", ("15%",)), id="own-faucet"),
    pytest.param(Ask("do you pull permits", "policies/water-heater-replacement", ("permit fee is included",)), id="permits"),
    pytest.param(Ask("i rent, can you still fix my sink", "policies/rentals", ("property owner or manager",)), id="renter"),
    pytest.param(Ask("is a clogged sink an emergency", "policies/emergencies"), id="clogged-sink-emergency"),
    pytest.param(Ask("do you clean up after yourselves", "policies/in-home-practices", ("background check",)), id="clean-up"),
    # service area as a policy question
    pytest.param(Ask("do you work in aurora", "policies/outside-area", ("Aurora",)), id="aurora"),
    pytest.param(Ask("do you come to wheat ridge", "policies/wheat-ridge", ("outside our service area",)), id="wheat-ridge"),
    pytest.param(Ask("do you work in central park", "policies/central-park", ("core area",)), id="central-park"),
    # out of scope
    pytest.param(Ask("do you fix furnaces", "policies/not-offered", ("furnaces",)), id="oos-furnaces"),
    pytest.param(Ask("can you replace my sewer line", "policies/not-offered", ("sewer line excavation",)), id="oos-sewer-replacement"),
    pytest.param(Ask("do you do roof repair", "policies/not-offered", ("roofing",)), id="oos-roof"),
    pytest.param(Ask("do you do gutters", "policies/not-offered"), id="oos-gutters"),
    pytest.param(Ask("do you offer financing", None), id="financing-not-in-profile"),
    pytest.param(Ask("do you have gift cards", None), id="gift-cards-not-in-profile"),
    # the profile says nothing about parking: "park" is not the Central Park neighbourhood
    pytest.param(Ask("is there parking", None, (), ("Central Park", "Yes")), id="is-there-parking"),
    pytest.param(Ask("where do I park", None, (), ("Central Park", "Yes")), id="where-do-i-park"),
    # the entry that explains the whole question, not the closest single variant
    pytest.param(Ask("how fast will i get a quote", "policies/quote-turnaround", ("24 hours",)), id="how-fast-quote"),
    pytest.param(Ask("how much do you charge for after hours", "policies/after-hours-billing", ("$150",)), id="after-hours-price"),
    pytest.param(Ask("do you accept checks", "policies/payment", ("checks",)), id="accept-checks"),
    pytest.param(Ask("do you give military discounts", "policies/discounts", ("veterans",)), id="military-discount"),
    pytest.param(Ask("can i cancel my appointment if something comes up", "policies/cancellation", ("$49",)), id="cancel-if-something-comes-up"),
    pytest.param(Ask("is there a fee to cancel within 24 hours", "policies/cancellation", ("$49",)), id="fee-to-cancel-within-24h"),
    pytest.param(Ask("do you do mold removal", "policies/not-offered", ("mold removal",)), id="oos-mold-removal"),
    pytest.param(Ask("can my 16 year old let you in", "policies/someone-home", ("18 or older",)), id="sixteen-year-old"),
    pytest.param(Ask("how many people work there", "policies/team", ("six people",)), id="team-size"),
]


@pytest.mark.parametrize("case", POLICIES)
async def test_policy_phrasing(client: Any, deps: Any, case: Ask) -> None:
    data = await call(client, "ask_policy", {"question": case.question})
    await assert_grounded(deps, data)
    never(data, *case.never)
    if case.entry is None:
        assert data["entry_ref"] is None, f"answered from {data['entry_ref']}: {data['speak']}"
        assert data["handoff"] is not None and data["facts"] == [], data["speak"]
        assert data["speak"] == "I don't know that. Want me to ask Jo?"
        return
    assert data["entry_ref"] == case.entry, f"answered from {data['entry_ref']}: {data['speak']}"
    profile = await deps.profiles.get_live(HOME)
    assert data["speak"] == profile.knowledge_entry(case.entry)["answer_spoken"]  # verbatim, never paraphrased
    says(data, *case.says)


async def test_license_number_is_a_cited_fact(client: Any, deps: Any) -> None:
    """'what's your license number' -> the licensing entry; the numbers ride along as cited facts."""
    data = await call(client, "ask_policy", {"question": "whats your license number"})
    assert data["entry_ref"] == "policies/licensing"
    facts = {f["fact_ref"]: f["value"] for f in data["facts"]}
    assert facts["policies/licensing#facts.master_plumber_license"] == "MP.0048213"
    assert facts["policies/licensing#facts.contractor_license"] == "PC-27715"
    await assert_grounded(deps, data)


# ============================================================================ check_service_area: ZIPs, towns, neighbourhoods


@dataclass(frozen=True)
class Area:
    postal_code: str | None = None
    locality: str | None = None
    served: tuple[bool | None, ...] = (True,)
    """Acceptable `served` values (None = an honest 'can't tell' hand-off)."""


AREAS = [
    pytest.param(Area(postal_code="80211"), id="zip-80211-core"),
    pytest.param(Area(postal_code="80238"), id="zip-80238-central-park-owner-said-core"),
    pytest.param(Area(postal_code="80214"), id="zip-80214-extended"),
    pytest.param(Area(postal_code="80211-1234"), id="zip-plus-four"),
    pytest.param(Area(postal_code="80033", served=(False,)), id="zip-80033-wheat-ridge-owner-said-outside"),
    pytest.param(Area(postal_code="80010", served=(False,)), id="zip-80010-aurora-referred-out"),
    pytest.param(Area(locality="lakewood"), id="lakewood-lowercase"),
    pytest.param(Area(locality="capitol hill"), id="capitol-hill"),
    pytest.param(Area(locality="Washington Park"), id="washington-park"),
    pytest.param(Area(locality="Wheat Ridge", served=(False,)), id="wheat-ridge"),
    pytest.param(Area(locality="Arvada", served=(False,)), id="arvada"),
    pytest.param(Area(locality="Boulder", served=(False,)), id="boulder"),
    # a town or ZIP the areas don't list is a "no" only where the profile says so (policies/outside-area)
    # or when the ZIP is in another region altogether; otherwise the ZIP decides or Jo does
    pytest.param(Area(locality="Denver", served=(None,)), id="denver"),
    pytest.param(Area(locality="Stapleton", served=(True, None)), id="stapleton-unlisted-name"),
    pytest.param(Area(postal_code="80239", served=(None,)), id="zip-80239-unlisted"),
    pytest.param(Area(postal_code="90210", served=(False,)), id="zip-90210-far-away"),
    pytest.param(Area(locality="Aurora", served=(False,)), id="aurora-named-outside"),
    # how people write a listed place
    pytest.param(Area(locality="Lakewood, CO"), id="lakewood-comma-state"),
    pytest.param(Area(locality="Lakewood CO"), id="lakewood-state"),
    pytest.param(Area(locality="Cap Hill", served=(True, None)), id="cap-hill-colloquial"),
    pytest.param(Area(locality="Wash Park", served=(True, None)), id="wash-park-colloquial"),
    pytest.param(Area(locality="Highlands", served=(True, None)), id="highlands-plural"),
]


@pytest.mark.parametrize("case", AREAS)
async def test_service_area_phrasing(client: Any, deps: Any, case: Area) -> None:
    args = {k: v for k, v in (("postal_code", case.postal_code), ("locality", case.locality)) if v is not None}
    data = await call(client, "check_service_area", args)
    await assert_grounded(deps, data)
    assert data["served"] in case.served, data["speak"]
    if data["served"] is True:
        assert data["speak"].startswith("Yes, Kettlewick Home Repair serves"), data["speak"]
    elif data["served"] is False:
        assert data["speak"].startswith("Sorry, Kettlewick Home Repair doesn't serve"), data["speak"]
    else:
        assert data["handoff"] is not None or "ZIP" in data["speak"], data["speak"]


# ============================================================================ hours, location, contact


async def test_are_you_open_right_now(client: Any, deps: Any) -> None:
    """FIXED_NOW is Wednesday 14 Oct 2026, 10:00 in Denver: the office is open 7:30 am to 5:30 pm."""
    data = await call(client, "get_business_info", {"topics": ["hours"]})
    assert data["open_now"] is True
    assert data["speak"].startswith("Kettlewick Home Repair is open now, until 5:30 pm.")
    says(data, "no walk-in service")
    await assert_grounded(deps, data)


async def test_open_on_thanksgiving_by_date(client: Any, deps: Any) -> None:
    data = await call(client, "get_business_info", {"topics": ["hours"], "on_date": "2026-11-26"})
    assert data["speak"].startswith("Kettlewick Home Repair is closed on Thursday, November 26 for Thanksgiving Day.")
    await assert_grounded(deps, data)


async def test_where_are_you_and_whats_your_number(client: Any, deps: Any) -> None:
    data = await call(client, "get_business_info", {"topics": ["location", "contact"]})
    assert data["address"] == "1880 W. Quill Ave, Unit B, Denver, CO 80211"
    assert data["phone"] == "+17205550142"
    says(data, "West Quill Avenue in Denver", "720-555-0142")
    await assert_grounded(deps, data)


# ============================================================================ find_business: how people describe the need


DISCOVERY = [
    pytest.param("my toilet is overflowing", None, HOME, id="toilet-overflowing"),
    pytest.param("handyman to hang a tv", None, HOME, id="hang-a-tv"),
    pytest.param("hot water heater went out", None, HOME, id="water-heater-went-out"),
    pytest.param("plumber", "Lakewood, CO", HOME, id="plumber-lakewood-co"),
    pytest.param("plumber", "Aurora", None, id="plumber-aurora-not-served"),
    pytest.param("electrician", None, None, id="oos-electrician"),
    pytest.param("furnace repair", None, None, id="oos-furnace"),
    pytest.param("someone to put together ikea furniture", None, HOME, id="put-together-ikea-furniture"),
]


@pytest.mark.parametrize(("need", "locality", "winner"), DISCOVERY)
async def test_find_business_phrasing(client: Any, deps: Any, need: str, locality: str | None, winner: str | None) -> None:
    args = {"need": need, **({"locality": locality} if locality else {})}
    data = await call(client, "find_business", args)
    await assert_grounded(deps, data)
    found = [b["business_id"] for b in data["businesses"]]
    if winner is None:
        assert HOME not in found, data["speak"]
    else:
        assert found and found[0] == winner, data["speak"]


# ============================================================================ get_catalog: browsing


@pytest.mark.parametrize(
    ("args", "refs"),
    [
        pytest.param(
            {"query": "plumbing"},
            ["services/leak-repair", "services/water-heater", "services/drain-cleaning", "services/fixture-install", "services/plumbing-labor"],
            id="what-plumbing-do-you-do",
        ),
        pytest.param(
            {"category": "handyman"},
            ["services/drywall-patch", "services/furniture-assembly", "services/tv-mounting", "services/handyman-labor"],
            id="handyman-category",
        ),
        pytest.param({"query": "toilet"}, ["services/drain-cleaning", "services/fixture-install"], id="anything-for-toilets"),
    ],
)
async def test_catalog_phrasing(client: Any, deps: Any, args: dict[str, Any], refs: list[str]) -> None:
    data = await call(client, "get_catalog", {**args, "limit": 25})
    assert [i["item_ref"] for i in data["items"]] == refs, data["speak"]
    await assert_grounded(deps, data)


# ============================================================================ check_availability


async def test_when_can_you_mount_a_tv(client: Any, deps: Any) -> None:
    data = await call(client, "check_availability", {"item": "mount a tv"})
    assert data["item_ref"] == "services/tv-mounting" and data["module_id"] == "quotes"
    assert data["slots"] and all(s["kind"] == "day" for s in data["slots"])
    says(data, "TV mounting")
    await assert_grounded(deps, data)


async def test_when_can_you_replace_my_sewer_line(client: Any) -> None:
    """Kettlewick does not replace sewer lines (policies/not-offered): no drain-cleaning days for one."""
    data = await call(client, "check_availability", {"item": "replace my sewer line"})
    assert data["item_ref"] != "services/drain-cleaning", data["speak"]
    assert not data["slots"], data["speak"]


# ============================================================================ request_quote: the commit path


async def committed(deps: Any) -> list[str | None]:
    items, _ = await deps.commitments.list_for_business(
        HOME, statuses=["pending_owner", "info_requested", "quoted", "declined", "cancelled", "expired"]
    )
    return [c.item_ref for c in items]


QUOTES = [
    pytest.param("mount a 65 inch tv", "80211", "services/tv-mounting", id="quote-65-inch-tv"),
    pytest.param("a leaky faucet", "80202", "services/fixture-install", id="quote-leaky-faucet"),
    pytest.param("leak repair", "80033", None, id="quote-wheat-ridge-refused"),
    pytest.param("clogged kitchen sink", "80211", "services/drain-cleaning", id="quote-clogged-kitchen-sink"),
    # policies/unlisted-zip: if your ZIP isn't listed, send a quote request anyway and Jo will tell you
    pytest.param("leak repair", "80239", "services/leak-repair", id="quote-unlisted-zip-80239"),
]


@pytest.mark.parametrize(("item", "zip_code", "ref"), QUOTES)
async def test_quote_request_phrasing(
    plain_client: Any, deps: Any, elicitation: ElicitationScript, item: str, zip_code: str, ref: str | None
) -> None:
    data = await call(plain_client, "request_quote", {"item": item, "details": {"zip": zip_code, "description": item}})
    if ref is None:
        assert data["status"] == "unavailable", data["speak"]
        assert data["speak"] == "Kettlewick Home Repair doesn't serve that area."
        assert await committed(deps) == []
        return
    assert data["status"] == "pending_owner", data["speak"]
    assert await committed(deps) == [ref]
    confirmation = elicitation.seen[-1][0]
    assert "We don't book jobs online or on the first call." in confirmation  # say_before_confirm, verbatim
    assert data["speak"].startswith("I've sent your quote request to Jo")


async def test_quote_for_a_sewer_line_replacement_is_not_drain_cleaning(plain_client: Any, deps: Any) -> None:
    """Kettlewick does not replace sewer lines (policies/not-offered): nothing is sent to Jo as a drain cleaning."""
    data = await call(
        plain_client, "request_quote", {"item": "replace my sewer line", "details": {"zip": "80211", "description": "replace my sewer line"}}
    )
    assert data["status"] == "not_offered", data["speak"]
    assert await committed(deps) == [], data["speak"]


async def test_a_misspelt_job_is_confirmed_before_it_is_quoted(plain_client: Any, deps: Any) -> None:
    """'watter heater' is a 'Did you mean ...?' naming the water heater job; nothing is sent to Jo yet."""
    data = await call(plain_client, "request_quote", {"item": "watter heater", "details": {"zip": "80211", "description": "watter heater"}})
    assert data["status"] == "needs_info" and data["speak"] == "Did you mean water heater repair or replacement?"
    assert [a["arguments"]["item"] for a in data["card"]["actions"]] == ["services/water-heater"]
    assert await committed(deps) == []
