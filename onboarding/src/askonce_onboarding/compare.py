"""Onboarded vs gold: a deterministic diff of what a customer could be told or asked.

`askonce-compare --demo` takes each onboarded demo profile (`out/<id>/profile.json`), applies the scripted
owner answers in memory if its questions are still open (the state a customer meets), and compares it
with the hand-curated gold profile for the same website (`profiles/*.json`). It reports, per business,
every check that was made and every mismatch, grouped by dimension:

  business     name, phone, email, address, timezone, currency
  hours        weekly hours and closures of every location (storefront and service areas)
  areas        the postal codes and places in each service zone
  items        every gold item has one onboarded item (no duplicates, no variants split off as items)
               with the same price, duration, option groups and choices (price, duration), allergens,
               dietary tags, the days it can be had, and its stated attributes
  knowledge    every gold policy/FAQ entry is covered: its numbers, yes/no facts and wording are in the
               onboarded entries that answer the same question; no duplicated entries
  flows        each order/booking/quote module: the items it takes, approval rules, intake fields,
               availability (lead time, capacity, booking window, closures), staff and their days,
               visit windows, fulfillment, what is read out before confirming
  owner        approver name and usual reply time

Matching is by content, never by id: names are compared as normalized word sets, numbers in any unit
(48 hours == 2880 minutes == 2 days), yes/no facts by key words and polarity. No model is involved.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JsonObj = dict[str, Any]
REPO_ROOT = Path(__file__).resolve().parents[3]
GOLD_DIR = REPO_ROOT / "profiles"
OUT_DIR = Path(__file__).resolve().parents[2] / "out"
ANSWERS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "owner-answers"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# --------------------------------------------------------------------------- text

STOP = frozenset(
    "a an the and or of with for to in on at by is are be our your we you it its this that from per each "
    "any all".split()
)
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "ninety": 90, "hundred": 100, "single": 1, "half": 50,
}  # fmt: skip


def ascii_lower(text: Any) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()


def stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def tokens(text: Any) -> set[str]:
    t = ascii_lower(text).replace("&", " and ")
    t = re.sub(r"(\w)'s\b", r"\1s", t)
    t = t.replace("'", "")
    return {stem(w) for w in re.findall(r"[a-z0-9]+", t) if w not in STOP}


def dice(a: set[str], b: set[str]) -> float:
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def weighted_dice(a: set[str], b: set[str], idf: dict[str, float]) -> float:
    """Dice with rare words counting more (a shared 'Wheat Ridge' says more than a shared 'we')."""
    w = lambda s: sum(idf.get(x, 1.0) for x in s)  # noqa: E731
    total = w(a) + w(b)
    return 2 * w(a & b) / total if total else 0.0


def price_words(item: JsonObj) -> str:
    p = item.get("price") or {}
    return f"${p['amount_cents'] / 100:.2f}" if p.get("amount_cents") else ""


def recall(want: set[str], have: set[str]) -> float:
    return len(want & have) / len(want) if want else 1.0


DURATION_UNITS = {
    "minute": 1, "min": 1, "mins": 1, "minutes": 1, "hour": 60, "hr": 60, "hrs": 60, "hours": 60,
    "day": 1440, "days": 1440, "week": 10080, "weeks": 10080, "month": 43200, "months": 43200,
    "year": 518400, "years": 518400,
}  # fmt: skip
"""A month is 30 days and a year 12 months, the same on both sides (a '12 month' warranty is '1 year')."""


def text_atoms(text: Any) -> set[tuple[str, float]]:
    """Every number a text states, canonicalized: ('minutes', 2880), ('cents', 5000), ('percent', 50),
    ('count', 48)."""
    t = ascii_lower(text)
    for w, v in NUMBER_WORDS.items():
        t = re.sub(rf"\b{w}\b", str(v), t)
    t = re.sub(r"\b(a|an|per)\s+(?=(business\s+)?(minute|hour|day|week|month|year)\b)", "1 ", t)
    out: set[tuple[str, float]] = set()
    for m in re.finditer(r"\$\s?(\d[\d,]*)(?:\.(\d{2}))?", t):
        out.add(("cents", int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0)))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s?(?:%|percent)", t):
        out.add(("percent", float(m.group(1))))
    for m in re.finditer(r"(\d+(?:\.\d+)?)(?:\s?(?:-|to|–)\s?\d+(?:\.\d+)?)?[\s-]*(?:business\s+)?([a-z]+)", t):
        unit = DURATION_UNITS.get(m.group(2))
        if unit:
            out.add(("minutes", float(m.group(1)) * unit))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s?(?:hr|h)\s?(\d+)\s?min", t):
        out.add(("minutes", float(m.group(1)) * 60 + int(m.group(2))))
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", t):
        out.add(("count", float(m.group(0).replace(",", ""))))
    return out


def key_unit(key: str) -> tuple[str, float]:
    k = key.lower()
    if re.search(r"(^|_)ages?(_|$)", k):
        return ("count", 1)  # an age in years is a count, not a duration
    for suffix, unit in (("_minutes", ("minutes", 1)), ("_hours", ("minutes", 60)), ("_days", ("minutes", 1440)),
                         ("_weeks", ("minutes", 10080)), ("_months", ("minutes", 43200)),
                         ("_cents", ("cents", 1)), ("_percent", ("percent", 1))):
        if k.endswith(suffix):
            return unit
    if k.endswith("_years") and "founded" not in k:
        return ("minutes", 518400)
    return ("count", 1)


def value_atoms(key: str, value: Any) -> set[tuple[str, float]]:
    """A structured fact's numbers, canonicalized like `text_atoms` (raw numbers also count)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return text_atoms(value) if isinstance(value, str) else set()
    unit, factor = key_unit(key)
    out = {(unit, float(value) * factor), ("count", float(value))}
    if unit == "minutes" and key.lower().endswith("_business_days"):
        out.add(("minutes", float(value) * 1440))
    return out


NEGATION = re.compile(r"\b(no|not|never|none|without|cannot|can't|cant|don't|dont|doesn't|doesnt|isn't|isnt|aren't|"
                      r"arent|won't|wont|nor|can not|unable|aren t|isn t|don t|only service animals)\b|n't\b")
GENERIC_KEY_WORDS = frozenset(
    "accept accepts accepted contains contain available option has have is can allowed welcome free required "
    "on request included include offer offers offered needed need ok".split()
)
KEY_SYNONYMS = {"check": {"check", "cheque"}, "walk": {"walk", "walkin"}, "latex": {"latex", "nitrile"}, "omit": {"omit", "leave"},
                "celiac": {"celiac", "coeliac"}, "ppd": {"ppd", "paraphenylenediamine"}}


def _key_hit(word: str, have: set[str]) -> bool:
    options = KEY_SYNONYMS.get(word, {word})
    return any(o in have or (len(o) >= 5 and any(h[:4] == o[:4] for h in have if len(h) >= 5)) for o in options)


def bool_claim(key: str, text: str, subject: set[str] | None = None, questions: Iterable[str] = (), with_hits: bool = False) -> Any:
    """What a text says about a yes/no fact key: True, False, or None if it never mentions it. The sentence
    that names most of the key's words decides; a negation in it means False. A sentence that makes an
    exception for the subject ("every cut includes a shampoo, except bang trims") says the opposite about it.
    An answer that opens with "Yes"/"No" to a question naming the key answers it directly."""
    all_words = list(tokens(key.replace("_", " ")))
    words = [w for w in all_words if w not in GENERIC_KEY_WORDS] or all_words
    low = ascii_lower(text).replace("’", "'")
    opener = re.match(r"\s*(yes|no)\b(?!-)", low)  # "No-show: ..." is not a "No"
    if opener and any(any(_key_hit(w, tokens(q)) for w in words) for q in questions):
        claim = opener.group(1) == "yes"
        return (claim, len(words) + 1) if with_hits else claim
    best, best_hits = None, 0
    for sentence in re.split(r"(?<=[.!?;])\s+|\n", low):
        if sentence.rstrip().endswith("?"):
            continue  # a question states nothing
        have = tokens(sentence)
        if not any(_key_hit(w, have) for w in words):
            continue
        hits = sum(_key_hit(w, have) for w in all_words)
        if hits > best_hits:
            best, best_hits = sentence, hits
    if best is None:
        return (None, 0) if with_hits else None
    # A negation counts only just before what it negates ("no travel fee, and we take after-hours
    # emergencies" says nothing negative about emergencies).
    sw = re.findall(r"[a-z0-9']+", best)
    hit_at = [i for i, w in enumerate(sw) if any(_key_hit(k, tokens(w)) for k in all_words)]
    neg_at = [i for i, w in enumerate(sw) if NEGATION.fullmatch(w) or w.endswith("n't") or w in ("dont", "cant", "wont", "isnt", "arent", "doesnt")]
    negated = any(0 <= h - n <= 5 for h in hit_at for n in neg_at) or bool(re.search(r"\bonly service animals\b", best))
    if key.lower().startswith(("no_", "not_")):
        negated = not negated
    exc = re.search(r"\b(except|other than|but not|apart from)\b(.*)", best)
    if subject and exc and len(subject & tokens(exc.group(2))) >= max(1, len(subject) - 1):
        negated = not negated
    return (not negated, best_hits) if with_hits else not negated


# --------------------------------------------------------------------------- report

INFORMATIONAL = frozenset({"routing"})
"""Routing asks the gold profile's own customer phrasings through the server's policy matcher. A miss there is
the matcher's recall (its synonyms and thresholds), not something onboarding extracted wrong, so it is
reported beside the mismatches rather than counted among them."""


@dataclass
class Check:
    dimension: str
    what: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    profile_id: str
    gold_id: str
    checks: list[Check] = field(default_factory=list)

    def add(self, dimension: str, what: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(dimension, what, ok, detail))
        return ok

    @property
    def mismatches(self) -> list[Check]:
        """What the onboarded profile gets wrong or leaves out (routing is reported apart: see `recall_gaps`)."""
        return [c for c in self.checks if not c.ok and c.dimension not in INFORMATIONAL]

    @property
    def recall_gaps(self) -> list[Check]:
        return [c for c in self.checks if not c.ok and c.dimension in INFORMATIONAL]

    def summary(self) -> JsonObj:
        dims: dict[str, list[int]] = {}
        for c in self.checks:
            d = dims.setdefault(c.dimension, [0, 0])
            d[0] += c.ok
            d[1] += 1
        counted = [c for c in self.checks if c.dimension not in INFORMATIONAL]
        return {
            "profile_id": self.profile_id,
            "gold": self.gold_id,
            "checks": len(counted),
            "mismatches": len(self.mismatches),
            "coverage": round(sum(c.ok for c in counted) / len(counted), 4) if counted else 1.0,
            "by_dimension": {k: {"ok": v[0], "checks": v[1]} for k, v in dims.items()},
            "matcher_recall_gaps": len(self.recall_gaps),
        }

    def as_dict(self) -> JsonObj:
        return {**self.summary(), "mismatch_list": [vars(c) for c in self.mismatches],
                "matcher_recall_gap_list": [vars(c) for c in self.recall_gaps]}


# --------------------------------------------------------------------------- profile views


def catalogs(doc: JsonObj) -> list[tuple[str, JsonObj]]:
    return [(k, m) for k, m in doc["modules"].items() if m["type"] == "catalog"]


def all_items(doc: JsonObj) -> dict[str, JsonObj]:
    return {f"{mid}/{it['id']}": it for mid, m in catalogs(doc) for it in m.get("items", [])}


def knowledge_entries(doc: JsonObj) -> dict[str, JsonObj]:
    return {f"{k}/{e['id']}": e for k, m in doc["modules"].items() if m["type"] == "knowledge" for e in m["entries"]}


def entry_text(e: JsonObj) -> str:
    facts = " ".join(f"{k.replace('_', ' ')} {v}" for k, v in (e.get("facts") or {}).items())
    return " ".join([e.get("topic", "").replace("_", " "), e["id"].replace("-", " "), *e.get("question_variants", []),
                     e.get("answer_spoken", ""), e.get("answer_card", ""), facts])


def entry_atoms(e: JsonObj) -> set[tuple[str, float]]:
    out = text_atoms(" ".join([e.get("answer_spoken", ""), e.get("answer_card", "")]))
    for k, v in (e.get("facts") or {}).items():
        out |= value_atoms(k, v) | (text_atoms(v) if isinstance(v, str) else set())
        if isinstance(v, list):
            out |= {a for x in v for a in text_atoms(x)}
    return out


def entry_atoms_spoken(e: JsonObj) -> set[tuple[str, float]]:
    out = text_atoms(e["answer_spoken"])
    for k, v in (e.get("facts") or {}).items():
        out |= value_atoms(k, v)
    return out


def item_text(it: JsonObj) -> str:
    return it.get("name") or it.get("spoken_name") or it["id"]


def norm_price(p: JsonObj | None, name: str = "") -> tuple | None:
    """A price as (kind, amount, max, per); 'per visit' on a 'Diagnostic visit' says nothing more than the name."""
    if not p:
        return None
    per = p.get("per")
    if per in (None, "each", "item_each") or (per and stem(per) in tokens(name)):
        per = None
    return (p.get("kind"), p.get("amount_cents"), p.get("max_cents"), per)


def price_str(p: JsonObj | None) -> str:
    if not p:
        return "no price"
    kind = p.get("kind")
    amount = p.get("amount_cents")
    s = {"quote_only": "quote only", "free": "free"}.get(kind) or f"{kind} ${(amount or 0) / 100:.2f}"
    if p.get("max_cents"):
        s += f"-${p['max_cents'] / 100:.2f}"
    if p.get("per"):
        s += f" per {p['per']}"
    return s


def weekly_map(weekly: Iterable[JsonObj] | None) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for line in weekly or []:
        for d in line.get("days", []):
            out[d] = (line["open"], line["close"])
    return out


def closures_set(closures: Iterable[JsonObj] | None) -> set[tuple[str, str]]:
    out = set()
    for c in closures or []:
        start = c["from"]
        end = c.get("to", start)
        # A closure over several days is the same as single days back to back.
        day = start
        while day <= end:
            out.add(day)
            y, m, d = map(int, day.split("-"))
            import datetime as _dt

            day = (_dt.date(y, m, d) + _dt.timedelta(days=1)).isoformat()
    return out


def open_days(doc: JsonObj) -> set[str]:
    for loc in doc["business"]["locations"]:
        if loc.get("kind") == "storefront" and loc.get("hours"):
            return set(weekly_map(loc["hours"].get("weekly")))
    return set(DAYS)


# --------------------------------------------------------------------------- comparison


class Comparer:
    def __init__(self, onboarded: JsonObj, gold: JsonObj) -> None:
        self.o, self.g = onboarded, gold
        self.r = Report(onboarded["profile_id"], gold["profile_id"])
        self.item_map: dict[str, str] = {}  # gold ref -> onboarded ref
        self.choice_map: dict[str, str] = {}  # gold item/group/choice -> onboarded item/group/choice
        self.entry_map: dict[str, list[str]] = {}  # gold entry ref -> onboarded entry refs (best first)
        self.resource_map: dict[str, str] = {}

    def run(self) -> Report:
        self.business()
        self.locations()
        self.items()
        self.knowledge()
        self.flows()
        self.owner()
        return self.r

    # ------------------------------------------------------------------ business

    def business(self) -> None:
        g, o = self.g["business"], self.o["business"]
        add = self.r.add
        add("business", "name", tokens(g["name"]) == tokens(o["name"]), f"gold {g['name']!r}, onboarded {o['name']!r}")
        for key in ("timezone", "currency"):
            add("business", key, g.get(key) == o.get(key), f"gold {g.get(key)}, onboarded {o.get(key)}")
        for key in ("phone", "email"):
            gv, ov = (g.get("contact") or {}).get(key), (o.get("contact") or {}).get(key)
            if gv:
                add("business", key, str(gv).lower() == str(ov or "").lower(), f"gold {gv}, onboarded {ov}")
        ga = next((loc.get("address") for loc in g["locations"] if loc.get("address")), None)
        oa = next((loc.get("address") for loc in o["locations"] if loc.get("address")), None)
        if ga:
            if not oa:
                add("business", "address", False, "gold has a street address, onboarded has none")
            else:
                for key in ("locality", "region", "postal_code", "country"):
                    add("business", f"address.{key}", tokens(ga.get(key)) == tokens(oa.get(key)),
                        f"gold {ga.get(key)!r}, onboarded {oa.get(key)!r}")
                gl = tokens(f"{ga.get('line1', '')} {ga.get('line2', '')}")
                ol = tokens(f"{oa.get('line1', '')} {oa.get('line2', '')}")
                add("business", "address.street", gl == ol, f"gold {ga.get('line1')} {ga.get('line2', '')}, onboarded {oa.get('line1')} {oa.get('line2', '')}")

    # ------------------------------------------------------------------ locations: hours and areas

    def locations(self) -> None:
        g_locs, o_locs = self.g["business"]["locations"], self.o["business"]["locations"]
        g_store = [loc for loc in g_locs if loc["kind"] == "storefront"]
        o_store = [loc for loc in o_locs if loc["kind"] == "storefront"]
        for gl in g_store:
            ol = o_store[0] if o_store else None
            self.hours(f"storefront {gl['id']}", gl.get("hours"), ol.get("hours") if ol else None)
        g_areas = [loc for loc in g_locs if loc["kind"] == "service_area"]
        o_areas = [loc for loc in o_locs if loc["kind"] == "service_area"]
        used: set[str] = set()
        for ga in g_areas:
            garea = ga.get("area") or {}
            gp, gn = set(garea.get("postal_codes", [])), {ascii_lower(x) for x in garea.get("localities", [])}
            best, score = None, 0.0
            for oa in o_areas:
                if oa["id"] in used:
                    continue
                oarea = oa.get("area") or {}
                op, on = set(oarea.get("postal_codes", [])), {ascii_lower(x) for x in oarea.get("localities", [])}
                s = dice(gp | gn, op | on) + (0.5 if oa["id"] == ga["id"] else 0)
                if s > score:
                    best, score = oa, s
            if best is None:
                self.r.add("areas", f"service area {ga['id']}", False, "no onboarded service area")
                continue
            used.add(best["id"])
            oarea = best.get("area") or {}
            op, on = set(oarea.get("postal_codes", [])), {ascii_lower(x) for x in oarea.get("localities", [])}
            for code in sorted(gp | op):
                self.r.add("areas", f"{ga['id']}: postal code {code}", code in gp and code in op,
                           "missing" if code in gp else f"not in gold {ga['id']}")
            for place in sorted(gn | on):
                in_g = place in gn or any(place.startswith(x) or x.startswith(place) for x in gn)
                in_o = place in on or any(place.startswith(x) or x.startswith(place) for x in on)
                if place in on and not in_g and place in self.city_names():
                    continue  # the business's own city named as the area is not an extra place
                self.r.add("areas", f"{ga['id']}: place {place}", in_g and in_o, "missing" if place in gn else f"not in gold {ga['id']}")
            if ga.get("hours") or best.get("hours"):
                self.hours(f"service area {ga['id']}", ga.get("hours"), best.get("hours"))
        for oa in o_areas:
            if oa["id"] not in used:
                self.r.add("areas", f"service area {oa['id']}", False, "onboarded service area not in gold")

    def city_names(self) -> set[str]:
        return {ascii_lower(loc.get("address", {}).get("locality")) for loc in self.g["business"]["locations"] if loc.get("address")}

    def hours(self, where: str, gh: JsonObj | None, oh: JsonObj | None) -> None:
        if not gh:
            if oh and oh.get("weekly"):
                self.r.add("hours", f"{where} hours", False, "onboarded states hours the gold profile does not")
            return
        gw, ow = weekly_map(gh.get("weekly")), weekly_map((oh or {}).get("weekly"))
        for day in DAYS:
            if day in gw or day in ow:
                self.r.add("hours", f"{where} {day}", gw.get(day) == ow.get(day), f"gold {gw.get(day) or 'closed'}, onboarded {ow.get(day) or 'closed'}")
        gc, oc = closures_set(gh.get("closures")), closures_set((oh or {}).get("closures"))
        for day in sorted(gc | oc):
            self.r.add("hours", f"{where} closure {day}", day in gc and day in oc, "missing" if day in gc else "not in gold")

    # ------------------------------------------------------------------ items

    def items(self) -> None:
        g_items, o_items = all_items(self.g), all_items(self.o)
        pairs = []
        for gref, gi in g_items.items():
            gt = tokens(item_text(gi))
            for oref, oi in o_items.items():
                s = dice(gt, tokens(item_text(oi)))
                if norm_price(gi.get("price")) and norm_price(gi.get("price")) == norm_price(oi.get("price")):
                    s += 0.15
                if s >= 0.5:
                    pairs.append((s, gref, oref))
        pairs.sort(key=lambda p: -p[0])
        used: set[str] = set()
        for _, gref, oref in pairs:
            if gref in self.item_map or oref in used:
                continue
            self.item_map[gref] = oref
            used.add(oref)
        # Gold choices, for spotting a variant extracted as an item of its own.
        g_choices = [
            (gref, og, c) for gref, gi in g_items.items() for og in gi.get("options", []) for c in og["choices"]
        ]
        for gref, gi in g_items.items():
            oref = self.item_map.get(gref)
            if not self.r.add("items", f"item {gi['name']}", oref is not None, "no onboarded item" if oref is None else ""):
                continue
            self.item(gref, gi, oref, o_items[oref])
        for oref, oi in o_items.items():
            if oref in used:
                continue
            ot = tokens(item_text(oi))
            near_item = max(g_items.items(), key=lambda kv: dice(ot, tokens(item_text(kv[1]))))
            near_choice = max(g_choices, key=lambda t: dice(ot, tokens(t[2].get("name") or t[2]["spoken_name"]) | tokens(item_text(g_items[t[0]])) & ot), default=None)
            if near_choice and dice(ot, tokens(near_choice[2].get("name") or near_choice[2]["spoken_name"]) | (tokens(item_text(g_items[near_choice[0]])) & ot)) >= 0.5:
                self.r.add("items", f"item {oi['name']}", False,
                           f"extracted as its own item; gold has it as the {near_choice[1]['name']!r} choice "
                           f"{near_choice[2].get('name') or near_choice[2]['spoken_name']!r} of {g_items[near_choice[0]]['name']!r}")
            elif dice(ot, tokens(item_text(near_item[1]))) >= 0.4:
                self.r.add("items", f"item {oi['name']}", False, f"duplicate of {near_item[1]['name']!r}")
            else:
                self.r.add("items", f"item {oi['name']}", False, "onboarded item not in gold")

    def item(self, gref: str, gi: JsonObj, oref: str, oi: JsonObj) -> None:
        name = gi["name"]
        add = self.r.add
        add("items", f"{name}: price", norm_price(gi.get("price"), name) == norm_price(oi.get("price"), oi["name"]),
            f"gold {price_str(gi.get('price'))}, onboarded {price_str(oi.get('price'))}")
        if gi.get("duration_minutes") or oi.get("duration_minutes"):
            add("items", f"{name}: duration", gi.get("duration_minutes") == oi.get("duration_minutes"),
                f"gold {gi.get('duration_minutes')}, onboarded {oi.get('duration_minutes')}")
        self.safety(name, gi, oi)
        self.days(gref, name, gi, oref, oi)
        o_choices = [(og, c) for og in oi.get("options", []) for c in og["choices"]]
        for og in gi.get("options", []):
            groups_hit = []
            for c in og["choices"]:
                cname = c.get("name") or c["spoken_name"]
                ct = tokens(cname)
                best = max(o_choices, key=lambda t: dice(ct, tokens(t[1].get("name") or t[1]["spoken_name"]))
                           + (0.15 if norm_price(c.get("price") or c.get("price_delta")) and norm_price(c.get("price") or c.get("price_delta")) == norm_price(t[1].get("price") or t[1].get("price_delta")) else 0),
                           default=None)
                score = dice(ct, tokens(best[1].get("name") or best[1]["spoken_name"])) if best else 0
                if best is not None and score < 0.4:
                    # Choice names often drop words the item name already has ("Balayage, past the shoulder blades").
                    score = dice(ct - tokens(name), tokens(best[1].get("name") or best[1]["spoken_name"]) - tokens(name))
                label = f"{name}: {og['name']} choice {cname!r}"
                gp_ = c.get("price") or {}
                matched = best is not None and score >= 0.4
                if matched and not gp_.get("amount_cents") and norm_price(gp_) != norm_price(best[1].get("price")) and self.stated_for(c, oi):
                    matched = False  # a quote-only case the onboarded item states in words, not a priced row
                if not matched and not gp_.get("amount_cents") and self.stated_for(c, oi):
                    add("items", label, True, "stated in the item's notes")
                    continue
                if not add("items", label, matched, "no matching choice" if o_choices else "onboarded item has no options"):
                    continue
                obg, oc = best
                groups_hit.append(obg["id"])
                self.choice_map[f"{gref}/{og['id']}/{c['id']}"] = f"{oref}/{obg['id']}/{oc['id']}"
                gp, op = c.get("price"), oc.get("price")
                gd, od = c.get("price_delta"), oc.get("price_delta")
                if gp or op or gd or od:
                    add("items", f"{label} price", norm_price(gp) == norm_price(op) and norm_price(gd) == norm_price(od),
                        f"gold {price_str(gp) if gp else ('+' + price_str(gd) if gd else 'no price')}, "
                        f"onboarded {price_str(op) if op else ('+' + price_str(od) if od else 'no price')}")
                if c.get("duration_minutes") or oc.get("duration_minutes"):
                    add("items", f"{label} duration", c.get("duration_minutes") == oc.get("duration_minutes"),
                        f"gold {c.get('duration_minutes')}, onboarded {oc.get('duration_minutes')}")
                self.safety(label, c, oc)
                self.attributes(label, c.get("attributes"), oc, oi, gi)
            if groups_hit:
                add("items", f"{name}: {og['name']} is one question", len(set(groups_hit)) == 1,
                    f"gold's choices are spread over onboarded groups {sorted(set(groups_hit))}")
                obg = next(g for g in oi["options"] if g["id"] == groups_hit[0])
                # A required group with a free default ("Drywall" beside "+$100 brick") prices like optional extras.
                free_default = og.get("required") and any(not c.get("price") and not c.get("price_delta") for c in og["choices"]) and \
                    all(c.get("price_delta") or not (c.get("price") or {}).get("amount_cents") for c in og["choices"])
                if not free_default:
                    add("items", f"{name}: {og['name']} required", bool(og.get("required")) == bool(obg.get("required")),
                        f"gold required={bool(og.get('required'))}, onboarded {bool(obg.get('required'))}")
        g_names = [tokens(c.get("name") or c["spoken_name"]) for og in gi.get("options", []) for c in og["choices"]]
        for og, c in o_choices:
            if f"{oref}/{og['id']}/{c['id']}" not in self.choice_map.values():
                ct = tokens(c.get("name") or c["spoken_name"])
                if not any(dice(ct, g) >= 0.4 for g in g_names):
                    add("items", f"{name}: choice {c.get('name') or c['spoken_name']!r}", False, "onboarded choice not in gold")
        self.attributes(f"{name}", gi.get("attributes"), oi, oi, gi)

    def stated_for(self, choice: JsonObj, oi: JsonObj) -> bool:
        """A choice without its own price (a quote-only case, an add-on) that the onboarded item states in words:
        most of its name, and its add-on price if it has one, are in the item's description, notes or the policy
        entries about it."""
        blob = self.item_blob(oi)
        name = choice.get("name") or choice["spoken_name"]
        delta = (choice.get("price_delta") or {}).get("amount_cents")
        ok = recall(tokens(name) - tokens(item_text(oi)), tokens(blob)) >= 0.6
        return ok and (delta is None or ("cents", float(delta)) in text_atoms(blob))

    def item_blob(self, oi: JsonObj) -> str:
        name_t = tokens(item_text(oi))
        texts = [json.dumps(oi.get("attributes") or {}), oi.get("description_spoken", ""), item_text(oi)]
        texts += [json.dumps(c.get("attributes") or {}) + " " + (c.get("name") or "") for og in oi.get("options", []) for c in og["choices"]]
        for e in knowledge_entries(self.o).values():
            et = entry_text(e)
            if recall(name_t, tokens(et)) >= 0.75 or any(str(a).split("/")[-1] == oi["id"] for a in e.get("applies_to") or []):
                texts.append(f"{e['answer_spoken']} {e.get('answer_card', '')}")  # what the entry says, not its keys
        return " ".join(texts)

    def safety(self, label: str, g: JsonObj, o: JsonObj) -> None:
        for key, inner in (("allergens", "contains"), ("dietary", "tags")):
            gv = set((g.get(key) or {}).get(inner, []))
            ov = set((o.get(key) or {}).get(inner, []))
            if gv or ov:
                self.r.add("items", f"{label}: {key}", gv == ov, f"gold {sorted(gv)}, onboarded {sorted(ov)}")

    def effective_days(self, doc: JsonObj, ref: str, item: JsonObj) -> set[str]:
        days = set((item.get("availability") or {}).get("days") or open_days(doc))
        for m in doc["modules"].values():
            if m["type"] != "booking":
                continue
            svc = next((s for s in m["services"] if s["item"] == ref), None)
            if svc is None:
                continue
            res = {r["id"]: r for r in m.get("resources", [])}
            allowed = [res[i] for i in svc.get("resource_ids") or res if i in res]
            if allowed:
                works: set[str] = set()
                for r in allowed:
                    works |= set((r.get("attributes") or {}).get("works_days") or DAYS)
                days &= works
        return days & open_days(doc)

    def days(self, gref: str, name: str, gi: JsonObj, oref: str, oi: JsonObj) -> None:
        gd, od = self.effective_days(self.g, gref, gi), self.effective_days(self.o, oref, oi)
        if gd != open_days(self.g) or od != open_days(self.o):
            self.r.add("items", f"{name}: days available", gd == od,
                       f"gold {[d for d in DAYS if d in gd]}, onboarded {[d for d in DAYS if d in od]}")

    def attributes(self, label: str, gattrs: JsonObj | None, o: JsonObj, oi: JsonObj, gold_item: JsonObj | None = None) -> None:
        """Each attribute the site states (a number, yes/no or short text) is said somewhere about the item:
        its attributes, description, choices, or a policy entry about it. A number counts as site-stated
        when the gold profile's own evidence for the item prints it (gold sometimes derives one, e.g.
        'tiers: 1' for a round cake, which no page says)."""
        if not gattrs:
            return
        blob = " ".join([json.dumps(o.get("attributes") or {}), o.get("description_spoken", ""), self.item_blob(oi)])
        atoms = text_atoms(blob)
        for d in (o.get("attributes") or {}, oi.get("attributes") or {}):
            for k, v in d.items():
                atoms |= value_atoms(k, v)
        gold_text = " ".join(e.get("quote", "") for e in ((gold_item or {}).get("provenance") or {}).get("evidence", []))
        subject = tokens(o.get("name") or item_text(oi))
        for key, value in gattrs.items():
            if key.startswith("x-") or key in ("trade",):
                continue
            what = f"{label}: {key.replace('_', ' ')}"
            if isinstance(value, bool):
                said = None
                for d in (o.get("attributes") or {}, oi.get("attributes") or {}):
                    for k, v in d.items():
                        if isinstance(v, bool) and tokens(k.replace("_", " ")) & (tokens(key.replace("_", " ")) - GENERIC_KEY_WORDS):
                            said = v
                if said is None:
                    said = bool_claim(key, blob, subject)
                self.r.add("items", what, said == value, f"gold {value}, onboarded {'not stated' if said is None else said}")
            elif isinstance(value, int | float):
                if gold_text and ("count", float(value)) not in text_atoms(gold_text) and not (value_atoms(key, value) & text_atoms(gold_text)):
                    continue
                ok = bool(value_atoms(key, value) - {("count", float(value))} & atoms) if key_unit(key)[0] != "count" else ("count", float(value)) in atoms
                self.r.add("items", what, ok, f"gold {value}, not stated for the onboarded item")
            elif isinstance(value, str) and not re.fullmatch(r"[a-z-]+/[a-z0-9-]+", value):
                rc = recall(tokens(value), tokens(blob))
                self.r.add("items", what, rc >= 0.6, f"gold {value!r}, onboarded says {rc:.0%} of it")
            elif isinstance(value, str):
                target = all_items(self.g).get(value)
                if target:
                    rc = recall(tokens(item_text(target)), tokens(blob))
                    self.r.add("items", what, rc >= 0.6, f"gold requires {target['name']!r} first; not stated")

    # ------------------------------------------------------------------ knowledge

    def o_profile(self) -> Any:
        """The onboarded profile as the server reads it (for routing questions with its own matcher)."""
        from askonce_server.profile import load_profile

        return load_profile(self.o, schema_path=REPO_ROOT / "schema" / "business-profile.schema.json", strict=False).profile

    def knowledge(self) -> None:
        """Content: each gold entry's subject is covered by onboarded entries (found by what they are about:
        their questions and answers against the gold entry's), whose spoken answers and facts say the gold
        entry's numbers and yes/no facts and most of its wording. Routing: the gold entry's own customer
        phrasings go through the server's policy matcher (`ask_policy`'s `best_entry`) on the onboarded
        profile, and at least one of them must reach an entry that covers the subject; phrasings that get
        no answer are listed (the matcher's recall, tracked separately from what onboarding extracted)."""
        from askonce_server.tools.knowledge import best_entry

        g_entries, o_entries = knowledge_entries(self.g), knowledge_entries(self.o)
        profile = self.o_profile()
        o_keys = {ref: tokens(" ".join([*e.get("question_variants", []), e["answer_spoken"],
                                        " ".join(f"{k.replace('_', ' ')} {v}" for k, v in (e.get("facts") or {}).items())]))
                  for ref, e in o_entries.items()}
        self.routing_misses: list[tuple[str, str]] = []
        import math

        df: dict[str, int] = {}
        for k in o_keys.values():
            for w in k:
                df[w] = df.get(w, 0) + 1
        idf = {w: math.log((len(o_keys) + 1) / (n + 0.5)) for w, n in df.items()}
        for gref, ge in g_entries.items():
            label = f"{ge['id']}"
            g_key = tokens(" ".join([*ge.get("question_variants", []), ge["answer_spoken"]]))
            ranked = sorted(o_entries, key=lambda r: -weighted_dice(g_key, o_keys[r], idf))
            top = [r for r in ranked[:3] if dice(g_key, o_keys[r]) >= 0.15]
            if top:
                # Statements nearly as close as the best are about the same subject too.
                best = weighted_dice(g_key, o_keys[top[0]], idf)
                top += [r for r in ranked[3:8] if weighted_dice(g_key, o_keys[r], idf) >= 0.7 * best]
            routed: list[tuple[str, str | None]] = []
            for q in ge.get("question_variants") or []:
                m = best_entry(profile, q, None)
                routed.append((q, f"{m[0]}/{m[1]['id']}" if m else None))
            hits = [r for _, r in routed if r]
            reached = list(dict.fromkeys(hits))
            # The entries about the subject: the closest ones, the rest of their topic (a section's statements
            # share one), and any entry the matcher reached that says what the gold entry says.
            topics = {o_entries[r]["topic"] for r in top[:1]}
            group = [r for r, e in o_entries.items() if e["topic"] in topics]
            covering = list(dict.fromkeys(top + group + reached))
            gold_quotes = " ".join(ev.get("quote", "") for ev in (ge.get("provenance") or {}).get("evidence", []))
            self.entry_map[gref] = covering
            if not self.r.add("knowledge", f"{label}: covered", bool(top), "no onboarded entry is about this"):
                continue
            ok_route = any(r in covering for r in hits)
            self.r.add("routing", f"{label}: reachable", ok_route,
                       f"none of {len(routed)} phrasings reaches it, e.g. {[q for q, _ in routed][:2]}")
            self.routing_misses += [(label, q) for q, r in routed if r is None or r not in covering]
            said = " ".join(o_entries[r]["answer_spoken"] for r in covering)
            # Items the gold answer names say their own details (a fee's conditions are on the fee).
            said_items = " ".join(f"{i.get('description_spoken', '')} {price_words(i)}" for i in all_items(self.o).values()
                                  if len(tokens(item_text(i))) >= 1 and recall(tokens(item_text(i)), tokens(ge["answer_spoken"])) >= 0.75)
            atoms = text_atoms(said) | text_atoms(said_items)
            facts_o: JsonObj = {}
            for r in covering:
                for k, v in (o_entries[r].get("facts") or {}).items():
                    facts_o.setdefault(k, v)
                    atoms |= value_atoms(k, v)
            where = ", ".join(r.split("/")[-1] for r in covering)
            qs = [q for r in covering for q in o_entries[r].get("question_variants") or []]
            for key, value in (ge.get("facts") or {}).items():
                what = f"{label}: {key.replace('_', ' ')}"
                if not self.site_stated(key, value, gold_quotes):
                    continue  # gold derived it (a phone number from another page, a postcode it added to the answer)
                if isinstance(value, bool):
                    claim = next((v for k, v in facts_o.items() if isinstance(v, bool)
                                  and (tokens(k.replace("_", " ")) - GENERIC_KEY_WORDS) & (tokens(key.replace("_", " ")) - GENERIC_KEY_WORDS)), None)
                    if claim is None:
                        claim = self.bool_in(key, covering, o_entries)
                    if claim is None and said_items:
                        claim = bool_claim(key, said_items)
                    self.r.add("knowledge", what, claim == value, f"gold {value}, onboarded {'not stated' if claim is None else claim} (in {where})")
                elif isinstance(value, int | float):
                    want = value_atoms(key, value)
                    if key_unit(key)[0] != "count":
                        want -= {("count", float(value))}
                    ok = bool(want & atoms)
                    if not ok and value == 0:
                        ok = bool_claim(re.sub(r"_(percent|cents|minutes|count)$", "", key), said) is False  # "no sales tax"
                    elsewhere = None if ok else self.said_elsewhere(lambda e: bool(want & entry_atoms_spoken(e)), profile)
                    self.r.add("knowledge", what, ok or elsewhere is not None,
                               f"gold {value} ({key_unit(key)[0]}), not said by {where}" if elsewhere is None else f"said by {elsewhere}")
                elif isinstance(value, list):
                    have = tokens(said) | tokens(" ".join(str(v) for v in facts_o.values()))
                    for v in value:
                        vt = tokens(str(v).replace("_", " ")) - {"card"}
                        self.r.add("knowledge", f"{what}: {v}", recall(vt, have) >= 0.5, f"{v!r} not said by {where}")
                elif isinstance(value, str):
                    blob = said + " " + " ".join(str(v) for v in facts_o.values())
                    digits = re.sub(r"\D", "", value)
                    if value.startswith("+") and len(digits) >= 10:
                        ok = digits[-10:] in re.sub(r"\D", "", blob)
                    elif re.fullmatch(r"\d\d:\d\d", value):
                        ok = value in blob or _clock_in(value, blob)
                    else:
                        ok = recall(tokens(value.replace("_", " ")), tokens(blob)) >= 0.6
                    elsewhere = None
                    if not ok and value.startswith("+") and len(digits) >= 10:
                        elsewhere = self.said_elsewhere(lambda e: digits[-10:] in re.sub(r"\D", "", e["answer_spoken"]), profile)
                    self.r.add("knowledge", what, ok or elsewhere is not None,
                               f"gold {value!r}, not said by {where}" if elsewhere is None else f"said by {elsewhere}")
            want = tokens(ge["answer_spoken"]) - tokens(self.g["business"]["name"])
            rc = recall(want, tokens(said))
            self.r.add("knowledge", f"{label}: wording", rc >= 0.5,
                       f"{where} say {rc:.0%} of {ge['answer_spoken'][:90]!r} (missing {sorted(want - tokens(said))[:8]})")

        # Duplicates: two onboarded entries saying the same thing confuse the matcher and the owner.
        refs = list(o_entries)
        for i, a in enumerate(refs):
            for b in refs[i + 1:]:
                ta = tokens(o_entries[a]["answer_spoken"])
                tb = tokens(o_entries[b]["answer_spoken"])
                if ta and tb and dice(ta, tb) >= 0.8:
                    self.r.add("knowledge", f"entry {b.split('/')[-1]}", False, f"duplicates {a.split('/')[-1]}")

    def said_elsewhere(self, says, profile: Any) -> str | None:
        """A fact the gold entry combines from another part of the site (a phone number, a notice period) that
        the onboarded profile states in an entry of its own, which its own question reaches."""
        from askonce_server.tools.knowledge import best_entry

        for ref, e in knowledge_entries(self.o).items():
            if not says(e):
                continue
            for q in e.get("question_variants", [])[:4]:
                m = best_entry(profile, q, None)
                if m and f"{m[0]}/{m[1]['id']}" == ref:
                    return ref.split("/")[-1]
        return None

    def bool_in(self, key: str, refs: list[str], entries: dict[str, JsonObj]) -> bool | None:
        """A yes/no fact as the entries say it: the entry whose statement names most of the key's words decides
        (each entry is read with its own question, so a bare "No." answers the question it was asked)."""
        best, best_hits = None, 0
        for r in refs:
            e = entries[r]
            claim, hits = bool_claim(key, e["answer_spoken"], questions=e.get("question_variants") or [], with_hits=True)
            if claim is not None and hits > best_hits:
                best, best_hits = claim, hits
        return best

    @staticmethod
    def site_stated(key: str, value: Any, quotes: str) -> bool:
        """A gold fact counts when the gold entry's own evidence quotes state it (any yes/no fact counts: it is
        a reading of the quote, not a value copied from it)."""
        if isinstance(value, bool) or not quotes:
            return True
        if isinstance(value, int | float):
            atoms = text_atoms(quotes)
            want = value_atoms(key, value)
            return bool(want & atoms) or (value == 0)
        if isinstance(value, str):
            digits = re.sub(r"\D", "", value)
            if value.startswith("+") and len(digits) >= 10:
                return digits[-10:] in re.sub(r"\D", "", quotes)
            if re.fullmatch(r"\d\d:\d\d", value):
                return value in quotes or _clock_in(value, quotes)
            return recall(tokens(value.replace("_", " ")), tokens(quotes)) >= 0.6
        if isinstance(value, list):
            return any(recall(tokens(str(v).replace("_", " ")), tokens(quotes)) >= 0.5 for v in value)
        return True

    def states_gold(self, ge: JsonObj, oe: JsonObj) -> bool:
        """An entry the matcher reached that says what the gold entry says (its numbers, or most of its words)."""
        nums = {a for k, v in (ge.get("facts") or {}).items() for a in value_atoms(k, v) if a[0] != "count"}
        if nums and nums & text_atoms(oe["answer_spoken"]):
            return True
        return recall(tokens(ge["answer_spoken"]), tokens(oe["answer_spoken"])) >= 0.5

    # ------------------------------------------------------------------ flows

    def flows(self) -> None:
        o_mods = [(k, m) for k, m in self.o["modules"].items() if m["type"] in ("custom_order", "booking", "quote_request")]
        for gid, gm in self.g["modules"].items():
            if gm["type"] not in ("custom_order", "booking", "quote_request"):
                continue
            om = next((m for _, m in o_mods if m["type"] == gm["type"]), None)
            label = f"{gm['type']} {gid}"
            if not self.r.add("flows", label, om is not None, "no onboarded module of this type"):
                continue
            self.flow(label, gm, om)
        for _, om in o_mods:
            if not any(gm["type"] == om["type"] for gm in self.g["modules"].values()):
                self.r.add("flows", f"{om['type']} {om.get('title')}", False, "onboarded flow not in gold")

    def map_ref(self, gref: str) -> str | None:
        return self.item_map.get(gref) or self.choice_map.get(gref)

    def flow(self, label: str, gm: JsonObj, om: JsonObj) -> None:
        add = self.r.add
        g_refs = [s["item"] for s in gm.get("services", [])] or gm.get("items", [])
        o_refs = [s["item"] for s in om.get("services", [])] or om.get("items", [])
        for ref in g_refs:
            add("flows", f"{label}: takes {ref.split('/')[-1]}", self.map_ref(ref) in o_refs, "item not offered in the onboarded flow")
        mapped = {self.map_ref(r) for r in g_refs}
        for ref in o_refs:
            if ref not in mapped:
                add("flows", f"{label}: takes {ref.split('/')[-1]}", False, "onboarded flow takes an item gold's does not")
        # Approval
        ga, oa = gm.get("approval"), om.get("approval")
        if ga or oa:
            add("flows", f"{label}: approval mode", (ga or {}).get("mode") == (oa or {}).get("mode"),
                f"gold {(ga or {}).get('mode')}, onboarded {(oa or {}).get('mode')}")
            for rule in (ga or {}).get("rules", []):
                self.rule(label, rule, (oa or {}).get("rules", []))
            for rule in (oa or {}).get("rules", []):
                if not any(self.same_rule(g, rule) for g in (ga or {}).get("rules", [])):
                    add("flows", f"{label}: approval rule {rule['when']} {rule['op']} {rule.get('value')}", False, "onboarded rule not in gold")
        # Intake
        self.intake(label, gm.get("intake_fields", []), om.get("intake_fields", []))
        # Availability
        gav, oav = gm.get("availability") or {}, om.get("availability") or {}
        for key in ("lead_time_minutes", "capacity_per_day", "max_advance_minutes", "capacity_per_slot"):
            if key in gav or key in oav:
                add("flows", f"{label}: {key}", gav.get(key) == oav.get(key), f"gold {gav.get(key)}, onboarded {oav.get(key)}")
        if gav.get("weekly") or oav.get("weekly"):
            gw = weekly_map(gav.get("weekly"))
            ow = weekly_map(oav.get("weekly"))
            add("flows", f"{label}: weekly window", gw == ow, f"gold {gw}, onboarded {ow}")
        gc, oc = closures_set(gav.get("closures")), closures_set(oav.get("closures"))
        store_c = set()
        for loc in self.o["business"]["locations"]:
            store_c |= closures_set((loc.get("hours") or {}).get("closures"))
        for day in sorted(gc | oc):
            # A closure the business's own hours already carry applies to the flow too.
            add("flows", f"{label}: closed {day}", day in gc and (day in oc or day in store_c), "missing" if day in gc else "not in gold")
        # Fulfillment
        gf, of = (gm.get("fulfillment") or {}).get("modes"), (om.get("fulfillment") or {}).get("modes")
        if gf or of:
            add("flows", f"{label}: fulfillment", set(gf or []) == set(of or []), f"gold {gf}, onboarded {of}")
        # Booking specifics
        if gm["type"] == "booking":
            gs, os_ = (gm.get("slotting") or {}), (om.get("slotting") or {})
            add("flows", f"{label}: slot step", gs.get("step_minutes") == os_.get("step_minutes"), f"gold {gs.get('step_minutes')}, onboarded {os_.get('step_minutes')}")
            self.resources(label, gm, om)
        if gm.get("visit_windows") or om.get("visit_windows"):
            gwv = {(w["start"], w["end"]) for w in gm.get("visit_windows", [])}
            owv = {(w["start"], w["end"]) for w in om.get("visit_windows", [])}
            for w in sorted(gwv | owv):
                add("flows", f"{label}: visit window {w[0]}-{w[1]}", w in gwv and w in owv, "missing" if w in gwv else "not in gold")
        # Read out before confirming
        g_say = (gm.get("confirmation") or {}).get("say_before_confirm", [])
        o_say = set((om.get("confirmation") or {}).get("say_before_confirm", []))
        for ref in g_say:
            cands = self.entry_map.get(ref, [])
            add("flows", f"{label}: reads {ref.split('/')[-1]} before confirming", bool(set(cands) & o_say),
                f"onboarded reads {sorted(o_say) or 'nothing'}")
        add("flows", f"{label}: confirms before commit", (om.get("confirmation") or {}).get("before_commit") is True, "")

    def same_rule(self, g: JsonObj, o: JsonObj) -> bool:
        if g["when"] != o["when"] and not ({g["when"], o["when"]} <= {"servings", "party_size"}):
            return False
        if g["op"] != o["op"]:
            gv, ov = g.get("value"), o.get("value")
            # "> 24" and ">= 25" are the same rule for whole numbers.
            if isinstance(gv, int | float) and isinstance(ov, int | float):
                lo = {">": gv + 1, ">=": gv}.get(g["op"])
                lo2 = {">": ov + 1, ">=": ov}.get(o["op"])
                return lo is not None and lo == lo2
            return False
        if g["op"] == "in":
            return {self.map_ref(v) for v in g.get("value", [])} == set(o.get("value", []))
        return g.get("value") == o.get("value")

    def rule(self, label: str, rule: JsonObj, o_rules: list[JsonObj]) -> None:
        what = f"{label}: approval rule {rule['when']} {rule['op']} " + (
            str([v.split('/')[-1] for v in rule['value']]) if isinstance(rule.get("value"), list) else str(rule.get("value")))
        ok = any(self.same_rule(rule, o) for o in o_rules)
        detail = ""
        if not ok:
            close = [o for o in o_rules if o["when"] == rule["when"]]
            if close and rule["op"] == "in":
                want = {self.map_ref(v) for v in rule["value"]}
                have = set(close[0].get("value", []))
                detail = f"missing {sorted(x or '?' for x in want - have)}, extra {sorted(have - want)}"
            else:
                detail = f"onboarded rules: {[(o['when'], o['op'], o.get('value')) for o in o_rules] or 'none'}"
        self.r.add("flows", what, ok, detail)

    FIELD_ALIASES = {"name": "customer_name", "phone": "customer_phone", "email": "customer_email"}

    def intake(self, label: str, g_fields: list[JsonObj], o_fields: list[JsonObj]) -> None:
        def ftext(f: JsonObj) -> set[str]:
            return tokens(f["key"].replace("_", " ")) | tokens(f.get("label")) - {"customer", "your"}

        used: set[str] = set()
        for gf in g_fields:
            gkey = self.FIELD_ALIASES.get(gf["key"], gf["key"])
            cands = [f for f in o_fields if f["key"] not in used]
            best = next((f for f in cands if self.FIELD_ALIASES.get(f["key"], f["key"]) == gkey), None)
            if best is None:
                scored = sorted(cands, key=lambda f: -(dice(ftext(gf), ftext(f)) + (0.2 if f["type"] == gf["type"] else 0)))
                if scored and dice(ftext(gf), ftext(scored[0])) + (0.2 if scored[0]["type"] == gf["type"] else 0) >= 0.5:
                    best = scored[0]
            what = f"{label}: asks {gf['key']}"
            if not self.r.add("flows", what, best is not None, f"gold asks {gf.get('label')!r} ({gf['type']})"):
                continue
            used.add(best["key"])
            self.r.add("flows", f"{what} type", best["type"] == gf["type"] or {best["type"], gf["type"]} <= {"text", "long_text"},
                       f"gold {gf['type']}, onboarded {best['type']}")
            self.r.add("flows", f"{what} required", bool(gf.get("required")) == bool(best.get("required")),
                       f"gold required={bool(gf.get('required'))}, onboarded {bool(best.get('required'))}")
            if gf.get("type") in ("text", "long_text") and gf.get("max_length", 1000) <= 100 and not gf.get("pii"):
                self.r.add("flows", f"{what} limit", gf.get("max_length") == best.get("max_length"),
                           f"gold max {gf.get('max_length')}, onboarded {best.get('max_length')}")
            if gf.get("options"):
                o_opts = best.get("options") or []
                for opt in gf["options"]:
                    ot = tokens(opt["label"]) | tokens(opt["value"].replace("-", " "))
                    hit = any(dice(ot, tokens(x["label"]) | tokens(str(x["value"]).replace("-", " "))) >= 0.4
                              or self.map_ref(f"services/{opt['value']}") == f"services/{x['value']}" for x in o_opts)
                    self.r.add("flows", f"{what} option {opt['label']!r}", hit, f"onboarded options {[x['label'] for x in o_opts]}")
        for of in o_fields:
            if of["key"] not in used and of.get("required"):
                self.r.add("flows", f"{label}: asks {of['key']}", False, "onboarded requires a field gold does not ask")

    def resources(self, label: str, gm: JsonObj, om: JsonObj) -> None:
        o_res = om.get("resources", [])
        for gr in gm.get("resources", []):
            orr = next((r for r in o_res if tokens(r["spoken_name"]) & tokens(gr["spoken_name"])), None)
            if not self.r.add("flows", f"{label}: staff {gr['spoken_name']}", orr is not None, "not an onboarded resource"):
                continue
            self.resource_map[gr["id"]] = orr["id"]
            gw = set((gr.get("attributes") or {}).get("works_days") or DAYS)
            ow = set((orr.get("attributes") or {}).get("works_days") or DAYS)
            self.r.add("flows", f"{label}: {gr['spoken_name']} works", gw == ow,
                       f"gold {[d for d in DAYS if d in gw]}, onboarded {[d for d in DAYS if d in ow]}")
        for svc in gm.get("services", []):
            oref = self.map_ref(svc["item"])
            osvc = next((s for s in om.get("services", []) if s["item"] == oref), None)
            if osvc is None:
                continue
            all_g = [r["id"] for r in gm.get("resources", [])]
            all_o = [r["id"] for r in o_res]
            gset = {self.resource_map.get(r, f"?{r}") for r in svc.get("resource_ids") or all_g}
            oset = set(osvc.get("resource_ids") or all_o)
            self.r.add("flows", f"{label}: who does {svc['item'].split('/')[-1]}", gset == oset,
                       f"gold {sorted(gset)}, onboarded {sorted(oset)}")

    # ------------------------------------------------------------------ owner

    def owner(self) -> None:
        go = next(m for m in self.g["modules"].values() if m["type"] == "owner_approval")
        oo = next((m for m in self.o["modules"].values() if m["type"] == "owner_approval"), None)
        if not self.r.add("owner", "owner module", oo is not None, ""):
            return
        self.r.add("owner", "approver", tokens(go["approver"]["spoken_name"]) == tokens(oo["approver"]["spoken_name"]),
                   f"gold {go['approver']['spoken_name']}, onboarded {oo['approver']['spoken_name']}")
        self.r.add("owner", "reply time", go["expected_response_minutes"] == oo["expected_response_minutes"],
                   f"gold {go['expected_response_minutes']}, onboarded {oo['expected_response_minutes']}")


def _clock_in(value: str, text: str) -> bool:
    h, m = int(value[:2]), int(value[3:])
    h12 = (h % 12) or 12
    pat = rf"\b{h12}(:{m:02d})?\s*{'pm' if h >= 12 else 'am'}" if m else rf"\b{h12}(:00)?\s*{'pm' if h >= 12 else 'am'}"
    return bool(re.search(pat, ascii_lower(text).replace(".", ""))) or (h == 12 and "noon" in ascii_lower(text))


# --------------------------------------------------------------------------- entry points


def gold_for(doc: JsonObj, gold_dir: Path = GOLD_DIR) -> JsonObj:
    site = doc["business"]["website"].rstrip("/")
    for path in sorted(gold_dir.glob("*.json")):
        gold = json.loads(path.read_text(encoding="utf-8"))
        if gold["business"]["website"].rstrip("/") == site:
            return gold
    raise LookupError(f"no gold profile for {site}")


def answered(doc: JsonObj, answers_path: Path) -> JsonObj:
    """The profile as a customer meets it: the scripted owner answers applied to any open question."""
    from askonce_onboarding.answers import apply_answers

    if not any(q["status"] == "open" for q in doc.get("open_questions", [])) or not answers_path.is_file():
        return doc
    answers = {k: v for k, v in json.loads(answers_path.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    open_ids = {q["id"] for q in doc["open_questions"] if q["status"] == "open"}
    return apply_answers(copy.deepcopy(doc), {k: v for k, v in answers.items() if k in open_ids}, answered_at="2026-10-15T12:00:00Z")


def compare(doc: JsonObj, gold: JsonObj) -> Report:
    return Comparer(doc, gold).run()


def compare_demo(out_dir: Path = OUT_DIR, answers_dir: Path = ANSWERS_DIR) -> list[Report]:
    reports = []
    for path in sorted(out_dir.glob("*/profile.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc = answered(doc, answers_dir / f"{doc['profile_id']}.json")
        reports.append(compare(doc, gold_for(doc)))
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="askonce-compare", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("profiles", nargs="*", type=Path, help="onboarded profile.json files (default: --demo)")
    parser.add_argument("--demo", action="store_true", help="compare the 3 onboarded demo profiles in onboarding/out/")
    parser.add_argument("--out", type=Path, default=OUT_DIR)
    parser.add_argument("--json", type=Path, help="also write the full report here")
    parser.add_argument("--limit", type=int, default=0, help="print at most this many mismatches per business (0 = all)")
    args = parser.parse_args(argv)
    if args.profiles:
        reports = []
        for path in args.profiles:
            doc = json.loads(path.read_text(encoding="utf-8"))
            doc = answered(doc, ANSWERS_DIR / f"{doc['profile_id']}.json")
            reports.append(compare(doc, gold_for(doc)))
    else:
        reports = compare_demo(args.out)
    total = 0
    for rep in reports:
        s = rep.summary()
        dims = ", ".join(f"{k} {v['ok']}/{v['checks']}" for k, v in s["by_dimension"].items())
        print(f"{rep.profile_id} vs {rep.gold_id}: {s['mismatches']} mismatch(es) in {s['checks']} checks "
              f"({s['coverage']:.1%} match) [{dims}]")
        shown = rep.mismatches if not args.limit else rep.mismatches[: args.limit]
        for c in shown:
            print(f"   {c.dimension:9} {c.what}: {c.detail}")
        for c in rep.recall_gaps:
            print(f"   (matcher recall) {c.what}: {c.detail}")
        total += s["mismatches"]
    if args.json:
        args.json.write_text(json.dumps([r.as_dict() for r in reports], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
