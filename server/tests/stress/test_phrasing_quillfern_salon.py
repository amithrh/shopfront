"""Phrasing stress test: Quillfern Hair Studio (`quillfern-salon`).

Each case is something a voice assistant plausibly passes through from a customer (item names with
modifiers, misspellings, plurals, colloquial words, hair lengths and head counts, allergy and
"dietary" needs, price, policy, hours and availability questions, and things the studio does not do),
driven in-process over the root conftest's `client` / `plain_client` fixtures, which serve live copies
of the gold profiles. The expected behaviour is what `profiles/quillfern-salon.json` says:

  * the right item (and hair-length choice) resolved, with its published price;
  * the right policy entry, spoken verbatim;
  * a "Did you mean ...?" whose options include the right item when the words are genuinely ambiguous;
  * an honest hand-off to Inés when the profile does not say -- never a wrong item or an invented fact.

Every reply is also checked for grounding (`assert_grounded`: spoken-safe, every fact statable) and for
dollar amounts the profile never publishes.

Known product gaps are `xfail(strict=True)` with their root cause, so the suite stays green until they
are fixed (and turns red, prompting an update here, the moment one is fixed).

Clock: support.FIXED_NOW = Wednesday 14 Oct 2026, 11:00 in Austin (America/Chicago).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from support import ElicitationScript

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))  # `tools_support` (importlib mode)

from tools_support import INTAKE, SALON, assert_grounded  # noqa: E402

pytestmark = pytest.mark.anyio

# Dates relative to FIXED_NOW (Wed 2026-10-14, 11:00 CDT).
TODAY, THU, SAT, SUN, MON, NEXT_WED = "2026-10-14", "2026-10-15", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-21"
THANKSGIVING, CHRISTMAS_EVE = "2026-11-26", "2026-12-24"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

COLOR_ITEMS = (
    "services/color-consult", "services/patch-test", "services/root-touch-up", "services/all-over-color",
    "services/partial-highlights", "services/full-highlights", "services/balayage", "services/color-correction",
)  # fmt: skip


# ---------------------------------------------------------------------------- fixtures


@pytest.fixture
def elicitation() -> ElicitationScript:
    """Overrides the root fixture: answers every salon intake field (stylist, name, phone, email)."""
    return ElicitationScript(intake=dict(INTAKE))


@pytest.fixture(scope="module")
def gold(raw_profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return raw_profiles[SALON]


def _cents(value: Any) -> list[int]:
    if isinstance(value, dict):
        return [
            c
            for k, v in value.items()
            for c in ([v] if (k == "amount_cents" or k.endswith("_cents")) and isinstance(v, int) else _cents(v))
        ]
    if isinstance(value, list):
        return [c for v in value for c in _cents(v)]
    return []


@pytest.fixture(scope="module")
def gold_dollars(gold: dict[str, Any]) -> set[str]:
    """Every dollar amount the profile publishes, as speech writes it ("$42", "$12.50")."""
    return {f"${c // 100}" if c % 100 == 0 else f"${c / 100:.2f}" for c in _cents(gold)}


@pytest.fixture(scope="module")
def gold_answers(gold: dict[str, Any]) -> dict[str, str]:
    return {f"info/{e['id']}": e["answer_spoken"] for e in gold["modules"]["info"]["entries"]}


async def call(client: Any, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    payload = {"business_id": SALON, **args} if tool != "find_business" else args
    result = await client.call_tool(tool, payload)
    assert result.is_error is False, result.content
    assert result.structured_content is not None, result
    return dict(result.structured_content)


async def grounded(deps: Any, data: dict[str, Any], gold_dollars: set[str]) -> None:
    """No fact without a statable source, and no dollar amount the studio never published."""
    await assert_grounded(deps, data)
    invented = set(re.findall(r"\$\d+(?:\.\d\d)?", data["speak"])) - gold_dollars
    assert not invented, f"speak names amounts the profile never publishes: {invented} in {data['speak']!r}"


# ---------------------------------------------------------------------------- expectations


@dataclass(frozen=True)
class Item:
    """Resolved to exactly `ref`, no hand-off. `focus` = {option group: choice ids} the words point at."""

    ref: str
    says: tuple[str, ...] = ()
    never: tuple[str, ...] = ()
    focus: tuple[tuple[str, tuple[str, ...]], ...] | None = None
    fields: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class Asks:
    """A "Did you mean ...?" whose options include every ref in `includes`."""

    includes: tuple[str, ...]


@dataclass(frozen=True)
class HandsOff:
    """An honest hand-off to Inés: no price spoken, no item unless `item` names the one it is about."""

    reasons: tuple[str, ...] = ("not_offered", "unknown")
    item: str | None = None
    never: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    """ask_policy answered from `entry`, verbatim."""

    entry: str
    says: tuple[str, ...] = ()
    never: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnyOf:
    options: tuple[Any, ...]


HANDOFF = HandsOff()


def resolved(data: dict[str, Any]) -> str | None:
    return (data.get("item") or {}).get("item_ref") or data.get("item_ref")


def mismatch(data: dict[str, Any], expect: Any, answers: dict[str, str]) -> str | None:
    """Why `data` is not `expect` (None when it is)."""
    speak = data["speak"]
    if isinstance(expect, AnyOf):
        problems = [mismatch(data, option, answers) for option in expect.options]
        return None if any(p is None for p in problems) else " / ".join(str(p) for p in problems)
    if isinstance(expect, Item):
        if resolved(data) != expect.ref:
            return f"resolved to {resolved(data)!r} (expected {expect.ref}); said {speak!r}"
        if data.get("handoff") is not None:
            return f"handed off ({data['handoff']['reason']}) instead of answering: {speak!r}"
        missing = [s for s in expect.says if s not in speak]
        if missing:
            return f"{speak!r} does not say {missing}"
        wrong = [s for s in expect.never if s in speak]
        if wrong:
            return f"{speak!r} says {wrong}"
        if expect.focus is not None:
            matched = {g["group_id"]: tuple(c["choice_id"] for c in g["choices"]) for g in data.get("matched_options") or ()}
            if matched != dict(expect.focus):
                return f"matched options {matched}, expected {dict(expect.focus)}"
        for key, value in expect.fields:
            if data.get(key) != value:
                return f"{key}={data.get(key)!r}, expected {value!r}; said {speak!r}"
        return None
    if isinstance(expect, Asks):
        if resolved(data) is not None:
            return f"silently resolved to {resolved(data)!r}: {speak!r}"
        if not speak.startswith("Did you mean"):
            return f"no question asked: {speak!r}"
        options = [f["fact_ref"].split("#")[0] for f in data.get("facts") or () if f["fact_ref"].endswith("#spoken_name")]
        missing = [r for r in expect.includes if r not in options]
        return f"question {speak!r} leaves out {missing}" if missing else None
    if isinstance(expect, HandsOff):
        hand = data.get("handoff")
        if hand is None:
            return f"answered instead of handing off (resolved {resolved(data)!r}): {speak!r}"
        if hand["reason"] not in expect.reasons:
            return f"hand-off reason {hand['reason']!r}, expected one of {expect.reasons}: {speak!r}"
        if hand["tool"] != "ask_owner" or hand["approver_spoken_name"] != "Inés" or "Inés" not in speak:
            return f"hand-off does not offer to ask Inés: {hand} / {speak!r}"
        if resolved(data) != expect.item:
            return f"hand-off about {resolved(data)!r}, expected {expect.item!r}: {speak!r}"
        if "$" in speak:
            return f"hand-off speaks a price: {speak!r}"
        if data.get("entry_ref"):
            return f"hand-off still cites {data['entry_ref']}: {speak!r}"
        wrong = [s for s in expect.never if s.casefold() in speak.casefold()]
        return f"{speak!r} says {wrong}" if wrong else None
    if isinstance(expect, Answer):
        if data.get("entry_ref") != expect.entry:
            got = data.get("entry_ref") or f"a hand-off ({(data.get('handoff') or {}).get('reason')})"
            return f"answered from {got}, expected {expect.entry}: {speak!r}"
        if speak != answers[expect.entry]:
            return f"not verbatim: {speak!r} != {answers[expect.entry]!r}"
        missing = [s for s in expect.says if s not in speak]
        wrong = [s for s in expect.never if s.casefold() in speak.casefold()]
        return f"{speak!r} misses {missing} / says {wrong}" if missing or wrong else None
    raise TypeError(expect)


def xfail(reason: str) -> Any:
    return pytest.mark.xfail(strict=True, reason=reason)


# ---------------------------------------------------------------------------- find_item: resolution


FIND_ITEM: list[Any] = [
    # -- cuts
    pytest.param("Women's Haircut", None, Item("services/womens-cut", says=("$78",)), id="cut-womens-caps-apostrophe"),
    pytest.param("womens haircut", None, Item("services/womens-cut", says=("$78",)), id="cut-womens-no-apostrophe"),
    pytest.param(
        "a ladies cut and blow dry", None,
        AnyOf((Item("services/womens-cut"), Asks(("services/womens-cut",)))),
        id="cut-ladies-cut-and-blow-dry",
    ),
    pytest.param("mens haircut", None, Item("services/mens-cut", says=("$42",)), id="cut-mens"),
    pytest.param("how much is a mens haircut", ["price"], Item("services/mens-cut", says=("$42",)), id="cut-mens-price-question"),
    pytest.param("two men's haircuts", None, Item("services/mens-cut", says=("$42",)), id="cut-mens-quantity"),
    pytest.param(
        "a men's trim", None,
        AnyOf((Item("services/mens-cut"), Asks(("services/mens-cut",)))),
        id="cut-mens-trim",
    ),
    pytest.param("haircut and beard", None, Item("services/mens-cut-beard", says=("$55",)), id="cut-haircut-and-beard"),
    pytest.param("beard trim", None, Item("services/mens-cut-beard", says=("men's cut and beard trim", "$55")), id="cut-beard-trim-names-combo"),
    pytest.param("boys haircut", None, Item("services/kids-cut", says=("$30",)), id="cut-boys"),
    pytest.param("two kids haircuts", None, Item("services/kids-cut", says=("$30",)), id="cut-kids-quantity"),
    pytest.param(
        "kids cut for my 14 year old", None,
        AnyOf((Item("services/kids-cut", says=("12 and under",)), HANDOFF)),
        id="cut-kids-over-age",
    ),
    pytest.param(
        "a haircut for my son", None,
        AnyOf((Item("services/kids-cut"), Item("services/mens-cut"), Asks(("services/kids-cut",)))),
        id="cut-for-my-son",
    ),
    pytest.param(
        "haircut for my 8 year old daughter", None,
        AnyOf((Item("services/kids-cut"), Asks(("services/kids-cut",)))),
        id="cut-for-8-year-old-daughter",
    ),
    pytest.param("cut for curly hair", None, Item("services/curly-cut", says=("$95",)), id="cut-curly"),
    pytest.param("fringe", None, Item("services/bang-trim", says=("$15",)), id="cut-fringe-british"),
    pytest.param("trim my bangs", None, Item("services/bang-trim", says=("$15",)), id="cut-trim-my-bangs"),
    pytest.param(
        "buzz cut", None, AnyOf((Item("services/mens-cut"), Asks(("services/mens-cut",)), HANDOFF)), id="cut-buzz-cut"
    ),
    # -- styling
    pytest.param("blow dry", None, Item("services/blowout", says=("$55",)), id="style-blow-dry"),
    pytest.param("blowdry", None, Item("services/blowout", says=("$55",)), id="style-blowdry-one-word"),
    pytest.param("blow-out", None, Item("services/blowout", says=("$55",)), id="style-blow-out-hyphen"),
    pytest.param("blowout with beach waves", None, Item("services/blowout-hot-tool", says=("$70",)), id="style-blowout-beach-waves"),
    pytest.param(
        "a blow out with curls", None, Item("services/blowout-hot-tool", says=("$70",), never=("$55",)),
        id="style-blow-out-with-curls",
    ),
    pytest.param(
        "brazilian blowout", None,
        AnyOf((Item("services/smoothing"), Asks(("services/smoothing",)), HANDOFF)),
        id="style-brazilian-blowout",
    ),
    pytest.param("updo for a wedding", None, Item("services/updo", says=("$110",)), id="style-updo-wedding"),
    # -- color
    pytest.param("balayages", None, Item("services/balayage", says=("$245", "$285")), id="color-balayage-plural"),
    pytest.param("bayalage", None, Item("services/balayage", says=("$245",)), id="color-balayage-listed-misspelling"),
    pytest.param(
        "balayge", None, Item("services/balayage"), id="color-balayage-unlisted-misspelling",
    ),
    pytest.param(
        "balayage for shoulder length hair", None,
        Item("services/balayage", says=("$245",), never=("$285",), focus=(("length", ("shoulder-or-shorter",)),)),
        id="color-balayage-shoulder-length",
    ),
    pytest.param(
        "balayage, my hair is really long", None,
        Item("services/balayage", says=("$285",), never=("$245",), focus=(("length", ("past-shoulder-blades",)),)),
        id="color-balayage-really-long",
    ),
    pytest.param(
        "balayage for medium length hair", None, Item("services/balayage", says=("$245", "$285"), focus=()),
        id="color-balayage-medium-asks-both-lengths",
    ),
    pytest.param("get my roots done", None, Item("services/root-touch-up", says=("$95",)), id="color-roots-done"),
    pytest.param("root touchup", None, Item("services/root-touch-up", says=("$95",)), id="color-root-touchup"),
    pytest.param("full head of highlights", None, Item("services/full-highlights", says=("$195",)), id="color-full-head-highlights"),
    pytest.param("half head foils", None, Item("services/partial-highlights", says=("$150",)), id="color-half-head-foils"),
    pytest.param(
        "highlights for long hair", None,
        Asks(("services/partial-highlights", "services/full-highlights")),
        id="color-highlights-long-hair",
    ),
    pytest.param(
        "color for long hair", None,
        AnyOf((Item("services/all-over-color"), Asks(("services/all-over-color",)))),
        id="color-color-long-hair",
    ),
    pytest.param(
        "hair color", None,
        AnyOf((Item("services/all-over-color", says=("$120",)), Asks(("services/all-over-color",)))),
        id="color-hair-color",
    ),
    pytest.param(
        "dye my hair", None,
        AnyOf((Item("services/all-over-color", says=("$120",)), Asks(("services/all-over-color",)))),
        id="color-dye-my-hair",
    ),
    pytest.param(
        "single process colour", None, Item("services/all-over-color", says=("$120",)),
        id="color-single-process-colour",
    ),
    pytest.param(
        "my color is orange, can you fix it", None, Item("services/color-correction", says=("$90 per hour",)),
        id="color-orange-fix-it",
    ),
    pytest.param("colour correction", None, Item("services/color-correction", says=("$90 per hour",)), id="color-colour-correction"),
    pytest.param("is the color consultation free", ["price"], Item("services/color-consult", says=("free",)), id="color-consult-free"),
    pytest.param("allergy test for hair dye", None, Item("services/patch-test", says=("free",)), id="color-allergy-test"),
    pytest.param(
        "cover my grays", None,
        AnyOf((HANDOFF, Item("services/root-touch-up"), Item("services/all-over-color"), Asks(("services/root-touch-up",)))),
        id="color-cover-grays",
    ),
    pytest.param("babylights", None, AnyOf((HANDOFF, Asks(("services/partial-highlights",)))), id="color-babylights"),
    # -- treatments
    pytest.param("deep conditioner", None, Item("services/deep-conditioning", says=("$30",)), id="treat-deep-conditioner"),
    pytest.param("frizz treatment", None, Item("services/smoothing", says=("$260",)), id="treat-frizz"),
    pytest.param("keratin treatment", None, AnyOf((Item("services/smoothing"), HANDOFF)), id="treat-keratin"),
    pytest.param("scalp massage", None, AnyOf((Item("services/scalp-treatment"), HANDOFF)), id="treat-scalp-massage"),
    # -- out of scope: an honest "I don't see that", never a near item
    pytest.param("manicure", None, HandsOff(("not_offered",)), id="oos-manicure"),
    pytest.param("eyebrow wax", None, HandsOff(("not_offered",)), id="oos-eyebrow-wax"),
    pytest.param("hair extensions", None, HandsOff(("not_offered",)), id="oos-hair-extensions"),
    pytest.param("perm", None, HandsOff(("not_offered",)), id="oos-perm"),
    # -- "dietary" and allergy modifiers: never claimed without an owner-confirmed fact
    pytest.param("vegan hair dye", None, HANDOFF, id="diet-vegan-hair-dye"),
    pytest.param("gluten free shampoo", None, HANDOFF, id="diet-gluten-free-shampoo"),
    pytest.param(
        "nut free blowout", None,
        HandsOff(("unknown",), item="services/blowout", never=("blowout is nut-free",)),
        id="diet-nut-free-blowout",
    ),
]


@pytest.mark.parametrize(("item", "aspects", "expect"), FIND_ITEM)
async def test_find_item_phrasing(
    client: Any, deps: Any, gold_answers: dict[str, str], gold_dollars: set[str], item: str, aspects: list[str] | None, expect: Any
) -> None:
    args: dict[str, Any] = {"item": item}
    if aspects:
        args["aspects"] = aspects
    data = await call(client, "find_item", args)
    problem = mismatch(data, expect, gold_answers)
    assert problem is None, problem
    await grounded(deps, data, gold_dollars)


# ---------------------------------------------------------------------------- find_item: what was asked about


ASPECTS: list[Any] = [
    pytest.param("how much is balayage", ["price"], Item("services/balayage", says=("starts at $245",)), id="price-balayage-from"),
    pytest.param(
        "color correction", ["price", "duration", "description"],
        Item("services/color-correction", says=("$90 per hour", "at least 2 hours", "consultation is required first")),
        id="price-color-correction-per-hour",
    ),
    pytest.param("men's cut", ["duration"], Item("services/mens-cut", says=("30 minutes",)), id="duration-mens-cut"),
    pytest.param(
        "how long does balayage take", ["duration"], Item("services/balayage", says=("hours",)),
        id="duration-balayage",
    ),
    pytest.param(
        "balayage for long hair", ["duration"], Item("services/balayage", says=("4 hours",)),
        id="duration-balayage-long-hair",
    ),
    pytest.param(
        "when can I get a curly cut", ["availability"],
        Item("services/curly-cut", says=("Only June", "Wednesday, Thursday, Saturday and Sunday")),
        id="availability-curly-cut-only-june",
    ),
    pytest.param("who does balayage", ["availability"], Item("services/balayage", says=("Only Inés",)), id="availability-who-does-balayage"),
    pytest.param("kids cut", ["price", "description"], Item("services/kids-cut", says=("$30", "12 and under")), id="description-kids-cut-age"),
    pytest.param(
        "partial highlights", ["description", "price"], Item("services/partial-highlights", says=("toner and a blowout", "$150")),
        id="description-partial-highlights",
    ),
    pytest.param(
        "is there anything in the women's cut I could be allergic to", ["allergens"],
        HandsOff(("unknown",), item="services/womens-cut", never=("kitchen",)),
        id="allergens-womens-cut-no-kitchen",
    ),
]


@pytest.mark.parametrize(("item", "aspects", "expect"), ASPECTS)
async def test_find_item_aspects(
    client: Any, deps: Any, gold_answers: dict[str, str], gold_dollars: set[str], item: str, aspects: list[str], expect: Any
) -> None:
    data = await call(client, "find_item", {"item": item, "aspects": aspects})
    problem = mismatch(data, expect, gold_answers)
    assert problem is None, problem
    await grounded(deps, data, gold_dollars)


# ---------------------------------------------------------------------------- ask_policy


POLICY: list[Any] = [
    # -- cancelling and rescheduling
    pytest.param("can I cancel", Answer("info/cancellation", says=("24 hours",)), id="cancel-can-i"),
    pytest.param("what's your cancelation policy?", Answer("info/cancellation"), id="cancel-misspelled"),
    pytest.param("is there a cancellation fee", Answer("info/cancellation", says=("50 percent",)), id="cancel-fee"),
    pytest.param("do you charge a fee for late cancellations", Answer("info/cancellation"), id="cancel-late-fee"),
    pytest.param("how much do you charge if I don't show up", Answer("info/cancellation", says=("no-show",)), id="cancel-no-show-charge"),
    pytest.param("If I cancel the day before, do I get charged?", Answer("info/cancellation"), id="cancel-day-before-charged"),
    pytest.param("what's the no-show fee", Answer("info/cancellation"), id="cancel-no-show-fee"),
    pytest.param("can I move my appointment to next week", Answer("info/how-to-cancel"), id="cancel-move-appointment"),
    # -- payment
    pytest.param("do you take cards", Answer("info/payment"), id="pay-cards"),
    pytest.param("do you take credit cards?", Answer("info/payment"), id="pay-credit-cards"),
    pytest.param("can I pay with Apple Pay", Answer("info/payment", says=("tap-to-pay",)), id="pay-apple-pay"),
    pytest.param("do you take checks", Answer("info/payment", says=("Checks aren't accepted",)), id="pay-checks"),
    pytest.param("do you accept Venmo", AnyOf((Answer("info/payment"), HandsOff(never=("venmo",)))), id="pay-venmo"),
    pytest.param("can I tip on my card", Answer("info/tips"), id="pay-tip-on-card"),
    # -- parking and getting there
    pytest.param("is there parking", Answer("info/parking", says=("free parking for 6 cars",)), id="park-is-there"),
    pytest.param("where do I park my car", Answer("info/parking"), id="park-where"),
    pytest.param("is parking free?", Answer("info/parking"), id="park-free"),
    pytest.param("where is the entrance", Answer("info/entrance", says=("Suite B",)), id="park-entrance"),
    # -- walk-ins and booking
    pytest.param("do you take walk-ins", Answer("info/walk-ins", says=("doesn't take walk-ins",)), id="walkin-do-you"),
    pytest.param("can I just walk in for a haircut today", Answer("info/walk-ins"), id="walkin-just-walk-in-today"),
    pytest.param("can I come in without an appointment", Answer("info/walk-ins"), id="walkin-without-appointment"),
    pytest.param("can I pick my stylist", Answer("info/stylist-choice"), id="book-pick-stylist"),
    pytest.param("can I request Wren", Answer("info/stylist-choice"), id="book-request-wren"),
    pytest.param("how far in advance can I book", Answer("info/booking-rules", says=("8 weeks",)), id="book-how-far-ahead"),
    pytest.param("how will I know my appointment is confirmed", Answer("info/email-confirmation"), id="book-confirmation"),
    # -- deposits
    pytest.param("do I need a deposit for highlights", Answer("info/deposit", says=("$50",)), id="deposit-highlights"),
    pytest.param("how much is the deposit", Answer("info/deposit", says=("$50",)), id="deposit-how-much"),
    pytest.param("can I get my deposit back if I cancel", Answer("info/deposit-refund"), id="deposit-back-if-cancel"),
    pytest.param("is the deposit refundable", Answer("info/deposit-refund"), id="deposit-refundable"),
    # -- lateness, redo, gift cards
    pytest.param("what if I'm running late", Answer("info/late-arrival", says=("15 minutes",)), id="late-running"),
    pytest.param("I'm going to be 20 minutes late", Answer("info/late-arrival"), id="late-20-minutes"),
    pytest.param("what if I don't like my color", Answer("info/redo", says=("7 days",)), id="redo-dont-like-color"),
    pytest.param("what if I hate my haircut", Answer("info/redo"), id="redo-hate-haircut"),
    pytest.param("do you sell gift cards", Answer("info/gift-cards"), id="gift-sell"),
    pytest.param("do gift certificates expire", Answer("info/gift-cards", says=("never expire",)), id="gift-certificates-expire"),
    # -- accessibility
    pytest.param("is the salon wheelchair accessible", Answer("info/accessibility"), id="access-wheelchair"),
    pytest.param("do you have an accessible bathroom", Answer("info/accessibility"), id="access-bathroom"),
    pytest.param(
        "can I bring my dog", AnyOf((Answer("info/accessibility", never=("pets",)), HandsOff(never=("pets",)))), id="access-dog"
    ),
    # -- allergies and ingredients (safety facts the owner confirmed)
    pytest.param("I'm allergic to latex", Answer("info/gloves", says=("latex-free",)), id="safety-latex"),
    pytest.param("I have a nut allergy", Answer("info/finishing-oil", says=("argan oil",)), id="safety-nut-allergy"),
    pytest.param(
        "does the keratin treatment have formaldehyde", Answer("info/smoothing-ingredients"), id="safety-formaldehyde-keratin"
    ),
    pytest.param("is there a persulfate-free bleach", Answer("info/lightener", says=("no persulfate-free",)), id="safety-persulfate-bleach"),
    pytest.param("do you have fragrance free shampoo", Answer("info/fragrance-free"), id="safety-fragrance-free"),
    pytest.param("do you have PPD free hair dye", Answer("info/hair-color-ppd", says=("PPD-free",)), id="safety-ppd-free-dye"),
    pytest.param("do I need a patch test", Answer("info/patch-test", says=("72 hours",)), id="safety-patch-test"),
    pytest.param("I reacted to hair dye before", Answer("info/patch-test"), id="safety-reacted-before"),
    # -- weddings and house calls
    pytest.param("do you do bridal hair", Answer("info/wedding-parties"), id="event-bridal-hair"),
    pytest.param(
        "can you do hair for my bridesmaids at the venue", Answer("info/wedding-parties", says=("studio only",)),
        id="event-bridesmaids-venue",
    ),
    pytest.param(
        "can you come to my house to do my hair",
        AnyOf((Answer("info/wedding-parties"), HandsOff(never=("we can come", "house calls"))),),
        id="event-house-call",
    ),
    # -- kids and teens
    pytest.param("how old does my kid have to be for the kids cut", Answer("info/minors-color", says=("12 and under",)), id="minor-kids-cut-age"),
    pytest.param("can my teenager get her hair colored", Answer("info/minors-color", says=("under 16",)), id="minor-teen-color"),
    pytest.param("can my 14 year old get highlights without me", Answer("info/minors-color"), id="minor-14-highlights-alone"),
    # -- hours / about
    pytest.param("can I get color on Sunday", Answer("info/sunday-services"), id="hours-color-sunday"),
    pytest.param("who owns the salon", Answer("info/about", says=("Inés Calderón",)), id="about-owner"),
    pytest.param("how many stylists work there", Answer("info/about"), id="about-how-many-stylists"),
    # -- out of scope: hand off, never guess
    pytest.param("do you offer student discounts", HandsOff(never=("discount",)), id="oos-student-discount"),
    pytest.param("what's the wifi password", HANDOFF, id="oos-wifi"),
    pytest.param("do you do manicures", HANDOFF, id="oos-manicures"),
]


@pytest.mark.parametrize(("question", "expect"), POLICY)
async def test_ask_policy_phrasing(
    client: Any, deps: Any, gold_answers: dict[str, str], gold_dollars: set[str], question: str, expect: Any
) -> None:
    data = await call(client, "ask_policy", {"question": question})
    problem = mismatch(data, expect, gold_answers)
    assert problem is None, problem
    await grounded(deps, data, gold_dollars)


# ---------------------------------------------------------------------------- get_business_info (hours, place, phone)


INFO: list[Any] = [
    pytest.param(
        "what are your hours", {"topics": ["hours"]},
        ("Thursday, 10 am to 8 pm", "Sunday, 11 am to 4 pm"), ("Monday",), id="hours-week",
    ),
    pytest.param("are you open Monday", {"topics": ["hours"], "on_date": MON}, ("closed on Monday",), ("am to",), id="hours-monday-closed"),
    pytest.param(
        "are you open on Thanksgiving", {"topics": ["hours"], "on_date": THANKSGIVING},
        ("closed on Thursday, November 26 for Thanksgiving Day",), ("10 am to 8 pm",), id="hours-thanksgiving",
    ),
    pytest.param(
        "are you open Christmas Eve", {"topics": ["hours"], "on_date": CHRISTMAS_EVE},
        ("closed", "Christmas Eve"), (), id="hours-christmas-eve",
    ),
    pytest.param("what time do you close Sunday", {"topics": ["hours"], "on_date": SUN}, ("11 am to 4 pm",), (), id="hours-sunday"),
    pytest.param("how late are you open Thursday", {"topics": ["hours"], "on_date": THU}, ("10 am to 8 pm",), (), id="hours-thursday-late"),
    pytest.param("where are you", {"topics": ["location"]}, ("East Marigold Street",), (), id="place-where"),
    pytest.param("what's your phone number", {"topics": ["contact"]}, ("512-555-0147",), (), id="place-phone"),
]


@pytest.mark.parametrize(("_said", "args", "says", "never"), INFO)
async def test_business_info_phrasing(
    client: Any, deps: Any, gold_dollars: set[str], _said: str, args: dict[str, Any], says: tuple[str, ...], never: tuple[str, ...]
) -> None:
    data = await call(client, "get_business_info", args)
    assert all(s in data["speak"] for s in says), data["speak"]
    assert not any(s in data["speak"] for s in never), data["speak"]
    assert data["handoff"] is None
    if args["topics"] == ["hours"] and "on_date" not in args:
        assert all("mon" not in line["days"] for line in data["hours"]), data["hours"]  # closed Mondays
    if args["topics"] == ["location"]:
        assert data["address"] == "1906 East Marigold Street, Suite B, Austin, TX 78702"
    if args["topics"] == ["contact"]:
        assert data["phone"] == "+15125550147"
    await grounded(deps, data, gold_dollars)


# ---------------------------------------------------------------------------- check_availability


@dataclass(frozen=True)
class Slots:
    """Open times for `ref`, every one with a stylist in `with_`, on `weekdays`, from `not_before` on."""

    ref: str
    with_: tuple[str, ...]
    weekdays: tuple[str, ...] | None = None
    not_before: str | None = None


@dataclass(frozen=True)
class NoSlots:
    """No open time for `ref`, said honestly (optionally offering to ask Inés)."""

    ref: str


AVAILABILITY: list[Any] = [
    pytest.param(
        {"item": "curly cut", "date_from": THU, "date_to": NEXT_WED},
        Slots("services/curly-cut", ("june",), ("wed", "thu", "sat", "sun")), id="curly-cut-next-week-only-june",
    ),
    pytest.param(
        {"item": "men's cut", "date_from": THU, "date_to": THU, "resource": "Wren"},
        Slots("services/mens-cut", ("wren",), ("thu",)), id="mens-cut-with-wren-thursday",
    ),
    pytest.param(
        {"item": "blow dry", "date_from": TODAY, "date_to": TODAY},
        Slots("services/blowout", ("june",), ("wed",), not_before=f"{TODAY}T11:00"), id="blow-dry-today-after-now",
    ),
    pytest.param(
        {"item": "balayage for long hair", "date_from": SAT, "date_to": SAT},
        Slots("services/balayage", ("ines",), ("sat",)), id="balayage-long-saturday-ines",
    ),
    pytest.param({"item": "balayage", "date_from": SUN, "date_to": SUN}, NoSlots("services/balayage"), id="balayage-sunday-no-color"),
    pytest.param({"item": "all over color", "date_from": SUN, "date_to": SUN}, NoSlots("services/all-over-color"), id="color-sunday-no-color"),
    pytest.param(
        {"item": "men's cut and beard trim", "date_from": NEXT_WED, "date_to": NEXT_WED},
        NoSlots("services/mens-cut-beard"), id="beard-trim-wednesday-wren-off",
    ),
    pytest.param({"item": "women's haircut", "date_from": MON, "date_to": MON}, NoSlots("services/womens-cut"), id="womens-cut-monday-closed"),
    pytest.param(
        {"item": "women's haircut", "date_from": THANKSGIVING, "date_to": THANKSGIVING}, NoSlots("services/womens-cut"),
        id="womens-cut-thanksgiving",
    ),
    pytest.param(
        {"item": "all over color", "date_from": THU, "date_to": NEXT_WED, "resource": "june"}, NoSlots("services/all-over-color"),
        id="color-with-june-who-does-no-color",
    ),
    pytest.param(
        {"item": "haircut", "date_from": SAT, "date_to": SAT},
        Asks(("services/womens-cut", "services/mens-cut")), id="haircut-generic-asks",
    ),
    pytest.param({"item": "manicure"}, HandsOff(("not_offered",)), id="manicure-not-offered"),
]


@pytest.mark.parametrize(("args", "expect"), AVAILABILITY)
async def test_check_availability_phrasing(
    client: Any, deps: Any, gold_answers: dict[str, str], gold_dollars: set[str], args: dict[str, Any], expect: Any
) -> None:
    data = await call(client, "check_availability", args)
    if isinstance(expect, Slots):
        assert data["item_ref"] == expect.ref, data["speak"]
        assert data["slots"], f"no open times: {data['speak']!r}"
        for slot in data["slots"]:
            assert slot["resource_id"] in expect.with_, (slot, data["speak"])
            if expect.weekdays is not None:
                assert WEEKDAYS[date.fromisoformat(slot["start"][:10]).weekday()] in expect.weekdays, slot
            if expect.not_before is not None:
                assert slot["start"][:16] >= expect.not_before, slot
        assert data["handoff"] is None
    elif isinstance(expect, NoSlots):
        assert data["item_ref"] == expect.ref, data["speak"]
        assert data["slots"] == [], data["slots"]
        assert data["speak"].startswith("I couldn't find an open time"), data["speak"]
        assert data["handoff"] is None or data["handoff"]["reason"] == "needs_owner"
    else:
        problem = mismatch(data, expect, gold_answers)
        assert problem is None, problem
        assert data["slots"] == []
    await grounded(deps, data, gold_dollars)


async def test_two_kids_are_not_offered_one_stylist_slot(client: Any) -> None:
    data = await call(client, "check_availability", {"item": "kids haircuts", "date_from": SAT, "date_to": SAT, "party_size": 2})
    assert data["item_ref"] == "services/kids-cut"
    single = [s for s in data["slots"] if s["resource_id"] is not None]
    assert not single, f"a party of 2 offered single-stylist slots: {data['speak']!r}"
    # Instead: times when two stylists are free together, one appointment each.
    assert data["slots"] and all(" and " in s["resource_spoken_name"] for s in data["slots"]), data["slots"]
    assert data["speak"].startswith("Each appointment is for one person, so here are times with two free together:"), data["speak"]
    assert data["speak"].endswith("I'll book each one separately."), data["speak"]


async def test_booking_two_kids_into_one_stylist_slot_is_refused_with_the_reason(plain_client: Any) -> None:
    data = await call(plain_client, "book", {"item": "kids haircut", "start": f"{SAT}T10:00", "party_size": 2})
    assert data["status"] == "needs_info" and data["missing_fields"][0] == "party_size", data
    assert data["speak"].startswith("Each appointment is for one person"), data["speak"]


# ---------------------------------------------------------------------------- book: the phrase reaches the commitment


async def test_book_blowout_with_curls_books_the_hot_tool_blowout(plain_client: Any) -> None:
    data = await call(plain_client, "book", {"item": "blowout with curls", "start": f"{SAT}T10:00"})
    assert data["status"] == "confirmed", data["speak"]
    assert data["commitment"]["item_ref"] == "services/blowout-hot-tool"
    assert "$70" in data["speak"]


async def test_book_a_blow_out_with_curls_books_the_hot_tool_blowout(plain_client: Any) -> None:
    data = await call(plain_client, "book", {"item": "a blow out with curls", "start": f"{SAT}T10:00"})
    committed = (data.get("commitment") or {}).get("item_ref")
    assert committed in (None, "services/blowout-hot-tool"), f"committed {committed}: {data['speak']!r}"
    assert "$55" not in data["speak"], data["speak"]


async def test_book_balayage_for_long_hair_does_not_ask_the_length_again(plain_client: Any) -> None:
    data = await call(plain_client, "book", {"item": "balayage for long hair", "start": f"{SAT}T10:00", "details": {"stylist": "ines"}})
    assert "length" not in data.get("missing_fields", []), data
    assert "shoulder" not in data["speak"], data["speak"]


@pytest.mark.parametrize(
    ("args", "status", "says"),
    [
        pytest.param(
            {"item": "balayage", "start": f"{SAT}T13:15", "choices": {"length": "long hair"}, "details": {"stylist": "ines"}},
            "unavailable", "outside the hours", id="balayage-long-1-15-saturday-ends-after-close",
        ),
        pytest.param(
            {"item": "balayage", "start": f"{SAT}T13:45", "choices": {"length": "short hair"}, "details": {"stylist": "ines"}},
            "unavailable", "outside the hours", id="balayage-short-1-45-saturday-ends-after-close",
        ),
        pytest.param(
            {"item": "balayage", "start": f"{SAT}T13:30", "choices": {"length": "short hair"}, "details": {"stylist": "ines"}},
            "pending_owner", "Inés", id="balayage-short-1-30-saturday-ends-at-close",
        ),
        pytest.param(
            {"item": "all over color", "start": f"{SUN}T12:00"}, "unavailable", "isn't one of the available start times",
            id="color-sunday-noon",
        ),
    ],
)
async def test_book_respects_close_and_color_days(
    plain_client: Any, deps: Any, gold_dollars: set[str], args: dict[str, Any], status: str, says: str
) -> None:
    """'Every appointment has to finish by closing time' (balayage 3.5 h short / 4 h long; Saturday closes at
    5 pm) and 'Color services are booked Tuesday through Saturday, never on Sundays'."""
    data = await call(plain_client, "book", args)
    assert data["status"] == status, data["speak"]
    assert says in data["speak"], data["speak"]
    for alternative in data.get("alternatives") or ():
        assert WEEKDAYS[date.fromisoformat(alternative["start"][:10]).weekday()] not in ("sun", "mon"), alternative
    await grounded(deps, data, gold_dollars)


# ---------------------------------------------------------------------------- get_catalog


CATALOG: list[Any] = [
    pytest.param(
        {"category": "treatments"}, {"services/deep-conditioning", "services/scalp-treatment", "services/smoothing"},
        ("$30", "$40", "$260"), (), id="what-treatments",
    ),
    pytest.param(
        {"query": "highlights"}, {"services/partial-highlights", "services/full-highlights", "services/balayage"},
        ("$150", "$195", "from $245"), (), id="what-highlights",
    ),
    pytest.param(
        {"query": "haircuts"},
        {"services/womens-cut", "services/mens-cut", "services/mens-cut-beard", "services/curly-cut", "services/kids-cut"},
        ("$78", "$42", "$30"), (), id="what-haircuts",
    ),
    pytest.param({"query": "nails"}, set(), (), (), id="nails-not-offered"),
    pytest.param(
        {"query": "cheap haircut"},
        {"services/womens-cut", "services/mens-cut", "services/mens-cut-beard", "services/curly-cut", "services/kids-cut"},
        (), (), id="cheap-haircut",
    ),
    pytest.param(
        {"category": "color"}, set(COLOR_ITEMS), ("$95", "$120"), ("8 color.",), id="what-color-services-grammar",
    ),
    pytest.param(
        {"category": "styling"}, {"services/blowout", "services/blowout-hot-tool", "services/updo"}, ("$55",),
        ("3 styling.",), id="what-styling-grammar",
    ),
]


@pytest.mark.parametrize(("args", "refs", "says", "never"), CATALOG)
async def test_get_catalog_phrasing(
    client: Any, deps: Any, gold_dollars: set[str], args: dict[str, Any], refs: set[str], says: tuple[str, ...], never: tuple[str, ...]
) -> None:
    data = await call(client, "get_catalog", {**args, "limit": 25})
    got = {i["item_ref"] for i in data["items"]}
    assert got == refs, (got, data["speak"])
    if not refs:
        assert data["handoff"] is not None and data["handoff"]["reason"] == "not_offered", data
    assert all(s in data["speak"] for s in says), data["speak"]
    assert not any(s in data["speak"] for s in never), data["speak"]
    await grounded(deps, data, gold_dollars)


# ---------------------------------------------------------------------------- find_business (multi-tenant entry)


FIND_BUSINESS: list[Any] = [
    pytest.param({"need": "I need a haircut in Austin"}, True, id="haircut-in-austin"),
    pytest.param({"need": "balayage near me"}, True, id="balayage-near-me"),
    pytest.param({"need": "barber for a beard trim"}, True, id="barber-beard-trim"),
    pytest.param({"need": "blow dry bar"}, True, id="blow-dry-bar"),
    pytest.param({"need": "hair salon", "locality": "78702"}, True, id="salon-in-78702"),
    pytest.param({"need": "hair salon", "locality": "East Austin"}, True, id="salon-in-east-austin"),
    pytest.param({"need": "hair salon", "locality": "Portland"}, False, id="salon-in-portland-none"),
    pytest.param({"need": "a mani pedi"}, False, id="mani-pedi-none"),
    pytest.param({"need": "somewhere to get my nails done"}, False, id="nails-done-none"),
    pytest.param(
        {"need": "nail salon"}, False, id="nail-salon",
    ),
]


@pytest.mark.parametrize(("args", "salon_found"), FIND_BUSINESS)
async def test_find_business_phrasing(client: Any, deps: Any, gold_dollars: set[str], args: dict[str, Any], salon_found: bool) -> None:
    data = await call(client, "find_business", args)
    ids = [b["business_id"] for b in data["businesses"]]
    if salon_found:
        assert ids and ids[0] == SALON, (ids, data["speak"])
        assert data["speak"].startswith("Quillfern Hair Studio")
    else:
        assert SALON not in ids, (ids, data["speak"])
        assert "Quillfern" not in data["speak"], data["speak"]
    await grounded(deps, data, gold_dollars)
