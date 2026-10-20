"""Read a page's own structure: price tables, menu cards, order forms, question-and-answer blocks, staff.

The per-page model call returns flat facts; it cannot be trusted to group a size table into one product
with a "Size" question, to copy an allergen list exactly, or to list a form's fields. Small-business
pages already encode all of that in HTML: a table with a price column, a `Contains:` line under a menu
card, a `<form>` with labelled fields, a `<dt>`/`<dd>` or a question heading followed by its answer.
This module reads those structures deterministically and returns records in the same shape the model's
records have, each with a `quote` that is the element's own text (so it always locates on the page).

Nothing here knows any particular site: the rules are about HTML and about how prices, durations,
weekdays and allergen lists are written.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from typing import Any

JsonObj = dict[str, Any]

VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
SKIP = frozenset("script style noscript template svg head iframe object canvas button nav".split())
BLOCK = frozenset(
    "address article aside blockquote dd details div dl dt fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 "
    "header li main ol p pre section summary table tbody td tfoot th thead tr ul caption label legend option select "
    "textarea br".split()
)
AUTO_CLOSE = {
    "p": frozenset("address article aside blockquote div dl fieldset footer form h1 h2 h3 h4 h5 h6 header hr main ol p pre section table ul".split()),
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "tr": frozenset({"tr"}),
    "td": frozenset({"td", "th", "tr"}),
    "th": frozenset({"td", "th", "tr"}),
    "option": frozenset({"option"}),
}
HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")


# --------------------------------------------------------------------------- a small DOM


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Any] = field(default_factory=list)  # Node | str
    parent: Node | None = None

    def elements(self) -> list[Node]:
        return [c for c in self.children if isinstance(c, Node)]

    def iter(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.iter()

    def find_all(self, *tags: str) -> list[Node]:
        return [n for n in self.iter() if n.tag in tags]

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def cls(self) -> str:
        return self.attrs.get("class", "")

    def text(self) -> str:
        return clean(_text(self))

    def own_text(self) -> str:
        """Text of this node's direct text children only (a name cell without its nested notes)."""
        return clean(" ".join(c for c in self.children if isinstance(c, str)))


def clean(text: str) -> str:
    return " ".join(unescape(text).split())


def _hidden(attrs: dict[str, str]) -> bool:
    style = attrs.get("style", "").replace(" ", "").lower()
    return ("hidden" in attrs or attrs.get("aria-hidden") == "true" or "display:none" in style
            or "visibility:hidden" in style)


def _text(node: Node) -> str:
    parts: list[str] = []
    for c in node.children:
        if isinstance(c, str):
            parts.append(c)
        else:
            inner = _text(c)
            if c.tag in BLOCK:
                parts.append(f" {inner} ")
            elif parts and inner and re.search(r"[A-Za-z0-9.,;:!?)]$", parts[-1]) and re.match(r"\s*[A-Z0-9$₹(]", inner) \
                    and not re.search(r"[$₹(]$", parts[-1]):
                parts.append(f" {inner}")  # "Women's cut & style" + "1 hr" are two pieces of text, not one word
            else:
                parts.append(inner)
    return "".join(parts)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        if self.skip_depth:
            if tag not in VOID:
                self.skip_depth += 1
            return
        if tag in SKIP or _hidden(attrs):
            if tag not in VOID:
                self.skip_depth = 1
            return
        closers = AUTO_CLOSE.get(self.cur.tag)
        while closers and tag in closers and self.cur.parent is not None:
            self.cur = self.cur.parent
            closers = AUTO_CLOSE.get(self.cur.tag)
        node = Node(tag, attrs, parent=self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
            return
        node = self.cur
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.cur = node.parent

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        if self.skip_depth or tag in SKIP or _hidden(attrs):
            return  # a self-closed element opens nothing
        self.cur.children.append(Node(tag, attrs, parent=self.cur))

    def handle_data(self, data: str) -> None:
        if not self.skip_depth and data:
            self.cur.children.append(data)


def parse_html(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    body = next((n for n in b.root.iter() if n.tag == "main"), None) or next((n for n in b.root.iter() if n.tag == "body"), b.root)
    return body


# --------------------------------------------------------------------------- values in text

MONEY = r"(?:\$|₹|£|€|A\$|AU\$|Rs\.?\s?|INR\s?)"
_AMOUNT = rf"{MONEY}\s?(\d[\d,]*)(?:\.(\d{{2}}))?"


def cents(m: re.Match[str], g1: int = 1, g2: int = 2) -> int:
    return int(m.group(g1).replace(",", "")) * 100 + int(m.group(g2) or 0)


def has_money(text: str) -> bool:
    return bool(re.search(_AMOUNT, text))


PER_WORDS = {"hr": "hour", "hour": "hour", "item": "item", "visit": "visit", "person": "person", "head": "person",
             "dozen": "dozen", "slice": "slice", "valve": "valve", "tier": "tier", "cake": "cake", "night": "night",
             "session": "session", "class": "class", "pound": "pound", "lb": "pound", "kg": "kg", "job": "job"}


def parse_price(text: str) -> JsonObj | None:
    """'$78', 'from $245', '$150–$275', '$90 per hour', '+$15.00', 'Free', 'Quoted from photos' -> price."""
    t = clean(text)
    low = t.lower()
    amounts = list(re.finditer(_AMOUNT, t))
    per = None
    if m := re.search(r"(?:\bper|/|\ban?|\beach)\s+(hr|hour|item|visit|person|head|dozen|slice|valve|tier|cake|night|session|class|pound|lb|kg|job)\b", low):
        per = PER_WORDS[m.group(1)]
    if not amounts:
        if re.fullmatch(r"(free|no charge|complimentary)\.?", low):
            return {"kind": "free"}
        if re.search(r"\b(quoted?|quote|estimate|ask|call|varies|on request)\b", low) and not re.search(r"\d", low):
            return {"kind": "quote_only"}
        if re.search(r"\bquoted?\b", low):
            return {"kind": "quote_only"}
        return None
    if len(amounts) >= 2 and re.search(r"\d\s+or\s+" + MONEY, t):
        return None  # "$0 or $35": two prices for different cases, not one price
    first = cents(amounts[0])
    price: JsonObj
    if re.match(r"\s*\+", t) or re.search(rf"\badd\s+{MONEY}", t, re.I) or re.search(r"\badd\b\s*$", low):
        price = {"kind": "fixed", "amount_cents": first, "delta": True}
    elif len(amounts) >= 2 and re.search(r"\d\s*(?:–|-|—|to)\s*" + MONEY, t[amounts[0].start():amounts[1].end()]):
        price = {"kind": "range", "amount_cents": first, "max_cents": cents(amounts[1])}
    elif re.search(r"\b(from|starting at|starts at)\b", low[: amounts[0].start() + 1]):
        price = {"kind": "from", "amount_cents": first}
    elif re.search(r"\bquoted?\b|\bestimate\b", low) and not re.search(r"^\s*" + MONEY, t):
        price = {"kind": "quote_only"}
    else:
        price = {"kind": "fixed", "amount_cents": first}
    if per and price["kind"] != "quote_only":
        price["per"] = per
    return price


def price_alternatives(text: str) -> list[tuple[str, JsonObj]]:
    """'$125 for a journeyman plumber, $145 for a master plumber' -> [('journeyman plumber', $125), ...];
    '$175 with a cleaning, $295 on its own' -> [('with a cleaning', $175), ('on its own', $295)]."""
    out = []
    for m in re.finditer(_AMOUNT + r"\s+((?:for|with|on|if|when)\b[^,.;$]{2,60})", text):
        label = re.sub(r"^(for)\s+(an?\s+)?", "", m.group(3).strip(), flags=re.I)
        out.append((label, {"kind": "fixed", "amount_cents": cents(m)}))
    return out if len(out) >= 2 else []


def parse_duration(text: str) -> tuple[int | None, bool]:
    """'1 hr 15 min' -> 75; '2 hrs minimum' -> (120, True); '3 hrs 30 min to 4 hrs' -> 210 (the shortest)."""
    t = clean(text).lower()
    first = re.split(r"\s(?:to|-|–|—)\s|\s?[–—]\s?", t)[0]
    total, found = 0, False
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h)\b", first):
        total += int(float(m.group(1)) * 60)
        found = True
    for m in re.finditer(r"(\d+)\s*(minutes?|mins?|m)\b", first):
        total += int(m.group(1))
        found = True
    return (total if found and total > 0 else None), bool(re.search(r"\b(minimum|at least|or more)\b", t))


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_RX = {
    "mon": r"mon(day)?s?", "tue": r"tue(s|sday)?s?", "wed": r"wed(nesday)?s?", "thu": r"thu(r|rs|rsday)?s?",
    "fri": r"fri(day)?s?", "sat": r"sat(urday)?s?", "sun": r"sun(day)?s?",
}  # fmt: skip


def parse_days(text: str) -> list[str]:
    """'Tuesday, Wednesday, Friday, Saturday' / 'Fridays and Saturdays only' / 'Tue–Sat' -> weekday tokens."""
    found: list[tuple[int, str]] = []
    for day, rx in _DAY_RX.items():
        for m in re.finditer(rf"(?<![a-z]){rx}(?![a-z])", text, re.I):
            found.append((m.start(), day))
    found.sort()
    days = {d for _, d in found}
    for (a_pos, a), (b_pos, b) in zip(found, found[1:]):
        if re.search(r"(\bto\b|through|thru|[-–—])", text[a_pos + 3 : b_pos], re.I):
            i, j = WEEKDAYS.index(a), WEEKDAYS.index(b)
            if i < j:
                days |= set(WEEKDAYS[i : j + 1])
    return [d for d in WEEKDAYS if d in days]


ALLERGEN_WORDS = {
    "wheat": "wheat", "gluten": "wheat", "milk": "milk", "dairy": "milk", "butter": "milk", "egg": "eggs", "eggs": "eggs",
    "soy": "soybeans", "soya": "soybeans", "soybean": "soybeans", "soybeans": "soybeans", "sesame": "sesame",
    "peanut": "peanuts", "peanuts": "peanuts", "tree nut": "tree_nuts", "tree nuts": "tree_nuts", "nuts": "tree_nuts",
    "almond": "tree_nuts", "almonds": "tree_nuts", "hazelnut": "tree_nuts", "hazelnuts": "tree_nuts",
    "walnut": "tree_nuts", "walnuts": "tree_nuts", "pecan": "tree_nuts", "pecans": "tree_nuts", "cashew": "tree_nuts",
    "cashews": "tree_nuts", "pistachio": "tree_nuts", "pistachios": "tree_nuts", "fish": "fish",
    "shellfish": "shellfish", "shrimp": "shellfish", "crab": "shellfish", "lobster": "shellfish",
}  # fmt: skip


def parse_allergens(text: str) -> list[str] | None:
    """'wheat, milk, eggs, tree nuts (almonds)' -> ['eggs', 'milk', 'tree_nuts', 'wheat']; 'none of the major
    allergens' -> []; text that is not an allergen list -> None."""
    t = clean(text).lower()
    if re.search(r"\b(none|no major allergens|free of the major)\b", t) and not re.search(r"\bnone\b.*\bexcept\b", t):
        return []
    t = re.sub(r"\([^)]*\)", " ", t)
    parts = [p.strip(" .") for p in re.split(r",|\band\b|;|/", t) if p.strip(" .")]
    out = set()
    for p in parts:
        if p in ALLERGEN_WORDS:
            out.add(ALLERGEN_WORDS[p])
        elif re.sub(r"s$", "", p) in ALLERGEN_WORDS:
            out.add(ALLERGEN_WORDS[re.sub(r"s$", "", p)])
        else:
            return None  # not a plain list of allergens
    return sorted(out)


def contains_clause(text: str) -> list[str] | None:
    """The allergens of a 'Contains: wheat, milk.' clause in a text, if it has one."""
    m = re.search(r"\bcontains:?\s*([^.;]*)", text, re.I)
    return parse_allergens(m.group(1)) if m else None


def variant_allergens(text: str) -> list[JsonObj]:
    """'Vanilla bean with vanilla buttercream, or dark chocolate with chocolate buttercream. ... Vanilla bean
    contains: wheat, milk, eggs. Dark chocolate contains: wheat, milk, eggs, soy.' -> one choice per variant."""
    found = [(m.group(1).strip(), parse_allergens(m.group(2))) for m in re.finditer(r"(?:^|(?<=[.!?]\s))([A-Z][\w -]{2,40}?) contains:?\s*([^.;]+)", text)]
    found = [(n, a) for n, a in found if a is not None]
    if len(found) < 2:
        return []
    first = re.split(r"(?<=[.!?])\s", text)[0].rstrip(".")
    alternatives = [a.strip() for a in re.split(r",?\s+or\s+|,\s+", first) if a.strip()]
    out = []
    for name, allergens in found:
        full = next((a for a in alternatives if a.lower().startswith(name.lower())), name)
        out.append({"name": full[:1].upper() + full[1:], "allergens": allergens})
    return out


DIET_WORDS = {"vegan": "vegan", "vegetarian": "vegetarian", "gluten-free": "gluten_free", "gluten free": "gluten_free",
              "dairy-free": "dairy_free", "dairy free": "dairy_free", "nut-free": "nut_free", "egg-free": "egg_free",
              "halal": "halal", "kosher": "kosher", "sugar-free": "sugar_free"}


def diet_labels(text: str) -> list[str]:
    low = text.lower()
    return sorted({tag for word, tag in DIET_WORDS.items() if re.search(rf"(?<![a-z-]){re.escape(word)}(?![a-z])", low)})


# --------------------------------------------------------------------------- page structure


GENERIC_HEADING = re.compile(
    r"^(our\s+)?(sizes?|prices?|pricing|options?|extras?|add-?ons?|flavou?rs?|fillings?|frostings?|menu|rates?|fees?|"
    r"sizes and prices|prices and sizes|rates and fees|what it costs|costs?|details|at a glance|how to order|more)\b",
    re.I,
)
ITEM_LABELS = frozenset({"service", "services", "item", "items", "product", "products", "treatment", "treatments", "name",
                         "menu", "dish", "class", "classes", "package", "packages", "what", "description"})


def _section_heading(node: Node, below_level: int = 7) -> Node | None:
    """The nearest heading that introduces `node`: a preceding heading inside an enclosing container."""
    child = node
    for anc in node.ancestors():
        idx = anc.children.index(child)
        for sib in reversed(anc.children[:idx]):
            if isinstance(sib, Node):
                heads = [h for h in sib.iter() if h.tag in HEADINGS and int(h.tag[1]) < below_level]
                if sib.tag in HEADINGS and int(sib.tag[1]) < below_level:
                    return sib
                if heads and sib.tag not in ("table", "ul", "ol", "dl", "form") and not any(h.tag == "h1" for h in heads) and sib.tag in ("div", "header", "hgroup"):
                    return heads[-1]
        child = anc
    return None


def heading_chain(node: Node) -> list[str]:
    """Headings that introduce `node`, nearest first (h3 then h2 then h1)."""
    out: list[str] = []
    level = int(node.tag[1]) if node.tag in HEADINGS else 7
    cur: Node | None = node
    while cur is not None:
        h = _section_heading(cur, level)
        if h is None:
            break
        out.append(h.text())
        level = int(h.tag[1])
        cur = h
    return out


def singular(name: str) -> str:
    words = name.split()
    if words and len(words[-1]) > 3 and words[-1].lower().endswith("s") and not words[-1].lower().endswith("ss"):
        last = words[-1]
        last = last[:-3] + "y" if last.lower().endswith("ies") else last[:-1]
        words[-1] = last
    return " ".join(words)


@dataclass
class Structure:
    url: str
    items: list[JsonObj] = field(default_factory=list)
    groups: list[JsonObj] = field(default_factory=list)  # option groups for the page's own product
    sections: list[JsonObj] = field(default_factory=list)  # question/answer blocks
    forms: list[JsonObj] = field(default_factory=list)
    people: list[JsonObj] = field(default_factory=list)
    schedules: list[JsonObj] = field(default_factory=list)  # {name, days}
    asks: list[JsonObj] = field(default_factory=list)  # {intro, items}: what the business asks customers for
    timetables: list[JsonObj] = field(default_factory=list)
    lede: list[str] = field(default_factory=list)  # {label, lines: [{days, open, close} | {days, closed}]}
    product: str | None = None


def read_page(url: str, html: str, *, title: str = "") -> Structure:
    root = parse_html(html)
    s = Structure(url)
    h1 = next((h for h in root.iter() if h.tag == "h1"), None)
    s.product = singular(h1.text()) if h1 else None
    if h1 is not None and h1.parent is not None:
        # The paragraphs right under the headline (the page's lede).
        for sib in h1.parent.children[h1.parent.children.index(h1) + 1 :]:
            if isinstance(sib, Node) and sib.tag in HEADINGS:
                break
            if isinstance(sib, Node) and sib.tag == "p" and sib.text():
                links = [x for x in sib.iter() if x.tag == "a"]
                if not (links and " ".join(x.text() for x in links) == sib.text()):
                    s.lede.append(sib.text())
    used: set[int] = set()
    item_tables: set[int] = set()
    for table in root.find_all("table"):
        if _table(s, table):
            item_tables.add(id(table))
            used |= {id(n) for n in table.iter()}  # read as a price list, a pick list or a schedule
    _cards(s, root, used)
    _forms(s, root)
    _sections(s, root, item_tables, used)
    return s


# ------------------------------------------------------------------ tables


def _rows(table: Node) -> tuple[list[str], list[list[Node]]]:
    header: list[str] = []
    rows: list[list[Node]] = []
    for tr in table.find_all("tr"):
        cells = [c for c in tr.elements() if c.tag in ("td", "th")]
        if not cells:
            continue
        in_head = any(a.tag == "thead" for a in tr.ancestors())
        if (in_head or all(c.tag == "th" and c.attrs.get("scope") != "row" for c in cells)) and not rows and not header:
            header = [c.text() for c in cells]
            continue
        rows.append(cells)
    return header, rows


def _col(header: list[str], *words: str) -> int | None:
    for i, h in enumerate(header):
        if any(re.search(rf"\b{w}", h, re.I) for w in words):
            return i
    return None


def _name_and_notes(cell: Node) -> tuple[str, list[str], str]:
    """A name cell: its own text is the name; nested elements are notes; one-character marks are footnote marks."""
    name = cell.own_text()
    notes, marks = [], ""
    for el in cell.elements():
        t = el.text()
        if len(t) <= 2 and not t.isalnum():
            marks += t
        elif not name:
            name = t
        elif t:
            notes.append(t)
    if not name:
        name = cell.text()
    marks += "".join(re.findall(r"[*†‡§]", name))
    name = re.sub(r"[*†‡§]+", "", name).strip()
    return name, notes, marks


def _table(s: Structure, table: Node) -> bool:
    """Read one table; True if it was a price list, a pick list or a staff schedule (not prose)."""
    header, rows = _rows(table)
    if not rows:
        return False
    caption = next((c.text() for c in table.iter() if c.tag == "caption"), "")
    chain = heading_chain(table)
    width = max(len(r) for r in rows)
    price_i = _col(header, "price", "cost", "rate", "fee", "amount")
    if price_i is None:
        # No header: the column whose cells are mostly prices.
        for i in range(1, width):
            vals = [r[i].text() for r in rows if len(r) > i]
            if vals and sum(bool(parse_price(v)) for v in vals) >= max(1, len(vals) * 0.6):
                price_i = i
                break
    if _timetable(s, header, rows, table):
        return True
    day_cols = [i for i, h in enumerate(header) if h and parse_days(h) and len(h) <= 10]
    if len(day_cols) >= 5:
        for r in rows:
            name = r[0].text()
            days = [parse_days(header[i])[0] for i in day_cols if i < len(r) and re.search(r"\b(in|on|yes|open|working|available|✓|x)\b", r[i].text(), re.I)]
            s.schedules.append({"name": name, "days": days, "quote": " ".join(c.text() for c in r)})
        return True
    time_i = _col(header, "time", "duration", "length", "how long")
    who_i = _col(header, "with", "stylist", "staff", "who", "therapist", "by")
    serves_i = _col(header, "serves", "servings", "feeds", "people")
    contains_i = _col(header, "contains", "allergen")
    detail_i = _col(header, "detail", "note", "description", "includes")
    first = (header[0] if header else "").strip().lower()
    label = re.sub(r"[^a-z ]", "", first).strip()
    container_heading = chain[0] if chain else ""
    variant_name = None
    if m := re.match(r"^(.*?)\s+(prices?|pricing|rates?|options?)$", caption, re.I):
        variant_name = m.group(1)
    records = []
    for r in rows:
        name, notes, marks = _name_and_notes(r[0])
        if not name:
            continue
        rec: JsonObj = {"name": name, "notes": notes, "marks": marks, "quote": " ".join(c.text() for c in r)}
        if price_i is not None and price_i < len(r):
            ptext = r[price_i].text()
            rec["price_text"] = ptext
            rec["price"] = parse_price(ptext)
            if rec["price"] and re.search(r"\badd(-on)?$", name, re.I):
                name = re.sub(r"\s*\badd(-on)?$", "", name, flags=re.I).strip()
                rec["name"] = name
                rec["price"] = {**rec["price"], "delta": True}
            if re.search(r"\binclud", ptext, re.I):
                rec["included"] = True
        if time_i is not None and time_i < len(r):
            rec["duration_minutes"], rec["duration_is_minimum"] = parse_duration(r[time_i].text())
        if who_i is not None and who_i < len(r):
            rec["staff"] = [x.strip() for x in re.split(r",|\band\b|&|/", r[who_i].text()) if x.strip()]
        if serves_i is not None and serves_i < len(r) and (m := re.search(r"\d+", r[serves_i].text())):
            rec.setdefault("facts", {})["serves"] = int(m.group(0))
        if contains_i is not None and contains_i < len(r):
            allergens = parse_allergens(r[contains_i].text())
            if allergens is not None:
                rec["allergens"] = allergens
        if detail_i is not None and detail_i < len(r):
            rec["notes"] = notes + [r[detail_i].text()]
            alts = price_alternatives(r[detail_i].text())
            if alts:
                rec["alternatives"] = alts
        # Yes/no columns ("Gluten-free version: Available") become per-row flags.
        for i, h in enumerate(header):
            if i in (0, price_i, time_i, who_i, serves_i, contains_i, detail_i) or i >= len(r) or not h:
                continue
            v = r[i].text().lower()
            if re.fullmatch(r"(available|yes|✓|not available|no|—|-)", v):
                rec.setdefault("flags", {})[h] = not v.startswith(("not", "no", "—", "-"))
        records.append(rec)
    if not records:
        return False
    context = {"caption": caption, "headings": chain, "header": header, "url": s.url}
    if price_i is None:
        if not any("allergens" in r or "flags" in r for r in records):
            return False  # a timetable or a fee schedule: its words are read as a section
        # A list the customer picks from (flavors, fillings): an option group of the page's product.
        s.groups.append({"name": caption or container_heading or header[0] if header else "Options", "label": label,
                         "choices": records, "required": None, **context})
        return True
    is_variants = bool(variant_name) or (label and label not in ITEM_LABELS)
    deltas = all((rec.get("price") or {}).get("delta") or rec.get("included") for rec in records)
    if deltas or (not header and GENERIC_HEADING.match(container_heading or "")):
        s.groups.append({"name": container_heading or caption or "Extras", "label": label, "choices": records,
                         "required": False, "addons": True, **context})
        return True
    if is_variants:
        owner = variant_name or next((h for h in chain if not GENERIC_HEADING.match(h)), None)
        if owner is None or (chain and owner == chain[-1] and len(chain) > 1 and GENERIC_HEADING.match(chain[0])):
            owner = s.product or owner
        s.items.append({"name": owner, "options": [{
            "name": header[0] if header else "Options", "required": True, "choices": records}],
            "quote": records[0]["quote"], "category": next((h for h in chain[1:] if not GENERIC_HEADING.match(h)), None),
            "notes": _container_notes(table), "description": _container_desc(table), "source": "variants", **context})
        return True
    category = container_heading if not GENERIC_HEADING.match(container_heading or "x") else None
    note = _container_desc(table)
    for rec in records:
        rec.update({"category": category or (caption if caption and not GENERIC_HEADING.match(caption) else None),
                    "section_note": note, "source": "table", **context})
        s.items.append(rec)
    return True


TIME_SPAN = re.compile(r"(\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.)?)\s*(?:–|-|—|to)\s*(\d{1,2}(?::\d{2})?\s?(?:am|pm|a\.m\.|p\.m\.))", re.I)


def _clock(text: str, hint: str = "") -> str | None:
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s?([ap])?\.?m?\.?\s*", text.lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap is None:
        ap = (re.search(r"([ap])\.?m", hint.lower()) or re.match("", "")).group(1) if re.search(r"([ap])\.?m", hint.lower()) else None
        if ap == "p" and h >= 7 and h != 12:
            ap = "a"  # "8 – 12 pm": a morning start
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def _timetable(s: Structure, header: list[str], rows: list[list[Node]], table: Node) -> bool:
    """An hours table: weekdays down the first column, opening times (or 'Closed') in the others. Each
    time column is one timetable, named by its header ('Office', 'Scheduled work')."""
    dayrows = [r for r in rows if parse_days(r[0].text()) and len(r[0].text()) <= 30]
    if len(dayrows) < 2 or len(dayrows) < len(rows) - 1:
        return False
    width = max(len(r) for r in dayrows)
    found = False
    for col in range(1, width):
        lines = []
        for r in dayrows:
            if col >= len(r):
                continue
            days = parse_days(r[0].text())
            cell = r[col].text()
            m = TIME_SPAN.search(cell)
            if m:
                opens, closes = _clock(m.group(1), m.group(2)), _clock(m.group(2))
                if opens and closes:
                    lines.append({"days": days, "open": opens, "close": closes})
            elif re.search(r"\bclosed\b", cell, re.I) or not re.search(r"\d", cell):
                lines.append({"days": days, "closed": True, "note": cell})
        if any("open" in x for x in lines):
            label = header[col] if col < len(header) else ""
            s.timetables.append({"label": label, "lines": lines, "quote": table.text(), "url": s.url})
            found = True
    return found


def _container(node: Node) -> Node | None:
    for anc in node.ancestors():
        if anc.tag in ("article", "section"):
            return anc
    return None


def _container_desc(node: Node) -> str:
    """The first paragraph of the section/article holding `node` (its intro text)."""
    box = _container(node)
    if box is None:
        return ""
    for p in box.iter():
        if p.tag == "p" and not any(a.tag in ("table", "li", "dd") for a in p.ancestors() if a is not box):
            return p.text()
    return ""


def _container_notes(node: Node) -> list[str]:
    box = _container(node)
    if box is None:
        return []
    out = []
    for ul in box.iter():
        if ul.tag in ("ul", "ol") and not any(a.tag in ("table", "figure", "nav") for a in ul.ancestors()):
            out += [li.text() for li in ul.elements() if li.tag == "li" and li.text()]
    for p in box.iter():
        if p.tag == "p" and "note" in p.cls() and p.text():
            out.append(p.text())
    return out


# ------------------------------------------------------------------ price cards (menus, price lists)


def _cards(s: Structure, root: Node, used: set[int]) -> None:
    for node in root.iter():
        if id(node) in used or node.tag not in ("div", "li", "a", "p", "article", "dt"):
            continue
        kids = [k for k in node.elements() if k.text()]
        if len(kids) < 2 or len(kids) > 5:
            continue
        price_kid = next((k for k in kids if len(k.text()) <= 40 and parse_price(k.text()) and has_money(k.text()) or re.fullmatch(r"free", k.text(), re.I)), None)
        name_kid = next((k for k in kids if k is not price_kid and k.text() and not has_money(k.text()) and len(k.text()) <= 80), None)
        if price_kid is None or name_kid is None or kids.index(name_kid) > kids.index(price_kid):
            continue
        if any(k.tag in ("ul", "ol", "table") for k in kids):
            continue
        card = node.parent if node.tag == "div" and node.parent is not None and node.parent.tag in ("li", "article") else node
        if id(card) in used:
            continue
        used |= {id(n) for n in card.iter()}
        name, name_notes, marks = _name_and_notes(name_kid)
        rest = [k.text() for k in card.elements() if k is not node and k.text()] if card is not node else [
            k.text() for k in kids if k not in (name_kid, price_kid)]
        between = [k.text() for k in kids[kids.index(name_kid) + 1 : kids.index(price_kid)]]
        rec: JsonObj = {"name": name, "price_text": price_kid.text(), "price": parse_price(price_kid.text()),
                        "quote": card.text(), "source": "card", "headings": heading_chain(card), "url": s.url,
                        "notes": name_notes, "marks": marks}
        text_rest = " ".join(rest + between + name_notes)
        dur, minimum = parse_duration(" ".join(between)) if between else (None, False)
        if dur:
            rec["duration_minutes"], rec["duration_is_minimum"] = dur, minimum
        if rest:
            rec["description"] = rest[0] if not re.match(r"^(contains|vegan|gluten)", rest[0], re.I) else ""
        variants = variant_allergens(" ".join([rec.get("description") or ""] + [t for t in rest[1:]]))
        if variants:
            rec["variant_choices"] = variants
        elif (allergens := contains_clause(text_rest)) is not None:
            rec["allergens"] = allergens
        labels = [k.text() for k in card.iter() if k.tag == "span" and re.search(r"diet|tag|label|badge", k.cls())]
        rec["dietary"] = diet_labels(" ".join(labels)) if labels else []
        whens = [k.text() for k in card.iter() if k.tag in ("span", "em", "small") and parse_days(k.text()) and re.search(r"\bonly\b", k.text(), re.I)]
        if whens:
            rec["days"] = parse_days(whens[0])
        if m := re.search(r"serves (?:up to )?(\d+)", text_rest + " " + " ".join(between), re.I):
            rec.setdefault("facts", {})["serves"] = int(m.group(1))
        cat = next((h for h in rec["headings"] if h != name), None)
        rec["category"] = cat
        s.items.append(rec)


# ------------------------------------------------------------------ forms


def _label_for(form: Node, field_node: Node) -> str:
    fid = field_node.attrs.get("id")
    if fid:
        for lab in form.iter():
            if lab.tag == "label" and lab.attrs.get("for") == fid:
                return lab.text()
    for anc in field_node.ancestors():
        if anc.tag == "label":
            return anc.text()
        if anc is form:
            break
    return field_node.attrs.get("aria-label") or field_node.attrs.get("placeholder") or field_node.attrs.get("name", "")


def _forms(s: Structure, root: Node) -> None:
    for form in root.find_all("form"):
        fields: list[JsonObj] = []
        seen_radio: set[str] = set()
        for el in form.iter():
            if el.tag not in ("input", "select", "textarea"):
                continue
            kind = el.attrs.get("type", "text").lower() if el.tag == "input" else el.tag
            name = el.attrs.get("name") or el.attrs.get("id") or ""
            if kind in ("hidden", "submit", "button", "reset", "image", "file", "search", "password") or not name:
                continue
            label = re.sub(r"\(optional\)", "", _label_for(form, el), flags=re.I).strip()
            required = "required" in el.attrs
            if kind == "radio":
                if name in seen_radio:
                    continue
                seen_radio.add(name)
                group = [r for r in form.iter() if r.tag == "input" and r.attrs.get("name") == name]
                fs = next((a for a in el.ancestors() if a.tag == "fieldset"), None)
                legend = next((c.text() for c in fs.iter() if c.tag == "legend"), "") if fs else ""
                options = [{"value": r.attrs.get("value") or _label_for(form, r), "label": _label_for(form, r)} for r in group]
                fields.append({"key": name, "label": legend or label, "type": "choice", "options": options,
                               "required": required or any("checked" in r.attrs for r in group)})
                continue
            if kind == "checkbox":
                fields.append({"key": name, "label": label, "type": "boolean", "required": required})
                continue
            if el.tag == "select":
                options = [o for o in el.iter() if o.tag == "option"]
                opts = [{"value": o.attrs.get("value") or o.text(), "label": o.text()} for o in options
                        if (o.attrs.get("value", None) != "" and o.text())]
                fields.append({"key": name, "label": label, "type": "choice", "options": opts, "required": required})
                continue
            ftype = {"tel": "phone", "email": "email", "date": "date", "time": "time", "number": "integer",
                     "textarea": "long_text"}.get(kind, "text")
            f: JsonObj = {"key": name, "label": label, "type": ftype, "required": required}
            if el.attrs.get("maxlength", "").isdigit():
                f["max_length"] = int(el.attrs["maxlength"])
            fields.append(f)
        if fields:
            s.forms.append({"fields": fields, "quote": " ".join(f["label"] for f in fields if f["label"]),
                            "headings": heading_chain(form), "url": s.url})


# ------------------------------------------------------------------ question/answer sections


ASK_INTRO = re.compile(r"\b(tell us|let us know|we need|we'll need|include|send us|provide|give us|have ready|we ask for|please bring|what we ask)\b", re.I)
POSTAL_LIST = re.compile(r"^\s*\d{4,6}(\s*[–-]\s*\d{4,6})?(\s*,\s*\d{4,6}(\s*[–-]\s*\d{4,6})?)*\s*[A-Z]")
"""'80205Five Points, Whittier': a row of a service-area list (the service area records hold it)."""


def _join_short_lists(units: list[str], paras: list[Node], used: set[int]) -> list[str]:
    """A list of three or more short items ('Shoe covers on.', 'We photograph the problem.', ...) says one
    thing: it becomes one unit. A list of places after a sentence about "these areas" completes that sentence
    (and is read as the places, without their postcodes)."""
    for p in paras:
        if p.tag not in ("ul", "ol"):
            continue
        items = [li.text() for li in p.elements() if li.tag == "li" and li.text() and id(li) not in used]
        items = [i for i in items if i in units]
        if len(items) >= 3 and all(len(i) <= 90 and not i.endswith("?") for i in items):
            postal = all(POSTAL_LIST.match(i) for i in items)
            joined = "; ".join(i.rstrip(".") for i in items) + "."
            pos = units.index(items[0])
            units = [u for u in units if u not in items]
            if postal and pos > 0 and re.search(r"\b(these|the following|below)\b|:$", units[pos - 1], re.I):
                places = "; ".join(re.sub(r"^[\d\s,–-]+", "", i).strip().rstrip(".") for i in items) + "."
                units[pos - 1] = f"{units[pos - 1].rstrip('.:')}: {places}"
            else:
                units.insert(pos, joined)
    return units


def _dl_pairs(dl: Node) -> list[tuple[str, str]]:
    pairs, term = [], None
    for el in dl.iter():
        if el.tag == "dt":
            term = el.text()
        elif el.tag == "dd" and term:
            pairs.append((term, el.text()))
            term = None
    return pairs


def _previous_text(node: Node) -> str:
    """The text of the element just before `node` among its siblings."""
    if node.parent is None:
        return ""
    prev = ""
    for sib in node.parent.children:
        if sib is node:
            return prev
        if isinstance(sib, Node) and sib.text():
            prev = sib.text()
    return prev


def _dl_item(s: Structure, dl: Node, dd: Node, term: str) -> bool:
    """A <dt>name</dt><dd>$79 When we need ...</dd> row of a price list is an item, not a question."""
    kids = dd.elements()
    lead = kids[0].text() if kids and dd.text().startswith(kids[0].text()) else ""
    if not lead or len(lead) > 40:
        return False  # a price list shows the price on its own; "Free up to 24 hours ..." is a sentence
    m = re.match(rf"^\s*(\+?\s?(?:from\s+)?{MONEY}\s?\d[\d,]*(?:\.\d{{2}})?(?:\s?[–-]\s?{MONEY}?\s?\d[\d,]*(?:\.\d{{2}})?)?(?:\s+(?:per|an?|each)\s+\w+)?|free\b|{MONEY}\d+ or {MONEY}\d+)\s*$", lead, re.I)
    if not m:
        return False
    ptext = m.group(1)
    dt = next((x for x in dl.iter() if x.tag == "dt" and x.text() == term), None)
    name, notes, marks = _name_and_notes(dt) if dt is not None else (term, [], "")
    rest = dd.text()[len(lead or ptext):].strip()
    rec: JsonObj = {"name": name, "price_text": ptext, "price": parse_price(ptext), "quote": f"{term} {dd.text()}",
                    "notes": notes + ([rest] if rest else []), "marks": marks, "source": "card",
                    "headings": heading_chain(dl), "url": s.url, "description": rest}
    if m2 := re.search(r"serves (?:up to )?(\d+)", " ".join(notes), re.I):
        rec["facts"] = {"serves": int(m2.group(1))}
    rec["category"] = rec["headings"][0] if rec["headings"] else None
    s.items.append(rec)
    return True


def _sections(s: Structure, root: Node, item_tables: set[int], used: set[int]) -> None:
    """Question/answer blocks: <dt>/<dd> pairs, and a heading followed by what it introduces. Each block
    keeps its parts (`units`): a paragraph, a list item or a small table each say one thing."""
    taken: set[int] = set()
    consumed: set[int] = set()
    for dl in root.find_all("dl"):
        intro = _previous_text(dl)
        if intro and intro.rstrip().endswith(":") and dl.parent is not None:
            # "Here's what we ask for:" + a definition list: one statement listing what is asked.
            pairs = []
            term = None
            for el in dl.iter():
                if el.tag == "dt":
                    term = el.text()
                elif el.tag == "dd" and term:
                    pairs.append(f"{term}: {el.text()}")
                    term = None
            if pairs:
                chain = heading_chain(dl)
                text = "; ".join(p.rstrip(".") for p in pairs) + "."
                s.sections.append({"question": chain[0] if chain else intro, "answer": text, "kind": "list", "headings": chain[1:],
                                   "url": s.url, "units": [text], "quote_parts": pairs})
                if ASK_INTRO.search(intro):
                    s.asks.append({"intro": intro, "items": [p.split(":")[0] for p in pairs], "headings": chain, "url": s.url})
                taken |= {id(n) for n in dl.iter()}
                continue
        pairs = _dl_pairs(dl)
        contact = sum(bool(re.search(r"\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}|@\w|\b\d+ [A-Z][a-z]+ (St|Ave|Rd|Street|Avenue|Road)\b", dd)) for _, dd in pairs)
        if len(pairs) >= 3 and contact >= 2 and all(len(dd) <= 90 for _, dd in pairs):
            # A block of phone numbers and addresses: one "how to reach us" statement.
            chain = heading_chain(dl)
            text = "; ".join(f"{t}: {d}".rstrip(".") for t, d in pairs) + "."
            s.sections.append({"question": chain[0] if chain else "Contact", "answer": text, "kind": "list", "headings": chain[1:],
                               "url": s.url, "units": [text], "quote_parts": [f"{t} {d}" for t, d in pairs]})
            taken |= {id(n) for n in dl.iter()}
            continue
        term = None
        for el in dl.iter():
            if el.tag == "dt":
                term = el.text()
            elif el.tag == "dd" and term and _dl_item(s, dl, el, term):
                term = None
            elif el.tag == "dd" and term:
                s.sections.append({"question": term, "answer": el.text(), "kind": "dl", "headings": heading_chain(dl),
                                   "url": s.url, "units": [el.text()], "quote_parts": [term, el.text()]})
                term = None
        taken |= {id(n) for n in dl.iter()}
    for h in root.iter():
        if h.tag not in HEADINGS or h.tag == "h1" or id(h) in taken or id(h) in used:
            continue
        level = int(h.tag[1])
        parent = h.parent
        if parent is None:
            continue
        idx = parent.children.index(h)
        following = list(parent.children[idx + 1 :])
        # A heading wrapped with its intro ("<div class=section-head><h2/><p/></div>"): what follows the
        # wrapper belongs to it too.
        if parent.tag in ("div", "header", "hgroup") and parent.parent is not None and \
                sum(1 for x in parent.iter() if x.tag in HEADINGS) == 1:
            following += list(parent.parent.children[parent.parent.children.index(parent) + 1 :])
        paras: list[Node] = []
        for sib in following:
            if not isinstance(sib, Node):
                continue
            if sib.tag in HEADINGS and int(sib.tag[1]) <= level:
                break
            if any(x.tag in HEADINGS and int(x.tag[1]) <= level for x in sib.iter()):
                break
            if sib.tag == "div" and any(x.tag in HEADINGS for x in sib.iter()):
                break
            if sib.find_all("form", "dl") or not sib.text():
                continue
            tables = sib.find_all("table")
            if tables and any(id(t) in item_tables for t in tables):
                continue
            links = [x for x in sib.iter() if x.tag == "a"]
            if len(links) == 1 and links[0].text() == sib.text():
                continue  # a link ("Map and directions") says nothing by itself
            if sib.tag in ("p", "ul", "ol", "div", "blockquote", "table"):
                paras.append(sib)
        if not paras:
            continue
        for p in paras:
            consumed.update(id(n) for n in p.iter())
        units: list[str] = []
        for p in paras:
            if id(p) in used:
                continue  # already read as a price list or a menu card
            lists = [x for x in p.iter() if x.tag in ("ul", "ol")]
            if p.tag == "ol" and not any(x.tag in HEADINGS for x in p.iter()):
                steps = [li.text() for li in p.elements() if li.tag == "li" and li.text() and id(li) not in used]
                if steps:
                    units.append(" ".join(step if step.endswith((".", "!", "?")) else step + "." for step in steps))
                continue  # numbered steps are one procedure
            if p.tag in ("ul", "ol"):
                for li in p.elements():
                    if li.tag != "li" or not li.text() or id(li) in used or any(x.tag in HEADINGS for x in li.iter()):
                        continue  # a step with its own heading is read under that heading
                    units.append(li.text())
            elif p.tag == "table" or p.find_all("table"):
                # A small prose table (when you cancel -> what happens): one statement per row, "row: cells".
                for t in [p] if p.tag == "table" else p.find_all("table"):
                    for tr in t.find_all("tr"):
                        if any(a.tag == "thead" for a in tr.ancestors()):
                            continue
                        cells = [c.text() for c in tr.elements() if c.tag in ("td", "th") and c.text()]
                        if len(cells) >= 2:
                            units.append(f"{cells[0]}: {' '.join(cells[1:])}")
                        elif cells:
                            units.append(cells[0])
            elif lists and p.tag == "div":
                units += [x.text() for x in p.elements() if x.text() and x.tag not in ("ul", "ol") and id(x) not in used]
                units += [li.text() for ul in lists for li in ul.elements() if li.tag == "li" and li.text() and id(li) not in used]
            else:
                units.append(p.text())
        # "Tell us: your pickup date, how many servings, ...": a list after a colon is what the business asks
        # for, not a set of statements.
        for i, p in enumerate(paras):
            if p.tag in ("ul", "ol") and i > 0 and paras[i - 1].text().rstrip().endswith(":") and ASK_INTRO.search(paras[i - 1].text()):
                asked = [li.text() for li in p.elements() if li.tag == "li" and li.text()]
                s.asks.append({"intro": paras[i - 1].text(), "items": asked, "headings": [h.text(), *heading_chain(h)], "url": s.url})
                units = [u for u in units if u not in asked]
        for p in paras:
            for ul in [x for x in p.iter() if x.tag in ("ul", "ol")]:
                prev = None
                for sib in ul.parent.children if ul.parent else []:
                    if sib is ul:
                        break
                    if isinstance(sib, Node) and sib.text():
                        prev = sib
                if prev is not None and prev.text().rstrip().endswith(":") and ASK_INTRO.search(prev.text()) and not any(a["items"] and a["url"] == s.url and a["intro"] == prev.text() for a in s.asks):
                    asked = [li.text() for li in ul.elements() if li.tag == "li" and li.text()]
                    s.asks.append({"intro": prev.text(), "items": asked, "headings": [h.text(), *heading_chain(h)], "url": s.url})
                    units = [u for u in units if u not in asked]
        units = [u for u in units if u]
        units = _join_short_lists(units, paras, used)
        s.sections.append({"question": h.text(), "answer": " ".join(units), "kind": "faq" if h.text().endswith("?") else "section",
                           "headings": heading_chain(h), "url": s.url, "units": units, "quote_parts": [h.text(), *units]})
    # Paragraphs no heading introduces (footnotes, notes under a table): statements too, named by their
    # container's label or the heading before them.
    lede = set(s.lede)
    for p in root.iter():
        if p.tag != "p" or id(p) in consumed or id(p) in used or id(p) in taken or p.text() in lede or len(p.text()) < 30:
            continue
        if any(a.tag in ("form", "dl", "table", "li", "figure", "blockquote", "header", "nav") for a in p.ancestors()):
            continue
        if any(x.tag in HEADINGS for x in p.iter()):
            continue
        box = next((a for a in p.ancestors() if a.tag in ("section", "article", "aside", "div") and a.attrs.get("aria-label")), None)
        chain = heading_chain(p)
        heading = (box.attrs.get("aria-label") if box is not None else None) or (chain[0] if chain else s.product or "")
        text = re.sub(r"^[*†‡§]\s*", "", p.text())
        s.sections.append({"question": heading, "answer": text, "kind": "note", "headings": chain, "url": s.url,
                           "units": [text], "quote_parts": [text]})
        consumed.update(id(n) for n in p.iter())
