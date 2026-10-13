"""Price computation from catalog facts. Never estimates: only arithmetic over stated prices.

Rules:
  * a choice with `price` replaces the item price (absolute price for that configuration);
  * a choice with `price_delta` adds to the running price (a PriceLine per delta);
  * `per: person` multiplies by party_size (when given), `per: each` by quantity 1;
  * any `quote_only` component makes the whole quote `quote_only` with no amount;
  * `from` stays `from` unless a fixed absolute choice price applies;
  * every PriceLine carries the fact ref it came from (`menu/celebration-cake/size/10in#price`).

Other `per` units on a *delta* (`tier`, `layer`, ...) multiply by the matching numeric attribute of
the configuration: the first selected choice (option-group order), else the item, with an attribute
named after the unit (`tiers` or `tier`). The gold bakery's "+$12 per tier" gluten-free sponge on a
two-tier cake is therefore +$24, cited to `menu/custom-cake/size/two-tier#attributes.tiers`. A delta
whose multiplier cannot be found cannot be totalled honestly, so the quote becomes `quote_only`.

`per` on the base price (`hour`, `dozen`, `visit`, ...) is kept on the quote (`$90 per hour`) except
`person`, which is multiplied out when the party size is known.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from askonce_server.domain.models import PriceLine, PriceQuote
from askonce_server.profile.model import JsonObj, Profile

PER_PERSON = "person"
PER_EACH = "each"


@dataclass(frozen=True)
class SelectedChoice:
    """One selected option choice with its full entity ref (`<item ref>/<group id>/<choice id>`)."""

    group_id: str
    choice: JsonObj
    ref: str


def selected_choices(item_ref: str, item: JsonObj, choices: Mapping[str, str] | None) -> Iterator[SelectedChoice]:
    """The item's selected choices in option-group order.

    `choices` maps group id -> choice id; several ids for a multi-select group are joined by commas
    (`"buttercream-flowers,fresh-fruit"`). Unknown groups or ids are skipped (planning validates them).
    """
    if not choices:
        return
    for group in item.get("options", ()) or ():
        wanted = choices.get(group["id"])
        if not wanted:
            continue
        ids = [part.strip() for part in wanted.split(",") if part.strip()]
        for choice in group.get("choices", ()):
            if choice["id"] in ids:
                yield SelectedChoice(group["id"], choice, f"{item_ref}/{group['id']}/{choice['id']}")


def price_for(
    profile: Profile, item_ref: str, *, choices: Mapping[str, str] | None = None, party_size: int | None = None
) -> PriceQuote | None:
    """The statable price of an item as configured, or None if the item has no price fact."""
    catalog_id, item = profile.item(item_ref)
    ref = profile.item_ref(catalog_id, item)
    selected = list(selected_choices(ref, item, choices))
    currency = profile.currency

    base: JsonObj | None = item.get("price")
    base_ref, base_label = f"{ref}#price", str(item.get("name") or item.get("spoken_name") or ref)
    for sel in selected:
        if sel.choice.get("price") is not None:
            base = sel.choice["price"]
            base_ref = f"{sel.ref}#price"
            base_label = str(sel.choice.get("name") or sel.choice.get("spoken_name") or sel.choice["id"])
    if base is None:
        return None

    refs: list[str] = [base_ref]
    deltas = [sel for sel in selected if sel.choice.get("price_delta") is not None]
    refs.extend(f"{sel.ref}#price_delta" for sel in deltas)
    if base["kind"] == "quote_only" or any(sel.choice["price_delta"]["kind"] == "quote_only" for sel in deltas):
        return PriceQuote(kind="quote_only", currency=base.get("currency", currency), fact_refs=tuple(refs))

    kind: str = base["kind"]
    amount = int(base.get("amount_cents") or 0)
    maximum = int(base["max_cents"]) if base.get("max_cents") is not None else None
    per: str | None = base.get("per")
    if per == PER_PERSON and party_size is not None:
        amount *= party_size
        maximum = maximum * party_size if maximum is not None else None
        base_label = f"{base_label} x {party_size}"
        per = None
    lines = [PriceLine(label=base_label, amount_cents=amount, fact_ref=base_ref)]

    for sel in deltas:
        delta = sel.choice["price_delta"]
        multiplier = _multiplier(delta.get("per"), item, ref, selected, party_size)
        if multiplier is None:
            return PriceQuote(kind="quote_only", currency=base.get("currency", currency), fact_refs=tuple(refs))
        times, multiplier_ref = multiplier
        if multiplier_ref is not None:
            refs.append(multiplier_ref)
        step = int(delta.get("amount_cents") or 0) * times
        step_max = int(delta["max_cents"]) * times if delta.get("max_cents") is not None else step
        kind, amount, maximum = _combine(kind, amount, maximum, delta["kind"], step, step_max)
        label = str(sel.choice.get("name") or sel.choice.get("spoken_name") or sel.choice["id"])
        lines.append(
            PriceLine(label=f"{label} x {times}" if times != 1 else label, amount_cents=step, fact_ref=f"{sel.ref}#price_delta")
        )

    return PriceQuote(
        kind=kind,  # type: ignore[arg-type]
        currency=base.get("currency", currency),
        amount_cents=None if kind == "free" else amount,
        max_cents=maximum if kind == "range" else None,
        per=per,
        lines=tuple(lines),
        fact_refs=tuple(dict.fromkeys(refs)),
    )


def _combine(
    kind: str, amount: int, maximum: int | None, delta_kind: str, step: int, step_max: int
) -> tuple[str, int, int | None]:
    """Add one delta to the running price, widening the kind (free < fixed < from/range)."""
    if delta_kind == "free":
        return kind, amount, maximum
    low = amount + step
    high = (maximum if maximum is not None else amount) + step_max
    if kind == "free":
        kind = "fixed"
    if kind == "from" or delta_kind == "from":
        return "from", low, None
    if kind == "range" or delta_kind == "range":
        return "range", low, high
    return "fixed", low, None


def _multiplier(
    per: str | None, item: JsonObj, item_ref: str, selected: list[SelectedChoice], party_size: int | None
) -> tuple[int, str | None] | None:
    """How many times a delta applies, with the fact ref of the count (None when implicit)."""
    if per is None or per == PER_EACH:
        return 1, None
    if per == PER_PERSON:
        return (party_size, None) if party_size is not None else None
    names = (f"{per}s", per)
    for sel in selected:
        found = _count_attribute(sel.choice, names)
        if found is not None:
            return found[0], f"{sel.ref}#attributes.{found[1]}"
    found = _count_attribute(item, names)
    if found is not None:
        return found[0], f"{item_ref}#attributes.{found[1]}"
    return None


def _count_attribute(entity: JsonObj, names: tuple[str, ...]) -> tuple[int, str] | None:
    attributes = entity.get("attributes") or {}
    for name in names:
        value = attributes.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool) and value >= 1 and float(value).is_integer():
            return int(value), name
    return None
