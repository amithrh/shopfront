"""Catalog assembly: one item per product, with its variants as option groups, from every page that shows it.

Sightings come from two places. The page's own structure (`structure.py`: price-table rows, menu cards,
pick lists) is preferred: it is exact and grouped. The model's per-page items fill in pages whose
structure says nothing. Sightings of the same product on several pages are merged (the home page's
"Balayage from $245" and the services table's two balayage lengths are one item); variants that a page
lists as separate rows ("Balayage, shoulder length", "Balayage, past the shoulder blades") become
choices of one item; category teasers ("Pastries from $3.75") and rows that repeat a choice ("6-inch
round $56") are dropped.

Every price, duration and choice is re-checked against its own quote on the page.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from askonce_onboarding.structure import diet_labels, parse_allergens, parse_price, singular

JsonObj = dict[str, Any]

STOP = frozenset("a an the and or of with for to in on at by your our".split())


def words(text: Any) -> set[str]:
    t = str(text or "").lower().replace("&", " and ")
    t = re.sub(r"(\w)'s\b", r"\1s", t).replace("'", "")
    out = set()
    for w in re.findall(r"[a-z0-9]+", t):
        if w in STOP:
            continue
        if len(w) > 5 and w.endswith("ing"):
            w = w[:-3]
        elif len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
            w = w[:-1]
        out.add(w)
    return out


def base_name(name: str) -> str:
    """'Kids cut, ages 12 and under' -> 'Kids cut'; 'Small item: chair, nightstand' -> 'Small item'."""
    return re.split(r"\s*[,:(]\s*", name, maxsplit=1)[0].strip()


def tail_name(name: str) -> str:
    parts = re.split(r"\s*[,:]\s*", name, maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else name


@dataclass
class Sighting:
    page_url: str
    rec: JsonObj
    source: str  # variants | table | card | model | product
    ev: JsonObj  # {"url", "quote"}
    extra_ev: list[JsonObj] = field(default_factory=list)


@dataclass
class Product:
    name: str
    sightings: list[Sighting] = field(default_factory=list)
    groups: list[JsonObj] = field(default_factory=list)  # {"name", "required", "choices": [choice rec], "addons", "ev"}

    @property
    def primary(self) -> Sighting:
        order = {"variants": 0, "product": 0, "table": 1, "card": 2, "model": 3}
        return min(self.sightings, key=lambda s: order.get(s.source, 9))


def price_ok(price: JsonObj | None, quote: str, quote_has_amount) -> bool:
    if not price or price.get("kind") in ("free", "quote_only"):
        return True
    ok = quote_has_amount(quote, price["amount_cents"])
    if price.get("max_cents") is not None:
        ok = ok and quote_has_amount(quote, price["max_cents"])
    return ok


def compatible(a: JsonObj | None, b: JsonObj | None) -> bool:
    """Two readings of one price that can be the same item ('from $245' and a $245 choice)."""
    if not a or not b:
        return True
    if a.get("kind") in ("free", "quote_only") or b.get("kind") in ("free", "quote_only"):
        return a.get("kind") == b.get("kind")
    lo_a, lo_b = a.get("amount_cents"), b.get("amount_cents")
    if a.get("kind") == "from" or b.get("kind") == "from":
        return lo_a == lo_b
    return lo_a == lo_b and a.get("max_cents") == b.get("max_cents")


def same_product(a: str, b: str) -> bool:
    if a.lower() == b.lower():
        return True
    wa, wb = words(a), words(b)
    if wa == wb:
        return True
    ba, bb = words(base_name(a)), words(base_name(b))
    return bool(ba) and ba == bb


def contains_product(small: str, big: str) -> bool:
    ws, wb = words(small), words(big)
    return bool(ws) and len(ws) >= 1 and ws <= wb


def group_label(choices: list[JsonObj], header: str, product: str) -> str:
    """A name for an option group: the table's own column label, unless that only repeats the product."""
    text = " ".join(c.get("name", "") for c in choices).lower()
    head = (header or "").strip()
    if head and words(head) - words(product) and head.lower() not in ("job", "item", "type", "options"):
        return head[:1].upper() + head[1:]
    if re.search(r"\b(length|shoulder|long|short)\b", text):
        return "Length"
    if re.search(r"\b(inch|in\b|small|medium|large|sheet|tier|size|\d+\s?(ft|in|cm))", text):
        return "Size"
    return head[:1].upper() + head[1:] if head else "Type"


def choice_id(name: str, used: set[str], slug) -> str:
    cid = slug(name)
    n = 2
    while cid in used:
        cid = f"{slug(name)}-{n}"
        n += 1
    used.add(cid)
    return cid
