"""Reduce step: build a schema-valid business profile and the owner's questions from quoted page facts.

Everything here is deterministic. Model output is treated as untrusted input: every value is
re-validated against the schema's shapes, every quote is re-located on its page (facts without a real
quote are dropped), safety facts (allergens, dietary claims, safety policies) are never written as
statable facts but turned into `confirm` questions, and the same fact read differently on two pages
becomes a blocking `contradiction` question instead of a guess.
"""

from __future__ import annotations

import re
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from askonce_onboarding.extract import PageFacts
from askonce_onboarding.injection import Canary
from askonce_onboarding.quotes import QuoteIndex
from askonce_onboarding.site import Page

JsonObj = dict[str, Any]

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_NAMES = dict(zip(DAYS, ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"), strict=True))
ALLERGENS = frozenset({"milk", "eggs", "fish", "shellfish", "tree_nuts", "peanuts", "wheat", "soybeans", "sesame"})
ALLERGEN_ALIASES = {"egg": "eggs", "soy": "soybeans", "nuts": "tree_nuts", "tree_nut": "tree_nuts", "peanut": "peanuts"}
DIETARY = frozenset(
    {"gluten_free", "vegan", "vegetarian", "dairy_free", "egg_free", "nut_free", "peanut_free", "soy_free",
     "sugar_free", "halal", "kosher"}
)
PRICE_KINDS = frozenset({"fixed", "from", "range", "quote_only", "free"})
US_STATE_TZ = {
    **dict.fromkeys(("OR", "WA", "CA", "NV"), "America/Los_Angeles"),
    **dict.fromkeys(("CO", "UT", "NM", "MT", "WY", "ID"), "America/Denver"),
    "AZ": "America/Phoenix",
    **dict.fromkeys(("TX", "IL", "MN", "WI", "MO", "IA", "LA", "OK", "KS", "NE", "AL", "MS", "AR", "TN"), "America/Chicago"),
    **dict.fromkeys(("NY", "NJ", "PA", "MA", "FL", "GA", "NC", "VA", "OH", "MI", "MD", "DC", "CT", "SC"), "America/New_York"),
}
RESPONSE_KEYS = ("owner_reply_minutes", "quote_response_minutes", "reply_minutes", "response_minutes")
DEFAULT_RESPONSE_MINUTES = 1440


# --------------------------------------------------------------------------- value hygiene


def slug(value: Any, fallback: str = "x") -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"&", " and ", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60].strip("-")
    return text or fallback


def snake(value: Any, fallback: str = "general") -> str:
    text = slug(value, "").replace("-", "_")
    text = re.sub(r"^[^a-z]+", "", text)[:40].rstrip("_")
    return text or fallback


def clean_text(value: Any, limit: int = 120) -> str | None:
    if not isinstance(value, str | int | float) or isinstance(value, bool):
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


_SPOKEN_BAD = re.compile(r"[<>{}\[\]|*#_~`\\]")
_NAME_BAD = re.compile(r"[<>{}\[\]|*#_~`@/\\]")


def spoken(value: Any, limit: int = 240) -> str | None:
    """At most two short sentences, no URLs or markup (the schema's `spoken` pattern)."""
    text = clean_text(value, 10_000)
    if not text or re.search(r"https?:|www\.", text, re.I):
        return None
    text = _SPOKEN_BAD.sub("", text)
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for sentence in sentences[:2]:
        candidate = f"{out} {sentence}".strip()
        if len(candidate) > limit:
            break
        out = candidate
    if not out:
        out = text[:limit].rsplit(" ", 1)[0] if len(text) > limit else text
    return out.strip() or None


def spoken_name(value: Any) -> str | None:
    text = clean_text(value, 1000)
    if not text:
        return None
    text = " ".join(_NAME_BAD.sub(" ", text).split())
    if len(text) > 40:
        text = text[:40].rsplit(" ", 1)[0]
    return text or None


def as_time(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if (h, mi) == (24, 0):
        return "24:00"
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def as_days(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    days = [str(d).strip().lower()[:3] for d in value]
    return [d for d in DAYS if d in days]


def as_int(value: Any, minimum: int = 0) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"\d+", value.strip()):
        value = int(value)
    return value if isinstance(value, int) and value >= minimum else None


def as_date(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])", value.strip()):
        return value.strip()
    return None


def e164(value: Any, country: str = "US") -> str | None:
    if not isinstance(value, str):
        return None
    digits = re.sub(r"\D", "", value)
    if value.strip().startswith("+") and 7 <= len(digits) <= 15:
        return "+" + digits
    if country == "US" and len(digits) == 10:
        return "+1" + digits
    if country == "US" and len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None


def fact_value(value: Any) -> Any:
    """A knowledge `facts` value: string (<=400), number, boolean, or a list flattened to text."""
    if isinstance(value, bool | int | float):
        return value
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if isinstance(v, str | int | float))
    if isinstance(value, str):
        text = " ".join(value.split())
        if text.lower() in ("true", "yes"):
            return True
        if text.lower() in ("false", "no"):
            return False
        if re.fullmatch(r"-?\d+", text):
            return int(text)
        return text[:400] or None
    return None


def human(value: Any, key: str = "") -> str:
    """A short label for a value, for owner-facing question text."""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int) and key.endswith("_minutes"):
        if value % 1440 == 0 and value >= 1440:
            days = value // 1440
            return f"{days * 24} hours" if days <= 3 else f"{days} days"
        if value % 60 == 0 and value >= 60:
            return f"{value // 60} hour" + ("s" if value != 60 else "")
        return f"{value} minutes"
    if isinstance(value, int) and key.endswith("_cents"):
        return f"${value / 100:,.2f}"
    if isinstance(value, int) and key.endswith("_percent"):
        return f"{value}%"
    if isinstance(value, str) and (t := as_time(value)):
        return clock12(t)
    return str(value)


def clock12(t: str) -> str:
    h, m = int(t[:2]), int(t[3:])
    if t == "24:00":
        return "midnight"
    suffix = "am" if h < 12 else "pm"
    return f"{(h % 12) or 12}:{m:02d} {suffix}"


# --------------------------------------------------------------------------- evidence registry


@dataclass
class Fact:
    """One quoted reading of one fact on one page, for contradiction detection and review."""

    id: str
    source_url: str
    page_title: str
    subject: str  # same subject on two pages = the same fact
    key: str  # value key (for labels)
    value: Any
    quote: str
    targets: list[str] = field(default_factory=list)
    describe: str = ""


@dataclass
class Result:
    profile: JsonObj
    questions: list[JsonObj]
    report: JsonObj


class Assembler:
    def __init__(
        self,
        pages: list[Page],
        page_facts: list[PageFacts],
        canaries: dict[str, list[Canary]],
        *,
        updated_at: str,
    ) -> None:
        self.pages = pages
        self.by_url = {p.url: p for p in pages}
        self.facts_by_url = {pf.page_url: pf.data for pf in page_facts}
        self.canaries = canaries
        self.updated_at = updated_at
        self.indexes = {p.url: QuoteIndex(p.html, [c.quote for c in canaries.get(p.url, [])]) for p in pages}
        self.dropped: list[JsonObj] = []
        self.registry: list[Fact] = []
        self.questions: list[JsonObj] = []
        self.vocab: dict[str, JsonObj] = {}
        self.home = pages[0].url
        parts = urlsplit(self.home)
        self.website = f"{parts.scheme}://{parts.netloc}/"

    # ------------------------------------------------------------------ helpers

    def records(self, section: str) -> Iterable[tuple[Page, JsonObj]]:
        for page in self.pages:
            data = self.facts_by_url.get(page.url) or {}
            items = data.get(section)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict):
                        yield page, item

    def evidence(self, page: Page, record: JsonObj, what: str) -> JsonObj | None:
        quote = self.indexes[page.url].locate(record.get("quote"))
        if quote is None:
            self.dropped.append({"page": page.url, "what": what, "quote": str(record.get("quote"))[:200]})
            return None
        return {"url": page.url, "quote": quote}

    def add_fact(self, page: Page, subject: str, key: str, value: Any, quote: str, targets: list[str], describe: str) -> None:
        self.registry.append(
            Fact(f"F{len(self.registry) + 1}", page.url, page.title, subject, key, value, quote, targets, describe)
        )

    @staticmethod
    def provenance(evidence: Iterable[JsonObj]) -> JsonObj | None:
        seen: list[JsonObj] = []
        for ev in evidence:
            if ev and ev not in seen:
                seen.append(ev)
        return {"owner_confirmed": False, "evidence": seen[:10]} if seen else None

    def page_label(self, url: str) -> str:
        page = self.by_url.get(url)
        title = page.title if page else url
        # "About Thimbleberry | Thimbleberry Bakehouse" -> "About Thimbleberry"
        title = re.split(r"\s[|·–—-]\s", title)[0].strip() or title
        return title[:60]

    # ------------------------------------------------------------------ business

    def business_fields(self) -> dict[str, list[tuple[Page, str, JsonObj]]]:
        fields: dict[str, list[tuple[Page, str, JsonObj]]] = {}
        for page, rec in self.records("business"):
            name = str(rec.get("field") or "").strip().lower()
            value = clean_text(rec.get("value"), 400)
            if not name or not value:
                continue
            ev = self.evidence(page, rec, f"business.{name}")
            if ev is None:
                continue
            fields.setdefault(name, []).append((page, value, ev))
        return fields

    def build(self, review: Callable[[list[JsonObj]], list[JsonObj]] | None = None) -> Result:
        fields = self.business_fields()

        def first(name: str) -> str | None:
            values = fields.get(name) or []
            return values[0][1] if values else None

        name = first("name") or urlsplit(self.home).hostname or "Business"
        region = (first("region") or "").strip()
        country = (first("country") or "").strip().upper()
        postal = first("postal_code")
        if len(country) != 2:
            country = "US" if (region.upper() in US_STATE_TZ or (postal and re.fullmatch(r"\d{5}", postal))) else ""
        currency = (first("currency") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", currency):
            currency = "USD"
        self.currency = currency
        timezone = self.timezone(first("timezone"), region)
        categories = list(OrderedDict.fromkeys(snake(v) for _, v, _ in fields.get("category", []) if snake(v, "")))
        self.template = choose_template(categories, name, self.offer_kinds())
        if not categories:
            categories = [TEMPLATE_CATEGORY[self.template]]

        locations = self.locations(fields, country or "US")
        business: JsonObj = {
            "name": clean_text(name) or "Business",
            "spoken_name": spoken_name(name) or "the business",
            "categories": categories[:8],
            "locale": "en-US" if (country or "US") == "US" else "en",
            "timezone": timezone,
            "currency": currency,
            "website": self.website,
            "locations": locations,
        }
        summary = spoken(first("summary"))
        if summary:
            business["summary_spoken"] = summary
        contact: JsonObj = {}
        phone = e164(first("phone"), country or "US")
        if phone:
            contact["phone"] = phone
        email = first("email")
        if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email, re.I):
            contact["email"] = email
        if contact:
            business["contact"] = contact
        evidence = [ev for key in ("name", "summary", "phone", "email") for _, _, ev in fields.get(key, [])[:2]]
        business["provenance"] = self.provenance(evidence) or self.provenance([self.registry_anchor()])
        self.business_conflicts(fields)

        modules: JsonObj = {}
        catalog_id = "menu" if self.template == "bakery" else "services"
        catalog = self.catalog(catalog_id)
        if catalog:
            modules[catalog_id] = catalog
        knowledge = self.knowledge("info")
        if knowledge:
            modules["info"] = knowledge
        owner = self.owner_module(fields)
        modules["owner"] = owner
        self.transactional(modules, catalog_id, locations)

        self.hours_conflicts(locations)
        self.zone_conflicts()
        self.deterministic_policy_conflicts()
        if review is not None:
            self.apply_review(review(self.review_facts()))
        self.missing_checklist(modules, locations)
        self.gap_questions(modules)
        self.confirm_questions(modules, catalog_id)

        sources = self.sources()
        profile: JsonObj = {
            "schema_version": "1.0",
            "profile_id": slug(urlsplit(self.home).hostname.split(".")[0] if urlsplit(self.home).hostname else name),
            "revision": 1,
            "updated_at": self.updated_at,
            "template": {"id": self.template, "version": "1.0"},
            "business": business,
            "modules": modules,
            "sources": sources,
            "open_questions": self.questions,
            "certification": {"status": "draft", "runs": []},
        }
        if self.vocab:
            profile["vocabulary"] = {"attributes": self.vocab}
        return Result(profile=profile, questions=self.questions, report={"dropped_facts": self.dropped})

    def timezone(self, given: str | None, region: str) -> str:
        for candidate in (given, US_STATE_TZ.get(region.upper())):
            if candidate and re.fullmatch(r"[A-Z][A-Za-z_]+(/[A-Za-z0-9_+-]+){1,2}", candidate):
                try:
                    ZoneInfo(candidate)
                    return candidate
                except (ZoneInfoNotFoundError, ValueError):
                    continue
        return "UTC"

    def offer_kinds(self) -> set[str]:
        kinds = set()
        for page, rec in self.records("offers"):
            kind = str(rec.get("kind") or "")
            if kind and self.evidence(page, rec, f"offer.{kind}"):
                kinds.add(kind)
        return kinds

    def offer_evidence(self, kind: str) -> list[JsonObj]:
        out = []
        for page, rec in self.records("offers"):
            if rec.get("kind") == kind and (ev := self.indexes[page.url].locate(rec.get("quote"))):
                out.append({"url": page.url, "quote": ev})
        return out

    # ------------------------------------------------------------------ locations and hours

    def locations(self, fields: dict[str, list[tuple[Page, str, JsonObj]]], country: str) -> list[JsonObj]:
        def first(key: str) -> tuple[str | None, list[JsonObj]]:
            values = fields.get(key) or []
            return (values[0][1], [ev for _, _, ev in values[:2]]) if values else (None, [])

        street, ev_street = first("street")
        locality, ev_loc = first("locality")
        region, _ = first("region")
        postal, _ = first("postal_code")
        locations: list[JsonObj] = []
        self.main_id: str | None = None
        if street and locality:
            address: JsonObj = {"line1": clean_text(street), "locality": clean_text(locality), "country": country}
            if region:
                address["region"] = clean_text(region)
            if postal:
                address["postal_code"] = str(postal)[:16]
            main: JsonObj = {"id": "main", "kind": "storefront", "name": clean_text(fields["name"][0][1]) if fields.get("name") else None}
            main = {k: v for k, v in main.items() if v}
            main["address"] = address
            hours = self.hours()
            if hours:
                main["hours"] = hours
            main["provenance"] = self.provenance(ev_street + ev_loc)
            locations.append(main)
            self.main_id = "main"
        locations.extend(self.service_areas())
        if not locations:
            locations.append({"id": "online", "kind": "virtual", "provenance": self.provenance([self.registry_anchor()])})
        return locations

    def hours(self) -> JsonObj | None:
        per_source: dict[str, dict[str, tuple[str, str]]] = OrderedDict()
        evidence: list[JsonObj] = []
        quotes: dict[tuple[str, str], str] = {}
        for page, rec in self.records("hours"):
            days, opens, closes = as_days(rec.get("days")), as_time(rec.get("open")), as_time(rec.get("close"))
            if not days or not opens or not closes:
                continue
            ev = self.evidence(page, rec, "hours")
            if ev is None:
                continue
            evidence.append(ev)
            table = per_source.setdefault(page.url, {})
            for day in days:
                table.setdefault(day, (opens, closes))
                quotes.setdefault((page.url, day), ev["quote"])
        for page, rec in self.records("closed_days"):
            if (ev := self.evidence(page, rec, "closed_days")) is not None:
                evidence.append(ev)
        if not per_source:
            return None
        # The most complete timetable is the base; days it lacks come from the other pages.
        ranked = sorted(per_source.items(), key=lambda kv: len(kv[1]), reverse=True)
        week: dict[str, tuple[str, str]] = {}
        for _, table in ranked:
            for day, span in table.items():
                week.setdefault(day, span)
        for url, table in per_source.items():
            page = self.by_url[url]
            for day, (o, c) in table.items():
                self.add_fact(page, f"hours:{day}", "hours", f"{o}-{c}", quotes[(url, day)],
                              ["locations/main#hours.weekly"], f"{DAY_NAMES[day]} hours")
        groups: dict[tuple[str, str], list[str]] = OrderedDict()
        for day in DAYS:
            if day in week:
                groups.setdefault(week[day], []).append(day)
        weekly = [{"days": days, "open": o, "close": c} for (o, c), days in groups.items()]
        hours: JsonObj = {"weekly": weekly}
        closures = self.closures()
        if closures:
            hours["closures"] = closures[0]
            evidence += closures[1]
        hours["provenance"] = self.provenance(evidence)
        return hours

    def closures(self) -> tuple[list[JsonObj], list[JsonObj]] | None:
        out: list[JsonObj] = []
        evidence: list[JsonObj] = []
        for page, rec in self.records("closures"):
            start = as_date(rec.get("from"))
            if not start:
                continue
            ev = self.evidence(page, rec, "closure")
            if ev is None:
                continue
            closure: JsonObj = {"from": start}
            end = as_date(rec.get("to"))
            if end and end > start:
                closure["to"] = end
            if reason := spoken_name(rec.get("name")):
                closure["reason_spoken"] = reason
            if all(c["from"] != start for c in out):
                out.append(closure)
                evidence.append(ev)
        return (out[:50], evidence) if out else None

    def service_areas(self) -> list[JsonObj]:
        zones: dict[str, JsonObj] = OrderedDict()
        self.zone_of: dict[str, str] = {}
        for page, rec in self.records("service_area"):
            zone = str(rec.get("zone") or "core").lower()
            ev = self.evidence(page, rec, "service_area")
            if ev is None:
                continue
            postal = [str(p).strip()[:16] for p in rec.get("postal_codes") or [] if str(p).strip()]
            places = [n for n in (spoken_name(x) for x in rec.get("localities") or []) if n]
            for code in postal:
                self.add_fact(page, f"zone:{code}", "zone", zone, ev["quote"], [], f"service zone of {code}")
            for place in places:
                self.add_fact(page, f"zone:{place.lower()}", "zone", zone, ev["quote"], [], f"service zone of {place}")
            if zone == "not_served":
                continue
            loc_id = "core-area" if zone == "core" else ("extended-area" if zone == "extended" else f"{slug(zone)}-area")
            entry = zones.setdefault(loc_id, {"postal_codes": [], "localities": [], "evidence": []})
            entry["postal_codes"] += [c for c in postal if c not in entry["postal_codes"]]
            entry["localities"] += [n for n in places if n not in entry["localities"]]
            entry["evidence"].append(ev)
        out = []
        for loc_id, entry in zones.items():
            area: JsonObj = {}
            if entry["postal_codes"]:
                area["postal_codes"] = entry["postal_codes"]
            if entry["localities"]:
                area["localities"] = entry["localities"]
            if not area:
                continue
            for code in entry["postal_codes"]:
                self.zone_of.setdefault(code, loc_id)
            for place in entry["localities"]:
                self.zone_of.setdefault(place.lower(), loc_id)
            out.append({"id": loc_id, "kind": "service_area", "area": area, "provenance": self.provenance(entry["evidence"])})
        return out

    # ------------------------------------------------------------------ catalog

    def catalog(self, module_id: str) -> JsonObj | None:
        items: dict[str, JsonObj] = OrderedDict()
        categories: dict[str, JsonObj] = OrderedDict()
        self.safety_items: dict[str, list[tuple[str, JsonObj, JsonObj]]] = OrderedDict()  # url -> [(ref, value, ev)]
        self.made_to_order: list[str] = []
        self.item_meta: dict[str, JsonObj] = {}
        for page, rec in self.records("items"):
            name = clean_text(rec.get("name"))
            if not name:
                continue
            ev = self.evidence(page, rec, f"item {name}")
            if ev is None:
                continue
            item_id = slug(name)
            price = self.price(rec)
            ref = f"{module_id}/{item_id}"
            if price is not None:
                self.add_fact(page, f"price:{item_id}", "price", price_label(price), ev["quote"], [f"{ref}#price"], f"price of {name}")
            if item_id in items:
                prov = items[item_id]["provenance"]
                if ev not in prov["evidence"] and len(prov["evidence"]) < 10:
                    prov["evidence"].append(ev)
                if price is not None and "price" not in items[item_id]:
                    items[item_id]["price"] = price
            else:
                item: JsonObj = {"id": item_id, "name": name, "spoken_name": spoken_name(name) or item_id}
                desc = spoken(rec.get("description"))
                if desc:
                    item["description_spoken"] = desc
                cat = clean_text(rec.get("category"))
                if cat:
                    cat_id = slug(cat)
                    categories.setdefault(cat_id, {"id": cat_id, "name": cat, "spoken_name": spoken_name(cat) or cat_id})
                    item["category_ids"] = [cat_id]
                if price is not None:
                    item["price"] = price
                duration = as_int(rec.get("duration_minutes"))
                if duration:
                    item["duration_minutes"] = duration
                if rec.get("made_to_order") is True:
                    item["availability"] = {"status": "made_to_order"}
                    self.made_to_order.append(ref)
                item["provenance"] = {"owner_confirmed": False, "evidence": [ev]}
                items[item_id] = item
                self.item_meta[item_id] = {"category": (cat or "").lower(), "name": name.lower()}
            allergens = sorted({ALLERGEN_ALIASES.get(a, a) for a in map(snake, rec.get("allergens") or [])} & ALLERGENS)
            dietary = sorted(set(map(snake, rec.get("dietary") or [])) & DIETARY)
            bucket = self.safety_items.setdefault(page.url, [])
            if allergens:
                bucket.append((f"{ref}#allergens", {"contains": allergens}, ev))
            if dietary:
                bucket.append((f"{ref}#dietary", {"tags": dietary}, ev))
        if not items:
            return None
        module: JsonObj = {"type": "catalog", "title": "Menu" if module_id == "menu" else "Services"}
        if categories:
            module["categories"] = list(categories.values())
        module["items"] = list(items.values())[:500]
        return module

    def price(self, rec: JsonObj) -> JsonObj | None:
        kind = str(rec.get("price_kind") or "").lower()
        amount, top = as_int(rec.get("amount_cents")), as_int(rec.get("max_cents"))
        if kind not in PRICE_KINDS:
            kind = "fixed" if amount is not None else ""
        if not kind:
            return None
        price: JsonObj = {"kind": kind}
        if kind in ("fixed", "from"):
            if amount is None:
                return None
            price.update(amount_cents=amount, currency=self.currency)
        elif kind == "range":
            if amount is None or top is None or top < amount:
                return None
            price.update(amount_cents=amount, max_cents=top, currency=self.currency)
        per = rec.get("per")
        if isinstance(per, str) and kind != "free" and (k := snake(per, "")) and k not in ("null", "none"):
            price["per"] = k
        return price

    # ------------------------------------------------------------------ knowledge

    def knowledge(self, module_id: str) -> JsonObj | None:
        entries: dict[str, JsonObj] = OrderedDict()
        self.safety_entries: list[tuple[str, JsonObj]] = []
        self.policy_facts: dict[str, tuple[Any, list[JsonObj], str]] = {}  # key -> (value, evidence, entry ref)
        for page, rec in self.records("policies"):
            ev = self.evidence(page, rec, "policy")
            if ev is None:
                continue
            answer = spoken(rec.get("answer"))
            if not answer:
                continue
            topic = snake(rec.get("topic"))
            entry_id = slug(topic)
            facts: JsonObj = {}
            for f in rec.get("facts") or []:
                if isinstance(f, dict) and (key := snake(f.get("key"), "")):
                    value = fact_value(f.get("value"))
                    if value is not None and value != "":
                        facts[key] = value
            ref = f"{module_id}/{entry_id}"
            for key, value in facts.items():
                self.add_fact(page, f"policy:{topic}:{key}", key, value, ev["quote"],
                              [f"{ref}#facts.{key}", f"{ref}#answer_spoken"], f"{topic.replace('_', ' ')}: {key.replace('_', ' ')}")
                self.policy_facts.setdefault(key, (value, [ev], ref))
            question = clean_text(rec.get("question"))
            if entry_id in entries:
                entry = entries[entry_id]
                for key, value in facts.items():
                    entry.setdefault("facts", {}).setdefault(key, value)
                if ev not in entry["provenance"]["evidence"] and len(entry["provenance"]["evidence"]) < 10:
                    entry["provenance"]["evidence"].append(ev)
                if question and question not in entry["question_variants"] and len(entry["question_variants"]) < 8 and len(question) >= 3:
                    entry["question_variants"].append(question)
                if answer != entry["answer_spoken"]:
                    card = f"{entry.get('answer_card', entry['answer_spoken'])} {answer}"
                    entry["answer_card"] = card[:1000]
            else:
                entry = {
                    "id": entry_id,
                    "topic": topic,
                    "question_variants": [question if question and len(question) >= 3 else f"what about {topic.replace('_', ' ')}"],
                    "answer_spoken": answer,
                }
                if facts:
                    entry["facts"] = facts
                entry["provenance"] = {"owner_confirmed": False, "evidence": [ev]}
                entries[entry_id] = entry
            if rec.get("safety") is True and all(r != ref for r, _ in self.safety_entries):
                self.safety_entries.append((ref, ev))
        if not entries:
            return None
        for entry in entries.values():
            for key, value in (entry.get("facts") or {}).items():
                self.declare(key, value)
        return {"type": "knowledge", "title": "Policies and FAQ", "entries": list(entries.values())[:100]}

    def declare(self, key: str, value: Any) -> None:
        if key in self.vocab:
            return
        kind = "boolean" if isinstance(value, bool) else "integer" if isinstance(value, int) else "number" if isinstance(value, float) else "text"
        claim = "price" if key.endswith("_cents") else "policy"
        self.vocab[key] = {"label": key.replace("_", " ")[:120], "type": kind, "claim_class": claim}

    # ------------------------------------------------------------------ owner and transactional modules

    def owner_module(self, fields: dict[str, list[tuple[Page, str, JsonObj]]]) -> JsonObj:
        owner_name = fields["owner_name"][0][1] if fields.get("owner_name") else None
        owner_ev = [ev for _, _, ev in fields.get("owner_name", [])[:2]]
        role = fields["owner_role"][0][1] if fields.get("owner_role") else None
        for page, rec in self.records("people"):
            if "owner" in str(rec.get("role") or "").lower() and (ev := self.evidence(page, rec, "people")):
                owner_name = owner_name or clean_text(rec.get("name"))
                role = role or clean_text(rec.get("role"))
                owner_ev.append(ev)
        first_name = (owner_name or "").split()[0] if owner_name else None
        approver: JsonObj = {"spoken_name": spoken_name(first_name) or "the owner"}
        if role:
            approver["role"] = clean_text(role)
        response = next((self.policy_facts[k] for k in RESPONSE_KEYS if k in getattr(self, "policy_facts", {})), None)
        minutes = as_int(response[0], 1) if response else None
        self.response_known = minutes is not None
        if minutes is None:
            minutes = DEFAULT_RESPONSE_MINUTES
        evidence = owner_ev + (response[1] if response and self.response_known else [])
        if not evidence:
            evidence = [self.registry_anchor()]
        return {
            "type": "owner_approval",
            "title": f"{approver['spoken_name']}'s approvals"[:120],
            "approver": approver,
            "channel": "owner_web",
            "expected_response_minutes": minutes,
            "expires_after_minutes": minutes * 2,
            "on_expiry": "notify_customer",
            "answers_unknown_questions": True,
            "provenance": self.provenance(evidence),
        }

    def registry_anchor(self) -> JsonObj:
        """Evidence for a setting the site does not state: the home page's name (or its first words)."""
        index = self.indexes[self.home]
        name = re.split(r"\s[|·–—-]\s", self.pages[0].title)[0].strip()
        return {"url": self.home, "quote": index.locate(name) or index.opening()}

    def transactional(self, modules: JsonObj, catalog_id: str, locations: list[JsonObj]) -> None:
        kinds = self.offer_kinds()
        catalog = modules.get(catalog_id)
        item_refs = [f"{catalog_id}/{i['id']}" for i in (catalog or {}).get("items", [])]
        contact_fields = [
            {"key": "customer_name", "label": "Name", "type": "text", "required": True, "max_length": 80, "pii": True},
            {"key": "customer_phone", "label": "Phone number", "type": "phone", "required": True, "pii": True},
        ]
        approval = {"module": "owner", "mode": "always", "decisions": ["approve", "decline", "counter_offer", "request_info"]}
        base_ev = [ev for k in ("custom_orders", "appointments", "quote_requests") for ev in self.offer_evidence(k)]

        if "custom_orders" in kinds or (self.template == "bakery" and self.made_to_order):
            refs = self.made_to_order or [r for r in item_refs if re.search(r"cake|custom|order", r)] or item_refs[:1]
            if refs:
                availability: JsonObj = {}
                ev = self.offer_evidence("custom_orders")
                for key, target in (("notice_minutes", "lead_time_minutes"), ("lead_time_minutes", "lead_time_minutes"), ("capacity_per_day", "capacity_per_day")):
                    if key in self.policy_facts and target not in availability and (v := as_int(self.policy_facts[key][0], 1)):
                        availability[target] = v
                        ev += self.policy_facts[key][1]
                availability["provenance"] = self.provenance(ev) or self.provenance([self.registry_anchor()])
                module: JsonObj = {
                    "type": "custom_order",
                    "title": "Custom orders",
                    "items": refs[:50],
                    "intake_fields": [
                        {"key": "pickup_date", "label": "Pickup date", "spoken_prompt": "What day would you like to pick it up?", "type": "date", "required": True},
                        {"key": "details", "label": "Order details", "spoken_prompt": "Tell me what you'd like, including size and flavor.", "type": "long_text", "required": True, "max_length": 1000},
                        *contact_fields,
                    ],
                    "confirmation": {"before_commit": True, "idempotency": "required", "read_back": ["item", "pickup_date", "details"]},
                    "availability": availability,
                    "approval": approval,
                }
                if self.main_id:
                    module["fulfillment"] = {"modes": ["pickup"], "location_ids": [self.main_id],
                                             "provenance": availability["provenance"]}
                modules["custom-orders"] = module

        if "appointments" in kinds or self.template == "salon":
            services = [{"item": f"{catalog_id}/{i['id']}"} for i in (catalog or {}).get("items", []) if i.get("duration_minutes")]
            services = services or [{"item": r} for r in item_refs]
            if services:
                step = as_int(self.policy_facts.get("step_minutes", (None,))[0], 5) or 15
                step = min(max(step, 5), 480)
                ev = self.offer_evidence("appointments") or base_ev or [self.registry_anchor()]
                availability = {"provenance": self.provenance(ev)}
                if v := as_int(self.policy_facts.get("max_advance_minutes", (None,))[0], 0):
                    availability["max_advance_minutes"] = v
                module = {
                    "type": "booking",
                    "title": "Appointments",
                    "services": services,
                    "slotting": {"mode": "interval", "step_minutes": step},
                    "intake_fields": [
                        *contact_fields,
                        {"key": "customer_email", "label": "Email", "type": "email", "required": False, "pii": True},
                    ],
                    "confirmation": {"before_commit": True, "idempotency": "required", "read_back": ["item", "when", "customer_name"]},
                    "availability": availability,
                }
                resources = self.resources()
                if resources:
                    module["resources"] = resources
                if self.main_id:
                    module["fulfillment"] = {"modes": ["on_site"], "location_ids": [self.main_id], "provenance": availability["provenance"]}
                modules["appointments"] = module

        if "quote_requests" in kinds or self.template == "home_services":
            if item_refs:
                areas = [loc["id"] for loc in locations if loc["kind"] == "service_area"]
                ev = self.offer_evidence("quote_requests") or base_ev or [self.registry_anchor()]
                module = {
                    "type": "quote_request",
                    "title": "Quote requests",
                    "items": item_refs[:50],
                    "intake_fields": [
                        {"key": "description", "label": "Describe the job", "spoken_prompt": "What would you like done, and where?", "type": "long_text", "required": True, "max_length": 2000},
                        {"key": "zip", "label": "ZIP code", "spoken_prompt": "What's the ZIP code where the work is?", "type": "text", "required": True, "max_length": 10},
                        *contact_fields,
                    ],
                    "confirmation": {"before_commit": True, "idempotency": "required", "read_back": ["item", "description", "zip"]},
                    "approval": {"module": "owner", "mode": "always", "decisions": ["quote", "request_info", "decline"]},
                    "provenance": self.provenance(ev),
                }
                if areas:
                    module["fulfillment"] = {"modes": ["at_customer"], "location_ids": areas, "provenance": self.provenance(ev)}
                modules["quotes"] = module

    def resources(self) -> list[JsonObj]:
        out: dict[str, JsonObj] = OrderedDict()
        for page, rec in self.records("people"):
            name = spoken_name(rec.get("name"))
            if not name:
                continue
            ev = self.evidence(page, rec, "people")
            if ev is None:
                continue
            rid = slug(name.split()[0])
            if rid in out:
                if ev not in out[rid]["provenance"]["evidence"] and len(out[rid]["provenance"]["evidence"]) < 10:
                    out[rid]["provenance"]["evidence"].append(ev)
                continue
            out[rid] = {"id": rid, "kind": "staff", "spoken_name": name.split()[0], "provenance": {"owner_confirmed": False, "evidence": [ev]}}
        return list(out.values())

    # ------------------------------------------------------------------ questions

    def add_question(self, question: JsonObj) -> None:
        base = question["id"]
        n = 2
        while any(q["id"] == question["id"] for q in self.questions):
            question["id"] = f"{base}-{n}"[:64]
            n += 1
        self.questions.append(question)

    def targeted(self, refs: Iterable[str]) -> bool:
        refs = set(refs)
        return any(refs & set(q["targets"]) for q in self.questions if q["kind"] == "contradiction")

    def contradiction(self, facts: list[Fact], prompt: str, why: str, qid: str, value_key: str) -> bool:
        """One blocking question from facts on different pages with different values (invariant L10)."""
        chosen: list[Fact] = []
        for fact in facts:
            if all(f.source_url != fact.source_url and f.value != fact.value for f in chosen):
                chosen.append(fact)
        if len(chosen) < 2:
            return False
        targets = list(OrderedDict.fromkeys(t for f in chosen for t in f.targets))
        if not targets or self.targeted(targets):
            return False
        candidates = [
            {
                "value": f.value,
                "label": clean_text(f"{human(f.value, value_key)}, as your {self.page_label(f.source_url)} page says") or "",
                "evidence": {"url": f.source_url, "quote": f.quote},
            }
            for f in chosen[:6]
        ]
        options = [{"value": str(c["value"])[:64], "label": clean_text(human(c["value"], value_key)) or "value"} for c in candidates]
        if len({o["value"] for o in options}) != len(options):
            options = [{"value": f"option-{i + 1}", "label": o["label"]} for i, o in enumerate(options)]
        self.add_question({
            "id": qid[:64],
            "kind": "contradiction",
            "targets": targets,
            "prompt": prompt[:300],
            "why": why[:300],
            "candidates": candidates,
            "answer_field": {"key": snake(value_key, "answer"), "label": clean_text(prompt, 120) or "Answer", "type": "choice", "required": True, "options": options},
            "blocking": True,
            "status": "open",
        })
        return True

    def by_subject(self, prefix: str) -> dict[str, list[Fact]]:
        groups: dict[str, list[Fact]] = OrderedDict()
        for fact in self.registry:
            if fact.subject.startswith(prefix):
                groups.setdefault(fact.subject, []).append(fact)
        return groups

    def hours_conflicts(self, locations: list[JsonObj]) -> None:
        if not self.main_id:
            return
        for subject, facts in self.by_subject("hours:").items():
            values = {f.value for f in facts}
            if len(values) < 2:
                continue
            day = DAY_NAMES[subject.split(":")[1]]
            opens = {v.split("-")[0] for v in values}
            if len(opens) == 1:
                # Only the closing time differs: ask about that, with closing times as the values.
                close_facts = [Fact(f.id, f.source_url, f.page_title, f.subject, "close", f.value.split("-")[1], f.quote, f.targets, f.describe) for f in facts]
                described = " but ".join(
                    f"your {self.page_label(f.source_url)} page says {clock12(f.value)}" for f in self.distinct(close_facts)
                )
                self.contradiction(close_facts, f"What time do you close on {day}s?",
                                   f"For {day} closing time, {described}. Until you answer, the assistant won't tell customers a {day} closing time.",
                                   f"q-{day.lower()}-close", "close_time")
            else:
                described = " but ".join(f"your {self.page_label(f.source_url)} page says {f.value}" for f in self.distinct(facts))
                self.contradiction(facts, f"What are your hours on {day}s?",
                                   f"For {day} hours, {described}. Until you answer, the assistant won't give customers {day} hours.",
                                   f"q-{day.lower()}-hours", "hours")

    @staticmethod
    def distinct(facts: list[Fact]) -> list[Fact]:
        out: list[Fact] = []
        for f in facts:
            if all(o.source_url != f.source_url and o.value != f.value for o in out):
                out.append(f)
        return out

    def business_conflicts(self, fields: dict[str, list[tuple[Page, str, JsonObj]]]) -> None:
        for name, target in (("phone", "business#contact.phone"), ("email", "business#contact.email")):
            norm = (lambda v: re.sub(r"\D", "", v)) if name == "phone" else str.lower
            for page, value, ev in fields.get(name, []):
                self.add_fact(page, f"business:{name}", name, norm(value), ev["quote"], [target], f"business {name}")

    def zone_conflicts(self) -> None:
        for subject, facts in self.by_subject("zone:").items():
            place = subject.split(":", 1)[1]
            loc = self.zone_of.get(place)
            if not loc:
                continue
            for f in facts:
                f.targets = [f"locations/{loc}#area.postal_codes" if re.fullmatch(r"[0-9A-Za-z -]{3,10}", place) and any(ch.isdigit() for ch in place) else f"locations/{loc}#area.localities"]
            facts_d = self.distinct(facts)
            if len(facts_d) < 2:
                continue
            described = " but ".join(f"your {self.page_label(f.source_url)} page says {f.value.replace('_', ' ')}" for f in facts_d)
            self.contradiction(facts, f"Is {place} in your core area, your extended area, or not served?",
                               f"For {place}, {described}. Until you answer, the assistant won't tell customers whether you serve it.",
                               f"q-zone-{slug(place)}", "zone")

    def deterministic_policy_conflicts(self) -> None:
        for prefix in ("policy:", "price:", "business:"):
            for subject, facts in self.by_subject(prefix).items():
                facts_d = self.distinct(facts)
                if len(facts_d) < 2:
                    continue
                what = facts_d[0].describe
                described = " but ".join(f"your {self.page_label(f.source_url)} page says {human(f.value, f.key)}" for f in facts_d)
                self.contradiction(facts, f"Which is right for {what}?"[:300],
                                   f"For {what}, {described}. Until you answer, the assistant won't state it.",
                                   f"q-{slug(subject.replace(':', '-'))}", facts_d[0].key)

    def review_facts(self) -> list[JsonObj]:
        """Compact fact list for the model's cross-page contradiction review."""
        return [
            {"id": f.id, "page": self.page_label(f.source_url), "fact": f.describe, "value": f.value, "quote": f.quote[:200]}
            for f in self.registry
            if f.targets
        ]

    def apply_review(self, found: list[JsonObj]) -> None:
        by_id = {f.id: f for f in self.registry}
        for item in found:
            ids = [i for i in item.get("fact_ids") or [] if isinstance(i, str) and i in by_id]
            facts = [by_id[i] for i in ids]
            if len(facts) < 2:
                continue
            prompt = clean_text(item.get("prompt"), 300) or f"Which is right for {facts[0].describe}?"
            why = clean_text(item.get("why"), 260) or "Two pages of your website disagree."
            if not why.endswith("."):
                why += "."
            why = (why + " Until you answer, the assistant won't state it.")[:300]
            # Values need a common key for labels; fall back to the first fact's.
            self.contradiction(facts, prompt, why, f"q-{slug(facts[0].describe)}", facts[0].key)

    def missing(self, qid: str, target: str, prompt: str, why: str, field: JsonObj, blocking: bool = True) -> None:
        if target != "info#entries" and any(target in q["targets"] for q in self.questions):
            return
        self.add_question({"id": qid, "kind": "missing", "targets": [target], "prompt": prompt[:300], "why": why[:300],
                           "answer_field": field, "blocking": blocking, "status": "open"})

    def missing_checklist(self, modules: JsonObj, locations: list[JsonObj]) -> None:
        approver = modules["owner"]["approver"]["spoken_name"]
        if not self.response_known:
            self.missing("q-owner-response-time", "owner#expected_response_minutes",
                         "How soon do you usually reply when the assistant sends you a request?",
                         f"Your website never says how quickly {approver} answers. The assistant tells customers when to expect a reply, so it needs a real number.",
                         {"key": "response_minutes", "label": "Usual reply time in minutes", "type": "integer", "required": True, "min": 1})
        co = modules.get("custom-orders")
        if co is not None:
            if "capacity_per_day" not in co["availability"]:
                self.missing("q-custom-order-capacity", "custom-orders#availability.capacity_per_day",
                             "How many custom orders can you take for one day?",
                             "Your website does not give a daily limit for custom orders. The assistant needs it so it never accepts more orders than you can make.",
                             {"key": "orders_per_day", "label": "Custom orders per day", "type": "integer", "required": True, "min": 1})
            if "lead_time_minutes" not in co["availability"]:
                self.missing("q-custom-order-notice", "custom-orders#availability.lead_time_minutes",
                             "How much notice do you need for a custom order?",
                             "Your website does not say how far ahead customers must order.",
                             {"key": "notice_hours", "label": "Notice needed, in hours", "type": "integer", "required": True, "min": 0})
        if self.main_id and not any(loc.get("hours") for loc in locations):
            self.missing("q-opening-hours", "locations/main#hours", "What are your opening hours?",
                         "Your website does not list opening hours.",
                         {"key": "opening_hours", "label": "Opening hours", "type": "long_text", "required": True, "max_length": 500})
        if "appointments" in modules and "info" in modules:
            topics = {e["topic"] for e in modules["info"]["entries"]}
            if not topics & {"walk_ins", "walk_in", "walkins"}:
                self.missing("q-walk-ins", "info#entries", "Do you take walk-ins? If so, for which services or times?",
                             "Your website does not say whether walk-ins are welcome, and customers often ask.",
                             {"key": "walk_ins", "label": "Walk-in policy", "type": "text", "required": True, "max_length": 300},
                             blocking=False)

    def gap_questions(self, modules: JsonObj) -> None:
        targets = {
            "capacity_per_day": "custom-orders#availability.capacity_per_day" if "custom-orders" in modules else None,
            "lead_time_minutes": "custom-orders#availability.lead_time_minutes" if "custom-orders" in modules else None,
            "owner_reply_minutes": "owner#expected_response_minutes",
            "quote_response_minutes": "owner#expected_response_minutes",
        }
        for page, rec in self.records("gaps"):
            key = snake(rec.get("key"), "")
            question = clean_text(rec.get("question"), 300)
            if not key or not question:
                continue
            ev = self.evidence(page, rec, f"gap {key}")
            if ev is None:
                continue
            target = targets.get(key)
            if key in RESPONSE_KEYS and self.response_known:
                continue
            if target is None:
                if "info" not in modules:
                    continue
                target = "info#entries"
            why = clean_text(rec.get("why"), 200) or "Your website hints at this but never says it."
            why = f'{why.rstrip(".")}. Your {self.page_label(page.url)} page says: "{ev["quote"][:80]}"'[:300]
            existing = next((q for q in self.questions if target in q["targets"] and q["kind"] == "missing"), None)
            if existing is not None and target != "info#entries":
                existing["why"] = why
                continue
            if any(q["id"] == f"q-{slug(key)}" for q in self.questions):
                continue
            field_type = "integer" if key.endswith(("_minutes", "_per_day", "_cents", "_percent", "_days")) else "text"
            answer = {"key": key, "label": clean_text(key.replace("_", " ").capitalize()) or "Answer", "type": field_type, "required": True}
            if field_type == "integer":
                answer["min"] = 0
            else:
                answer["max_length"] = 300
            self.missing(f"q-{slug(key)}", target, question, why, answer, blocking=target != "info#entries")

    def confirm_questions(self, modules: JsonObj, catalog_id: str) -> None:
        if catalog_id in modules:
            candidates, targets = [], []
            for url, rows in self.safety_items.items():
                if not rows or len(candidates) >= 6:
                    continue
                value = {ref: v for ref, v, _ in rows}
                targets += [ref for ref in value if ref not in targets]
                candidates.append({"value": value, "label": clean_text(f"As listed on your {self.page_label(url)} page") or "As listed",
                                   "evidence": rows[0][2]})
            if candidates:
                self.add_question({
                    "id": "q-allergens",
                    "kind": "confirm",
                    "targets": targets,
                    "prompt": "Your website lists what items contain and which are vegan, gluten-free and so on. Is all of it correct?",
                    "why": "Allergen and diet answers are safety facts, so the assistant only says them after you confirm them.",
                    "candidates": candidates,
                    "answer_field": {"key": "allergens_correct", "label": "Allergen and diet information is correct", "type": "boolean", "required": True},
                    "blocking": True,
                    "status": "open",
                })
        if self.safety_entries:
            refs = [ref for ref, _ in self.safety_entries]
            self.add_question({
                "id": "q-safety-policies",
                "kind": "confirm",
                "targets": [f"{ref}#answer_spoken" for ref in refs],
                "prompt": "Your website makes some health and safety statements. Are they all correct?",
                "why": "Safety statements are only repeated to customers after you confirm them.",
                "candidates": [{"value": ref, "label": clean_text(f"As your {self.page_label(ev['url'])} page says") or "As written", "evidence": ev}
                               for ref, ev in self.safety_entries[:6]],
                "answer_field": {"key": "safety_correct", "label": "Safety statements are correct", "type": "boolean", "required": True},
                "blocking": True,
                "status": "open",
            })

    # ------------------------------------------------------------------ sources

    def sources(self) -> JsonObj:
        out: JsonObj = {}
        for page in self.pages:
            key = f"web-{page.path_key}"[:64]
            source: JsonObj = {"kind": "web_page", "url": page.url, "title": clean_text(page.title) or page.url[:120],
                               "retrieved_at": page.retrieved_at, "content_sha256": page.sha256}
            canaries = self.canaries.get(page.url) or []
            if canaries:
                source["injection_canaries"] = [{"quote": c.quote, "reason": c.reason} for c in canaries]
            out[key] = source
        return out


def price_label(price: JsonObj) -> str:
    kind = price["kind"]
    if kind in ("quote_only", "free"):
        return kind
    amount = f"${price['amount_cents'] / 100:,.2f}"
    if kind == "range":
        return f"{amount}-${price['max_cents'] / 100:,.2f}"
    return f"from {amount}" if kind == "from" else amount


TEMPLATE_CATEGORY = {"bakery": "bakery", "salon": "hair_salon", "home_services": "home_repair", "generic": "local_business"}


def choose_template(categories: list[str], name: str, offers: set[str]) -> str:
    text = " ".join(categories + [name.lower()])
    if re.search(r"bak|cake|pastr|patisser|cafe|coffee|deli", text):
        return "bakery"
    if re.search(r"salon|hair|barber|spa|nail|beauty|stylist", text):
        return "salon"
    if re.search(r"plumb|handy|repair|electric|hvac|clean|contractor|roof|landscap|home", text):
        return "home_services"
    if "appointments" in offers:
        return "salon"
    return "generic"

