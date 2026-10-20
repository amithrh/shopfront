"""Reduce step: build a schema-valid business profile and the owner's questions from quoted page facts.

Everything here is deterministic. Model output is treated as untrusted input: every value is
re-validated against the schema's shapes, every quote is re-located on its page (facts without a real
quote are dropped), safety facts (allergens, dietary claims, safety policies) are never written as
statable facts but turned into `confirm` questions, and the same fact read differently on two pages
becomes a blocking `contradiction` question instead of a guess.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from askonce_onboarding.catalog import (
    Product,
    Sighting,
    base_name,
    choice_id,
    compatible,
    contains_product,
    group_label,
    price_ok,
    same_product,
    tail_name,
    words,
)
from askonce_onboarding.extract import PageFacts
from askonce_onboarding.holidays import closure_dates, holiday_name
from askonce_onboarding.injection import Canary
from askonce_onboarding.quotes import QuoteIndex
from askonce_onboarding.site import Page
from askonce_onboarding.structure import diet_labels, parse_allergens, price_alternatives, read_page, singular

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
    text = re.sub(r"(\w)['’](\w)", r"\1\2", str(value or ""))  # "Men's cut" -> "mens-cut"
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
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


FOOTNOTE_WORDS = {"*": "an asterisk", "†": "a dagger", "‡": "a double dagger"}


def spoken(value: Any, limit: int = 240) -> str | None:
    """At most two short sentences, no URLs or markup (the schema's `spoken` pattern)."""
    text = clean_text(value, 10_000)
    if text:
        text = re.sub(r"\bmarked\s*([*†‡])", lambda m: f"marked with {FOOTNOTE_WORDS[m.group(1)]}", text)
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
    if not isinstance(value, str):
        return None
    try:
        day = dt.date.fromisoformat(value.strip())
    except ValueError:
        return None
    return day.isoformat() if 2000 <= day.year <= 2100 else None


COUNTRY_NAMES = {"australia": "AU", "india": "IN", "united states": "US", "usa": "US", "us": "US",
                 "new zealand": "NZ", "united kingdom": "GB", "uk": "GB", "canada": "CA"}
COUNTRY_BY_TLD = {"au": "AU", "in": "IN", "nz": "NZ", "uk": "GB", "ca": "CA", "us": "US"}
CURRENCY_BY_COUNTRY = {"AU": "AUD", "IN": "INR", "NZ": "NZD", "GB": "GBP", "CA": "CAD", "US": "USD"}
AU_STATE_TZ = {"NSW": "Australia/Sydney", "ACT": "Australia/Sydney", "VIC": "Australia/Melbourne", "TAS": "Australia/Hobart",
               "QLD": "Australia/Brisbane", "SA": "Australia/Adelaide", "WA": "Australia/Perth", "NT": "Australia/Darwin"}
COUNTRY_TZ = {"IN": "Asia/Kolkata", "NZ": "Pacific/Auckland", "GB": "Europe/London", "AU": "Australia/Sydney"}
_DIAL = {"US": "1", "CA": "1", "AU": "61", "IN": "91", "NZ": "64", "GB": "44"}


def e164(value: Any, country: str = "US") -> str | None:
    if not isinstance(value, str):
        return None
    digits = re.sub(r"\D", "", value)
    if value.strip().startswith("+") and 7 <= len(digits) <= 15:
        return "+" + digits
    dial = _DIAL.get(country)
    if country in ("AU", "NZ", "GB", "IN") and dial:
        if digits.startswith(dial) and len(digits) >= len(dial) + 8:
            return "+" + digits
        if digits.startswith("0") and 9 <= len(digits) <= 11:
            return f"+{dial}{digits[1:]}"
        if country == "IN" and len(digits) == 10:
            return f"+91{digits}"
        return None
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


_CLOCK = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?![\d%$])", re.I)


def quote_times(quote: str) -> set[str]:
    """Every clock time a quote could mean ("3:00 pm" -> 15:00; a bare "3:00" -> 03:00 or 15:00)."""
    out: set[str] = set()
    lowered = quote.lower()
    if "noon" in lowered:
        out.add("12:00")
    if "midnight" in lowered:
        out |= {"24:00", "00:00"}
    for m in _CLOCK.finditer(quote):
        h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if not m.group(2) and not ampm:
            continue  # a bare number is not a time
        if h > 24 or mi > 59:
            continue
        if ampm.startswith("p") and h < 12:
            h += 12
        elif ampm.startswith("a") and h == 12:
            h = 0
        out.add(f"{h:02d}:{mi:02d}")
        if not ampm and h < 12:
            out.add(f"{h + 12:02d}:{mi:02d}")
    return out


def _names_item(quote: str, name: str) -> bool:
    words = re.findall(r"[a-z]{4,}", name.lower()) or re.findall(r"[a-z0-9]{2,}", name.lower())
    return any(w in quote.lower() for w in words[:4])


def quote_has_amount(quote: str, cents: int) -> bool:
    """The quote prints this amount ($9, $9.00, $1,050.00)."""
    for m in re.finditer(r"(?:\$|₹|\brs\.?|\binr)\s?(\d[\d,]*)(?:\.(\d{2}))?", quote, re.I):
        if int(m.group(1).replace(",", "")) * 100 + int(m.group(2) or 0) == cents:
            return True
    return False


_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty-eight": 48, "seventy-two": 72,
    "twenty-four": 24, "half": 50,
}
_UNIT_FACTORS = (1, 60, 1440, 10080, 43200, 100)


def quote_supports_number(quote: str, value: int) -> bool:
    """Some number printed in the quote, in some unit (minutes, hours, days, weeks, months, cents), is `value`.

    Catches the model's invented numbers (a 24-hour reply time read off a page that names none)."""
    lowered = quote.lower()
    numbers = {int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", lowered)}
    numbers |= {v for w, v in _NUMBER_WORDS.items() if re.search(rf"\b{w}\b", lowered)}
    if re.search(r"\b(a|an|one|per)\s+(hour|day|business day|week|month|year)\b", lowered):
        numbers.add(1)
    return any(n * f == value for n in numbers for f in _UNIT_FACTORS)


_DAY_WORDS = {
    "mon": r"mon(day)?s?", "tue": r"tue(s|sday)?s?", "wed": r"wed(nesday)?s?", "thu": r"thu(r|rs|rsday)?s?",
    "fri": r"fri(day)?s?", "sat": r"sat(urday)?s?", "sun": r"sun(day)?s?",
}
_NOT_OPENING_HOURS = re.compile(r"\b(pick ?-?ups?|picked up|deliver(y|ies|ed)?|phones?|call(s|ing)? us|we answer|office line)\b", re.I)


def quote_days(quote: str) -> set[str]:
    """Weekdays a timetable line names, ranges ("Tuesday to Friday", "Tue-Fri") expanded."""
    found: list[tuple[int, str]] = []
    for day, pattern in _DAY_WORDS.items():
        for m in re.finditer(rf"(?<![a-z]){pattern}(?![a-z])", quote, re.I):
            found.append((m.start(), day))
    found.sort()
    days = {d for _, d in found}
    for (a_pos, a), (b_pos, b) in zip(found, found[1:]):
        between = quote[a_pos:b_pos]
        if re.search(r"(\bto\b|through|thru|[-–—])", between[3:], re.I):
            i, j = DAYS.index(a), DAYS.index(b)
            if i < j:
                days |= set(DAYS[i : j + 1])
    return days


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
        self.structures = {p.url: read_page(p.url, p.html, title=p.title) for p in pages}
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

    def build(self, review: Callable[[list[JsonObj]], list[JsonObj]] | None = None,
              variants: Callable[[str, list[JsonObj]], dict[str, list[str]]] | None = None) -> Result:
        fields = self.business_fields()

        def first(name: str) -> str | None:
            values = fields.get(name) or []
            return values[0][1] if values else None

        name = first("name") or urlsplit(self.home).hostname or "Business"
        region = (first("region") or "").strip()
        country = (first("country") or "").strip().upper()
        postal = first("postal_code")
        country = COUNTRY_NAMES.get(country.lower(), country)
        if len(country) != 2:
            tld = (urlsplit(self.home).hostname or "").rsplit(".", 1)[-1].lower()
            if tld in COUNTRY_BY_TLD:
                country = COUNTRY_BY_TLD[tld]
            elif region.upper() in AU_STATE_TZ:
                country = "AU"
            elif region.upper() in US_STATE_TZ or (postal and re.fullmatch(r"\d{5}", postal)):
                country = "US"
            elif postal and re.fullmatch(r"\d{6}", postal):
                country = "IN"
            else:
                country = ""
        self.country = country
        currency = (first("currency") or "").strip().upper()
        local = CURRENCY_BY_COUNTRY.get(country)
        if not re.fullmatch(r"[A-Z]{3}", currency) or (local and currency == "USD" and local != "USD"):
            currency = local or "USD"  # "$" on an Australian site is AUD
        self.currency = currency
        timezone = self.timezone(first("timezone"), region, country)
        categories = list(OrderedDict.fromkeys(snake(v) for _, v, _ in fields.get("category", []) if snake(v, "")))
        self.template = choose_template(categories, name, self.offer_kinds())
        if not categories:
            categories = [TEMPLATE_CATEGORY[self.template]]

        locations = self.locations(fields, country or "US")
        business: JsonObj = {
            "name": clean_text(name) or "Business",
            "spoken_name": spoken_name(name) or "the business",
            "categories": categories[:8],
            "locale": f"en-{country}" if country else "en-US",
            "timezone": timezone,
            "currency": currency,
            "website": self.website,
            "locations": locations,
        }
        summary = spoken(first("summary"))
        if summary:
            business["summary_spoken"] = summary
        contact: JsonObj = {}
        phones = [(v, ev) for _, v, ev in fields.get("phone", [])]
        main = next((v for v, ev in phones if not re.search(r"\bemergenc|after[- ]hours|24/7", ev["quote"], re.I)), None)
        phone = e164(main or first("phone"), country or "US")
        if phone:
            contact["phone"] = phone
        email = first("email")
        if email and re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", email, re.I):
            contact["email"] = email
        if contact:
            business["contact"] = contact
        evidence = [ev for key in ("name", "summary", "phone", "email") for _, _, ev in fields.get(key, [])[:2]]
        business["provenance"] = self.provenance(evidence) or self.provenance([self.registry_anchor()])

        modules: JsonObj = {}
        catalog_id = "menu" if self.template == "bakery" else "services"
        self.catalog_id = catalog_id
        catalog = self.catalog(catalog_id)
        if catalog:
            modules[catalog_id] = catalog
        knowledge = self.knowledge("info")
        if knowledge:
            if variants is not None:
                self.add_variants(knowledge["entries"], variants(business["name"], knowledge["entries"]), business["name"])
            for e in knowledge["entries"]:
                e.pop("_heading", None)
            modules["info"] = knowledge
        owner = self.owner_module(fields)
        modules["owner"] = owner
        self.transactional(modules, catalog_id, locations)

        self.info_entries = (modules.get("info") or {}).get("entries", [])
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
            "profile_id": slug(re.sub(r"^www\d?\.", "", urlsplit(self.home).hostname or "").split(".")[0] or name),
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

    def timezone(self, given: str | None, region: str, country: str = "US") -> str:
        local = AU_STATE_TZ.get(region.upper()) if country == "AU" else US_STATE_TZ.get(region.upper()) if country == "US" else None
        for candidate in (given, local, COUNTRY_TZ.get(country)):
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
        self.street_line = re.split(r",", street or "")[0].strip()
        self.own_postal, _ = first("postal_code")
        self.own_locality, _ = first("locality")
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
            if not {opens, closes} <= quote_times(ev["quote"]) or _NOT_OPENING_HOURS.search(ev["quote"]):
                self.dropped.append({"page": page.url, "what": "hours (not opening hours in quote)", "quote": ev["quote"][:200]})
                continue
            named = quote_days(ev["quote"])
            if not named or not set(days) <= named:
                # A quote must name the days the model gave it ("Tuesday 10-7" is not Tue-Thu).
                days = [d for d in days if d in named]
                if not days:
                    self.dropped.append({"page": page.url, "what": "hours (days not in quote)", "quote": ev["quote"][:200]})
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
        """Holiday closures: every date a "closed on ..." sentence names, resolved in code (a printed date as
        written, a named holiday as its next date in the business's country), never the model's guess."""
        if hasattr(self, "_closures"):
            return self._closures
        ref = dt.date.fromisoformat(min(p.retrieved_at for p in self.pages)[:10])
        sentences: list[tuple[str, str]] = []
        for page, rec in self.records("closures"):
            if (ev := self.evidence(page, rec, "closure")) is not None:
                sentences.append((page.url, ev["quote"]))
        for page in self.pages:
            for sec in self.structures[page.url].sections:
                for unit in sec["units"]:
                    for sentence in re.split(r"(?<=[.!?])\s+", unit):
                        if re.search(r"\bclosed\b", sentence, re.I):
                            sentences.append((page.url, sentence))
        out: list[JsonObj] = []
        evidence: list[JsonObj] = []
        for url, sentence in sentences:
            if not re.search(r"\bclosed\b", sentence, re.I):
                continue
            dates = closure_dates(sentence, country=self.country or "US", ref=ref)
            if not dates:
                continue
            ev = self.locate_ev(url, sentence)
            if ev is None:
                continue
            for first, last, name in dates:
                if any(c["from"] == first.isoformat() for c in out):
                    continue
                closure: JsonObj = {"from": first.isoformat()}
                if last > first:
                    closure["to"] = last.isoformat()
                name = name or holiday_name(first, self.country or "US")
                if reason := spoken_name(name):
                    closure["reason_spoken"] = reason
                out.append(closure)
                if ev not in evidence:
                    evidence.append(ev)
        out.sort(key=lambda c: c["from"])
        self._closures = (out[:50], evidence) if out else None
        return self._closures

    def service_areas(self) -> list[JsonObj]:
        zones: dict[str, JsonObj] = OrderedDict()
        self.zone_of: dict[str, str] = {}
        street = (getattr(self, "street_line", "") or "").lower()
        for page, rec in self.records("service_area"):
            zone = str(rec.get("zone") or "core").lower()
            ev = self.evidence(page, rec, "service_area")
            if ev is None:
                continue
            if street and street in ev["quote"].lower():
                continue  # the business's own address is where it is, not an area it serves
            postal = list(OrderedDict.fromkeys(str(p).strip()[:16] for p in rec.get("postal_codes") or [] if str(p).strip()))
            places = list(OrderedDict.fromkeys(n for n in (spoken_name(x) for x in rec.get("localities") or []) if n))
            # A code or place the page does not print is not grounded; an "area" that is only the business's own
            # postcode and town ("picked up at the bakery") is its address, not a service area.
            text = self.page_text(page.url)
            postal = [c for c in postal if c in text or postal_in_range(c, text)]
            places = [n for n in places if base_place(n).lower() in text.lower()]
            own = {str(self.own_postal or "").lower(), str(self.own_locality or "").lower()} - {""}
            if not postal and not places or {x.lower() for x in postal + places} <= own:
                continue
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
            loc = {"id": loc_id, "kind": "service_area", "area": area, "provenance": self.provenance(entry["evidence"])}
            if hours := self.work_hours():
                loc["hours"] = hours
            out.append(loc)
        return out

    def work_hours(self) -> JsonObj | None:
        """When jobs are done at the customer's place, if the site gives its own timetable for that (a
        'Scheduled work' column beside the office hours). Holiday closures apply to it too."""
        for s in self.structures.values():
            for t in s.timetables:
                if not WORK_LABEL.search(t["label"] or ""):
                    continue
                ev = self.locate_ev(t["url"], t["quote"])
                if ev is None:
                    continue
                week: dict[str, tuple[str, str]] = {}
                for line in t["lines"]:
                    if "open" in line:
                        for d in line["days"]:
                            week.setdefault(d, (line["open"], line["close"]))
                groups: dict[tuple[str, str], list[str]] = OrderedDict()
                for d in DAYS:
                    if d in week:
                        groups.setdefault(week[d], []).append(d)
                hours: JsonObj = {"weekly": [{"days": ds, "open": o, "close": c} for (o, c), ds in groups.items()]}
                evidence = [ev]
                if closures := self.closures():
                    hours["closures"] = closures[0]
                    evidence += closures[1]
                hours["provenance"] = self.provenance(evidence)
                return hours
        return None

    # ------------------------------------------------------------------ catalog

    def catalog(self, module_id: str) -> JsonObj | None:
        self.safety_items: dict[str, list[tuple[str, JsonObj, JsonObj]]] = OrderedDict()  # url -> [(ref, value, ev)]
        self.made_to_order: list[str] = []
        self.item_meta: dict[str, JsonObj] = {}
        self.item_staff: dict[str, list[str]] = {}
        self.deposit_items: list[tuple[str, JsonObj]] = []  # (ref, evidence of the deposit rule)
        products = self.products()
        items: dict[str, JsonObj] = OrderedDict()
        categories: dict[str, JsonObj] = OrderedDict()
        for prod in products:
            item = self.product_item(prod, module_id, categories)
            if item is None:
                continue
            base, n = item["id"], 2
            while item["id"] in items:
                item["id"] = f"{base}-{n}"[:64]
                n += 1
            items[item["id"]] = item
            self.finish_item(prod, item, module_id)
        self.item_overflow = {}
        for item, rest, url in getattr(self, "item_overflow_pending", []):
            self.item_overflow.setdefault(item["id"], []).append((url, rest))
        if not items:
            return None
        # Categories and items share one id space per module (invariant L2).
        for cat_id in [c for c in categories if c in items]:
            new_id = f"{cat_id}-category"[:64]
            categories[new_id] = {**categories.pop(cat_id), "id": new_id}
            for it in items.values():
                if cat_id in it.get("category_ids", []):
                    it["category_ids"] = [new_id if c == cat_id else c for c in it["category_ids"]]
        used = {c for it in items.values() for c in it.get("category_ids", [])}
        cat_names = {cid: c["name"].lower() for cid, c in categories.items()}
        self.catalog_items = {}
        for it in items.values():
            page = next((u for u, st in self.structures.items() if st.product and same_product(st.product, it["name"])), None)
            self.catalog_items[f"{module_id}/{it['id']}"] = {
                "name": it["name"].lower(), "category": next((cat_names[c] for c in it.get("category_ids", []) if c in cat_names), None),
                "product_page": page}
        self.item_names = [it["name"] for it in items.values()]
        self.catalog_evidence = [ev for it in items.values() for ev in it["provenance"]["evidence"]]
        module: JsonObj = {"type": "catalog", "title": "Menu" if module_id == "menu" else "Services"}
        if categories:
            module["categories"] = [c for cid, c in categories.items() if cid in used]
        module["items"] = list(items.values())[:500]
        return module

    # ------------------------------------------------------------------ catalog: sightings -> products

    def locate_ev(self, page_url: str, quote: str) -> JsonObj | None:
        found = self.indexes[page_url].locate(quote)
        return {"url": page_url, "quote": found} if found else None

    def products(self) -> list[Product]:
        """Every product the site sells, each once, from the pages' structure (then the model's items)."""
        found: list[Product] = []
        structured_pages: set[str] = set()
        self.table_rows: dict[tuple, list[JsonObj]] = {}
        for page in self.pages:
            s = self.structures[page.url]
            own: list[Product] = []
            for rec in s.items:
                if rec.get("source") != "variants":
                    continue
                ev = self.locate_ev(page.url, rec["quote"])
                if ev is None:
                    continue
                prod = Product(rec["name"], [Sighting(page.url, rec, "variants", ev)])
                for og in rec["options"]:
                    prod.groups.append(self.group_from(page.url, og["choices"], og.get("name") or "", rec["name"],
                                                       required=True, addons=False))
                self.split_addons(prod)
                own.append(prod)
            rows: list[Sighting] = []
            for rec in s.items:
                if rec.get("source") not in ("table", "card"):
                    continue
                if re.search(r"%\s*off\b|\bdiscount\b", rec.get("price_text", ""), re.I):
                    continue  # a discount is a policy, not something to buy
                ev = self.locate_ev(page.url, rec["quote"])
                if ev is None:
                    self.dropped.append({"page": page.url, "what": f"item {rec['name']} (quote not on page)", "quote": rec["quote"][:200]})
                    continue
                if rec.get("source") == "table":
                    key = (page.url, tuple(rec.get("headings") or ()), tuple(rec.get("header") or ()), rec.get("caption"))
                    rec["table_key"] = key
                    self.table_rows.setdefault(key, []).append(rec)
                rows.append(Sighting(page.url, rec, rec["source"], ev))
            own += self.prefix_groups(page.url, rows)
            if s.groups:
                name = s.product or page.title
                prod = next((p for p in own if same_product(p.name, name)), None)
                if prod is None:
                    first = s.groups[0]["choices"][0]
                    ev = self.locate_ev(page.url, first["quote"])
                    if ev is not None:
                        prod = Product(name, [Sighting(page.url, {"name": name, "description": "", "source": "product"}, "product", ev)])
                        own.insert(0, prod)
                if prod is not None:
                    for g in s.groups:
                        prod.groups.append(self.group_from(page.url, g["choices"], g["name"], prod.name,
                                                           required=g.get("required"), addons=bool(g.get("addons"))))
                    self.diet_versions(prod)
            if own:
                structured_pages.add(page.url)
            found += own
        order = {"variants": 0, "product": 0, "table": 1, "card": 2, "model": 3}
        found.sort(key=lambda p: order.get(p.primary.source, 9))
        prods = self.merge([], found)
        # The model's items, for pages whose structure lists nothing.
        model: list[Product] = []
        for page, rec in self.records("items"):
            sighting = self.model_sighting(page, rec)  # unquoted readings are reported as dropped either way
            if sighting is None:
                continue
            if page.url in structured_pages:
                if self.match_product(prods, Product(sighting.rec["name"], [sighting])) is None:
                    self.dropped.append({"page": page.url, "what": f"item {sighting.rec['name']} (not in the page's own price list)",
                                         "quote": sighting.ev["quote"][:200]})
                continue
            model.append(Product(sighting.rec["name"], [sighting]))
        prods = self.merge(prods, model)
        return [self.zone_priced(p) for p in self.drop_teasers(prods)]

    def zone_priced(self, prod: Product) -> Product:
        """'Extended-area travel fee $35 per visit — No travel fee in our core area': a fee that depends on the
        service zone is one item with a choice per zone."""
        zones = sorted({loc for loc in getattr(self, "zone_of", {}).values()})
        labels = {z: z.replace("-", " ") for z in zones}
        name = prod.name.lower().replace("-", " ")
        hit = next((z for z, lab in labels.items() if lab in name), None)
        if hit is None or prod.groups:
            return prod
        prim = prod.primary
        price = prim.rec.get("price")
        base = re.sub(rf"\b{re.escape(labels[hit])}\b", "", prod.name.replace("-", " "), flags=re.I).strip(" -,")
        if not price or not base or len(base) < 3:
            return prod
        notes = " ".join(prim.rec.get("notes") or []) + " " + (prim.rec.get("description") or "")
        choices = [{"name": labels[hit][:1].upper() + labels[hit][1:], "price": price, "ev": prim.ev, "id_hint": hit}]
        base_words = words(base)
        for z, lab in labels.items():
            if z == hit:
                continue
            m = re.search(rf"\bno ([a-z -]+?) in (?:our )?{re.escape(lab)}\b", notes, re.I)
            if m and words(m.group(1)) <= base_words | {"fee"}:
                choices.append({"name": lab[:1].upper() + lab[1:], "price": {"kind": "free"}, "ev": prim.ev, "id_hint": z})
        if len(choices) < 2:
            return prod
        for sighting in prod.sightings:
            sighting.rec = {**sighting.rec, "price": None}  # the price now depends on the zone
        prod.name = base[:1].upper() + base[1:]
        prod.groups = [{"name": "Service zone", "required": True, "addons": False, "choices": choices}]
        return prod

    def split_addons(self, prod: Product) -> None:
        """In one price table, rows priced as additions ('add $100', '+$129') are optional extras."""
        for g in list(prod.groups):
            adds = [c for c in g["choices"] if (c.get("price") or {}).get("delta")]
            if adds and len(adds) < len(g["choices"]):
                g["choices"] = [c for c in g["choices"] if c not in adds]
                prod.groups.append({"name": "Extras", "required": False, "addons": True, "choices": adds})

    def model_sighting(self, page: Page, rec: JsonObj) -> Sighting | None:
        name = clean_text(rec.get("name"))
        if not name:
            return None
        ev = self.evidence(page, rec, f"item {name}")
        if ev is None:
            return None
        price = self.price(rec)
        if price is not None and "amount_cents" in price and not price_ok(price, ev["quote"], quote_has_amount):
            self.dropped.append({"page": page.url, "what": f"price of {name} (amount not in quote)", "quote": ev["quote"][:200]})
            price = None
        if price is not None and not _names_item(ev["quote"], name):
            widened = self.indexes[page.url].widen_to_name(ev["quote"], name)
            if widened and quote_has_amount(widened, price.get("amount_cents", -1)):
                ev = {"url": page.url, "quote": widened}
            else:
                self.dropped.append({"page": page.url, "what": f"price of {name} (quote does not name it)", "quote": ev["quote"][:200]})
                price = None
        if price is not None and price["kind"] == "fixed" and re.search(
            rf"\b(from|starting at|starts at)\s+(\$|₹\s?|rs\.?\s?)?{price['amount_cents'] // 100:,}\b".replace(",", ",?"), ev["quote"], re.I
        ):
            price["kind"] = "from"
        out = {"name": name, "price": price, "description": rec.get("description"), "category": rec.get("category"),
               "duration_minutes": as_int(rec.get("duration_minutes")), "made_to_order": rec.get("made_to_order") is True,
               "model_allergens": rec.get("allergens"), "model_dietary": rec.get("dietary")}
        return Sighting(page.url, out, "model", ev)

    def group_from(self, page_url: str, choices: list[JsonObj], label: str, product: str, *, required: bool | None,
                   addons: bool) -> JsonObj:
        out = []
        expanded: list[JsonObj] = []
        for rec in choices:
            alts = price_alternatives(rec.get("price_text") or "")
            if not alts:
                expanded.append(rec)
                continue
            # "$175 with a cleaning, $295 on its own": one choice per case; "with <another job>" is an add-on.
            for alt_label, alt_price in alts:
                addon = alt_label.lower().startswith("with ") and bool(words(alt_label) & (words(product) | words(" ".join(c["name"] for c in choices if c is not rec))))
                expanded.append({**rec, "name": f"{rec['name']}, {alt_label}", "price": {**alt_price, **({"delta": True} if addon else {})}})
        for rec in expanded:
            if rec.get("included") or not rec.get("name"):
                continue
            ev = self.locate_ev(page_url, rec["quote"])
            if ev is None:
                continue
            price = rec.get("price")
            if price is not None and not price_ok(price, ev["quote"], quote_has_amount):
                price = None
            out.append({**rec, "price": price, "ev": ev})
        text = self.page_text(page_url).lower()
        name = group_label(out, singular(label), product)
        if required is None:
            noun = next(iter(words(singular(label)) - words(product)), "") or next(iter(words(label)), "")
            required = bool(noun) and bool(re.search(rf"\b(choose|pick|select)\b[^.]{{0,80}}\b{re.escape(noun)}", text))
        return {"name": name, "required": bool(required), "addons": addons, "choices": out}

    def alternative_groups(self, page_url: str, rec: JsonObj) -> list[JsonObj]:
        return []

    def prefix_groups(self, page_url: str, rows: list[Sighting]) -> list[Product]:
        """Rows that share a leading name ('Balayage, shoulder length', 'Balayage, past the shoulder blades')
        are one product with a choice; every other row is a product of its own."""
        by_base: dict[str, list[Sighting]] = OrderedDict()
        for s in rows:
            name = s.rec["name"]
            key = base_name(name).lower() if re.search(r"[,:]", name) else ""
            by_base.setdefault(key or f"\0{id(s)}", []).append(s)
        out: list[Product] = []
        for key, group in by_base.items():
            if key.startswith("\0") or len(group) < 2 or len({s.source for s in group}) > 1:
                for s in group:
                    prod = Product(s.rec["name"], [s])
                    if alts := self.alternative_choices(s):
                        prod.groups.append(alts)
                    if s.rec.get("variant_choices"):
                        label = "Flavor" if re.search(r"\bflavou?r|chocolate|vanilla", s.ev["quote"], re.I) else "Type"
                        prod.groups.append({"name": label, "required": True, "addons": False,
                                            "choices": [{**c, "ev": s.ev} for c in s.rec["variant_choices"]]})
                    out.append(prod)
                continue
            name = base_name(group[0].rec["name"])
            choices = [{**s.rec, "spoken": tail_name(s.rec["name"]), "ev": s.ev} for s in group]
            common = [n for n in group[0].rec.get("notes") or [] if all(n in (x.rec.get("notes") or []) for x in group)]
            anchor = min(group, key=lambda s: (s.rec.get("price") or {}).get("amount_cents") or 10**12)
            prod = Product(name, [Sighting(page_url, {"name": name, "description": "", "category": group[0].rec.get("category"),
                                                      "notes": common, "section_note": group[0].rec.get("section_note"),
                                                      "staff": group[0].rec.get("staff") if all(x.rec.get("staff") == group[0].rec.get("staff") for x in group) else None,
                                                      "marks": "".join(dict.fromkeys("".join(x.rec.get("marks") or "" for x in group))),
                                                      "table_key": group[0].rec.get("table_key")}, "variants", anchor.ev)])
            prod.groups.append({"name": group_label(choices, "", name), "required": True, "addons": False, "choices": choices})
            out.append(prod)
        return out

    def alternative_choices(self, s: Sighting) -> JsonObj | None:
        """'$125 for a journeyman plumber, $145 for a master plumber' in a row's details: an optional choice."""
        alts = s.rec.get("alternatives") or []
        if len(alts) < 2:
            return None
        choices = []
        for label, price in alts:
            if quote_has_amount(s.ev["quote"], price["amount_cents"]):
                per = (s.rec.get("price") or {}).get("per")
                choices.append({"name": label[:1].upper() + label[1:], "price": {**price, **({"per": per} if per else {})}, "ev": s.ev})
        return {"name": "Option", "required": False, "addons": False, "choices": choices} if len(choices) >= 2 else None

    def diet_versions(self, prod: Product) -> None:
        """A pick list with '<diet> version: Available' columns: each available version is a choice of its own,
        priced by the product's '<diet>' extra (which then leaves the extras)."""
        extras = [g for g in prod.groups if g.get("addons")]
        for g in prod.groups:
            flagged = [c for c in g["choices"] if c.get("flags")]
            if not flagged:
                continue
            new = []
            for header in flagged[0]["flags"]:
                tags = diet_labels(header)
                if len(tags) != 1:
                    continue
                extra = next(((eg, c) for eg in extras for c in eg["choices"] if diet_labels(c["name"]) == tags), None)
                word = re.split(r"\s+version", header, flags=re.I)[0].strip()
                for c in flagged:
                    if c["flags"].get(header):
                        version = {"name": f"{word} {c['name'][:1].lower()}{c['name'][1:]}", "spoken": None, "dietary": tags,
                                   "ev": c["ev"], "quote": c["quote"]}
                        if extra is not None:
                            version["price"] = extra[1].get("price")
                            version["per_text"] = extra[1]["name"]
                            version["ev_extra"] = extra[1]["ev"]
                        new.append(version)
                if extra is not None:
                    extra[0]["choices"] = [c for c in extra[0]["choices"] if c is not extra[1]]
            self.version_allergens(prod, new)
            g["choices"] += new
        prod.groups = [g for g in prod.groups if g["choices"]]

    def version_allergens(self, prod: Product, versions: list[JsonObj]) -> None:
        """'Gluten-free cakes contain milk and eggs, and the dark chocolate one also contains soy.' / 'Vegan cakes
        contain no milk or eggs, but they do contain wheat and soy.': what each diet version contains."""
        page_url = prod.primary.page_url
        for sec in self.structures[page_url].sections:
            tags = diet_labels(sec["question"])
            if len(tags) != 1:
                continue
            for unit in sec["units"]:
                for sentence in re.split(r"(?<=[.!?])\s+", unit):
                    low = sentence.lower()
                    m = re.search(r"\bdo contain ([^.;]+)", low) or re.search(r"\bcontains? (?!no\b)([^.;]+?)(?:,? and the |, but |\.|$)", low)
                    if not m or not re.search(r"\bcontains?\b", low):
                        continue
                    base = parse_allergens(m.group(1))
                    if base is None:
                        continue
                    extras = {f.strip(): parse_allergens(a) or [] for f, a in re.findall(r"the ([a-z -]+?) one also contains ([^.;,]+)", low)}
                    ev = self.locate_ev(page_url, sentence)
                    if ev is None:
                        continue
                    for v in versions:
                        if v.get("dietary") != tags or "allergens" in v:
                            continue
                        found = set(base)
                        for flavor, more in extras.items():
                            if words(flavor) <= words(v["name"]):
                                found |= set(more)
                        v["allergens"] = sorted(found)
                        v["ev"] = ev

    def merge(self, prods: list[Product], incoming: list[Product]) -> list[Product]:
        for new in incoming:
            target = self.match_product(prods, new)
            if target is None:
                if self.absorbed_by_choice(prods, new):
                    continue
                prods.append(new)
                continue
            target.sightings += new.sightings
            if new.groups and not target.groups:
                target.groups = new.groups
            if words(new.name) > words(target.name) and not re.search(r"[,:(]", new.name) and new.primary.source in ("card", "table", "variants"):
                target.name = new.name  # "Water heater repair and replacement" names more than "Water heaters"
        return prods

    def match_product(self, prods: list[Product], new: Product) -> Product | None:
        price = (new.primary.rec.get("price") or None)
        for p in prods:
            if same_product(p.name, new.name) and compatible(self.product_price(p), price):
                return p
        for p in prods:
            if same_product(p.name, new.name) and p.groups and (price or {}).get("kind") == "from" and self.floor_splits(p, price):
                return p  # "TV mounting from $149": the cheaper rows of its table are extras
        for p in prods:
            pp = self.product_price(p)
            if (contains_product(new.name, p.name) or contains_product(p.name, new.name)) and (price is None or pp is None or compatible(pp, price)):
                if min(len(words(new.name)), len(words(p.name))) >= 1 and (new.groups or p.groups or price is None or pp is None or compatible(pp, price)):
                    if contains_product(base_name(new.name), p.name) or contains_product(base_name(p.name), new.name):
                        return p
        return None

    def floor_splits(self, p: Product, floor: JsonObj) -> bool:
        """A product's own "from $X" names its cheapest real choice: rows in its table priced below $X are
        extras (a mount, an anchoring kit), not choices. True if that makes the product's price $X."""
        amount = floor.get("amount_cents")
        base = p.groups[0] if p.groups and not p.groups[0].get("addons") else None
        if amount is None or base is None:
            return False
        below = [c for c in base["choices"] if (c.get("price") or {}).get("amount_cents") is not None and c["price"]["amount_cents"] < amount]
        rest = [c for c in base["choices"] if c not in below]
        if not below or not any((c.get("price") or {}).get("amount_cents") == amount for c in rest):
            return False
        base["choices"] = rest
        extras = next((g for g in p.groups if g.get("addons")), None)
        moved = [{**c, "price": {**c["price"], "delta": True}} for c in below]
        if extras is None:
            p.groups.append({"name": "Extras", "required": False, "addons": True, "choices": moved})
        else:
            extras["choices"] = moved + extras["choices"]
        return True

    def absorbed_by_choice(self, prods: list[Product], new: Product) -> bool:
        """A row that repeats one product's choice ('6-inch round $56' beside the size table) is that choice."""
        price = new.primary.rec.get("price")
        for p in prods:
            for g in p.groups:
                for c in g["choices"]:
                    if (contains_product(new.name, c["name"]) or same_product(new.name, c["name"])) and compatible(c.get("price"), price):
                        return True
        return False

    def product_price(self, p: Product) -> JsonObj | None:
        for s in sorted(p.sightings, key=lambda s: {"variants": 0, "product": 0, "table": 1, "card": 2, "model": 3}.get(s.source, 9)):
            if s.rec.get("price"):
                return s.rec["price"]
        absolute = [c["price"]["amount_cents"] for g in p.groups if not g.get("addons") for c in g["choices"]
                    if c.get("price") and c["price"].get("amount_cents") is not None and not c["price"].get("delta")]
        return {"kind": "from", "amount_cents": min(absolute)} if absolute else None

    def drop_teasers(self, prods: list[Product]) -> list[Product]:
        """'Pastries from $3.75' on the home page names a category of the menu, not a product."""
        cats: dict[frozenset[str], int] = {}
        for p in prods:
            for s in p.sightings:
                c = s.rec.get("category")
                if c:
                    cats[frozenset(words(singular(c)))] = cats.get(frozenset(words(singular(c))), 0) + 1
        out = []
        for p in prods:
            key = frozenset(words(singular(p.name)))
            if all(s.source == "card" for s in p.sightings) and not p.groups and cats.get(key, 0) >= 2:
                self.dropped.append({"page": p.primary.page_url, "what": f"category teaser {p.name}", "quote": p.primary.ev["quote"][:200]})
                continue
            out.append(p)
        return out

    def page_text(self, url: str) -> str:
        page = self.by_url[url]
        cache = self.__dict__.setdefault("_page_text", {})
        if url not in cache:
            from askonce_onboarding.injection import model_text

            cache[url] = model_text(page.html, [c for c in self.canaries.get(url, [])])
        return cache[url]

    # ------------------------------------------------------------------ catalog: product -> item

    def product_item(self, prod: Product, module_id: str, categories: dict[str, JsonObj]) -> JsonObj | None:
        prim = prod.primary
        name = clean_text(prod.name)
        if not name:
            return None
        item: JsonObj = {"id": slug(name), "name": name, "spoken_name": spoken_name(name) or slug(name)}
        descs: list[str] = []
        desc_url: dict[str, str] = {}
        for s in sorted(prod.sightings, key=lambda s: {"variants": 0, "product": 0, "table": 1, "card": 2, "model": 3}.get(s.source, 9)):
            notes = [n for n in s.rec.get("notes") or [] if n and not re.match(r"^\d", n)]
            if s.rec.get("price_text") and not s.rec.get("price") and re.search(r"[a-z]", s.rec["price_text"], re.I):
                notes.insert(0, s.rec["price_text"])  # "Our cost plus 15%": the price, in words
            # A product's own intro comes before the notes under it; a row's note is its description.
            ordered = [s.rec.get("description") or "", *notes] if s.source in ("variants", "product") else [*notes, s.rec.get("description") or ""]
            for text in ordered:
                text = clean_text(text, 400) or ""
                if not text or has_price_only(text) or parse_duration_only(text):
                    continue
                tw = words(text)
                if len(tw - words(name)) <= 1 or any(len(tw & words(d)) >= 0.6 * len(tw) for d in descs):
                    continue  # the item's own name, or a sentence another description already says
                if not re.search(r"[.!?]$", text):
                    text += "."
                descs.append(text)
                desc_url.setdefault(text, s.page_url)
        desc = spoken(" ".join(descs[:2])) if descs else None
        if desc:
            item["description_spoken"] = desc
            first = descs[0]
            rest = first[len(desc):].strip() if first.startswith(desc) and len(first) > len(desc) else ""
            if rest and len(rest) >= 20 and prod.primary.source != "model":
                self.__dict__.setdefault("item_overflow_pending", []).append((item, rest, desc_url[first]))
        cat = next((s.rec.get("category") for s in sorted(prod.sightings, key=lambda s: {"table": 0, "variants": 0, "card": 1, "model": 2}.get(s.source, 9))
                    if s.rec.get("category") and not GENERIC_CATEGORY.search(s.rec["category"])), None)
        if cat:
            cat_id = slug(cat)
            categories.setdefault(cat_id, {"id": cat_id, "name": clean_text(cat), "spoken_name": spoken_name(cat) or cat_id})
            item["category_ids"] = [cat_id]
        evidence: list[JsonObj] = []
        price = None
        for s in sorted(prod.sightings, key=lambda s: {"variants": 0, "product": 0, "table": 1, "card": 2, "model": 3}.get(s.source, 9)):
            p = s.rec.get("price")
            if p and not p.get("delta") and price_ok(p, s.ev["quote"], quote_has_amount):
                price = self.money(p)
                evidence.insert(0, s.ev)
                break
        free_choice = any((c.get("price") or {}).get("kind") == "free" for g in prod.groups if not g.get("addons") for c in g["choices"])
        if price is None and prod.groups and not free_choice:
            absolute = [(c["price"], c["ev"]) for g in prod.groups if not g.get("addons") for c in g["choices"]
                        if c.get("price") and c["price"].get("amount_cents") is not None and not c["price"].get("delta")
                        and c["price"]["kind"] in ("fixed", "range", "from")]
            if absolute:
                low = min(absolute, key=lambda pe: pe[0]["amount_cents"])
                price = self.money({"kind": "from", "amount_cents": low[0]["amount_cents"], **({"per": low[0]["per"]} if low[0].get("per") else {})})
                evidence.insert(0, low[1])
        if price is not None:
            item["price"] = price
        for s in prod.sightings:
            if s.ev not in evidence:
                evidence.append(s.ev)
        duration = next((s.rec.get("duration_minutes") for s in sorted(prod.sightings, key=lambda s: {"table": 0, "variants": 0, "card": 1, "model": 2}.get(s.source, 9))
                         if s.rec.get("duration_minutes")), None)
        has_choice_durations = any(c.get("duration_minutes") for g in prod.groups for c in g["choices"])
        if duration and not has_choice_durations:
            item["duration_minutes"] = duration
        options = self.options(prod, item)
        if options:
            item["options"] = options
            for g in prod.groups:
                for c in g["choices"]:
                    if c.get("ev") and c["ev"] not in evidence:
                        evidence.append(c["ev"])
        days = next((s.rec.get("days") for s in prod.sightings if s.rec.get("days")), None)
        if days and set(days) != set(DAYS):
            item["availability"] = {"status": "available", "days": [d for d in DAYS if d in days]}
        if any(s.rec.get("made_to_order") for s in prod.sightings):
            item["availability"] = {**item.get("availability", {}), "status": "made_to_order"}
        item["provenance"] = {"owner_confirmed": False, "evidence": evidence[:10]}
        return item

    def money(self, p: JsonObj) -> JsonObj:
        out: JsonObj = {"kind": p["kind"]}
        if p["kind"] in ("fixed", "from", "range"):
            out["amount_cents"] = p["amount_cents"]
            out["currency"] = self.currency
        if p["kind"] == "range":
            if p.get("max_cents") is None:
                out["kind"] = "fixed"
            else:
                out["max_cents"] = p["max_cents"]
        if p.get("per") and p["kind"] != "free":
            out["per"] = p["per"]
        return out

    def options(self, prod: Product, item: JsonObj) -> list[JsonObj]:
        out = []
        gids: set[str] = set()
        for g in prod.groups[:6]:
            used: set[str] = set()
            choices = []
            for c in g["choices"][:12]:
                cname = clean_text(c["name"])
                if not cname:
                    continue
                choice: JsonObj = {"id": choice_id(c.get("id_hint") or cname, used, slug), "name": cname,
                                   "spoken_name": spoken_name(c.get("spoken") or cname) or slug(cname)}
                p = c.get("price")
                if p and p.get("kind") in ("fixed", "range", "from", "free", "quote_only"):
                    money = self.money(p)
                    if "per" not in money and (per := last_per(c.get("per_text") or cname)):
                        money["per"] = per
                    if p.get("delta"):
                        choice["price_delta"] = money
                    else:
                        choice["price"] = money
                if c.get("duration_minutes"):
                    choice["duration_minutes"] = c["duration_minutes"]
                attrs = {k: v for k, v in (c.get("facts") or {}).items() if isinstance(v, int | float | str | bool)}
                if attrs:
                    choice["attributes"] = attrs
                    for k, v in attrs.items():
                        self.declare(k, v)
                choice["_safety"] = {"allergens": c.get("allergens"), "dietary": c.get("dietary"), "ev": c.get("ev")}
                choices.append(choice)
            if not choices:
                continue
            gid = choice_id(g["name"], gids, slug)
            group: JsonObj = {"id": gid, "name": clean_text(g["name"]) or "Options", "required": bool(g.get("required")) and not g.get("addons"),
                              "choices": choices}
            if g.get("addons"):
                group["max_selections"] = len(choices)
            out.append(group)
        return out

    def finish_item(self, prod: Product, item: JsonObj, module_id: str) -> None:
        ref = f"{module_id}/{item['id']}"
        meta_cat = next((s.rec.get("category") for s in prod.sightings if s.rec.get("category")), "") or ""
        self.item_meta[item["id"]] = {"category": meta_cat.lower(), "name": item["name"].lower()}
        staff = next((s.rec.get("staff") for s in prod.sightings if s.rec.get("staff")), None)
        if staff:
            self.item_staff[item["id"]] = staff
        if item.get("availability", {}).get("status") == "made_to_order":
            self.made_to_order.append(ref)
        attrs: JsonObj = {}
        for s in prod.sightings:
            for k, v in (s.rec.get("facts") or {}).items():
                if isinstance(v, int | float | str | bool):
                    attrs.setdefault(snake(k), v)
            if s.rec.get("duration_is_minimum"):
                attrs["duration_is_minimum"] = True
        marks = "".join(s.rec.get("marks") or "" for s in prod.sightings)
        marks += "".join(c.get("marks") or "" for g in prod.groups for c in g["choices"])
        for mark in dict.fromkeys(marks):
            rule = self.mark_rule(prod.primary.page_url, mark)
            if rule is not None and re.search(r"\bdeposit\b", rule["quote"], re.I):
                attrs["requires_deposit"] = True
                self.deposit_items.append((ref, rule))
        if "requires_deposit" not in attrs:
            for s in prod.sightings:
                siblings = self.table_rows.get(s.rec.get("table_key"), [])
                marked = {m for r in siblings for m in r.get("marks") or ""}
                if any((rule := self.mark_rule(s.page_url, m)) is not None and re.search(r"\bdeposit\b", rule["quote"], re.I) for m in marked):
                    attrs["requires_deposit"] = False  # "services marked * need a deposit": the others do not
        if attrs:
            item["attributes"] = {**item.get("attributes", {}), **attrs}
            for k, v in attrs.items():
                self.declare(k, v)
        # Safety facts (allergens, diets) wait for the owner's confirmation.
        for s in prod.sightings:
            allergens = s.rec.get("allergens")
            dietary = s.rec.get("dietary")
            if s.source == "model":
                continue  # the model's allergen guesses are never used; only what a page structure states
            bucket = self.safety_items.setdefault(s.page_url, [])
            if allergens is not None and not any(r == f"{ref}#allergens" for r, _, _ in bucket):
                bucket.append((f"{ref}#allergens", {"contains": sorted(allergens)}, s.ev))
            if dietary and not any(r == f"{ref}#dietary" for r, _, _ in bucket):
                bucket.append((f"{ref}#dietary", {"tags": sorted(dietary)}, s.ev))
        for group in item.get("options", []):
            for choice in group["choices"]:
                info = choice.pop("_safety", {})
                ev = info.get("ev")
                if not ev:
                    continue
                bucket = self.safety_items.setdefault(ev["url"], [])
                cref = f"{ref}/{group['id']}/{choice['id']}"
                if info.get("allergens") is not None:
                    bucket.append((f"{cref}#allergens", {"contains": sorted(info["allergens"])}, ev))
                if info.get("dietary"):
                    bucket.append((f"{cref}#dietary", {"tags": sorted(info["dietary"])}, ev))

    def mark_rule(self, page_url: str, mark: str) -> JsonObj | None:
        """The sentence a footnote mark points to ('Services marked * need a $50 deposit', '*Color services ...')."""
        text = self.page_text(page_url)
        esc = re.escape(mark)
        for m in re.finditer(rf"(?:[^.!?]*\bmarked\s*{esc}[^.!?]*[.!?])|(?:(?<!\w){esc}\s*[A-Z][^{esc}]{{10,300}}?[.!?](?:\s[A-Z][^.!?]*[.!?])?)", text):
            quote = m.group(0).strip()
            ev = self.locate_ev(page_url, quote)
            if ev is not None:
                return ev
        return None

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
        """Policy and FAQ entries: one per thing the site says (an FAQ answer, a list item, a paragraph), from
        the pages' own sections, with the model's policy facts attached to the entry whose words they quote.
        The same statement on two pages is one entry."""
        self.safety_entries: list[tuple[str, JsonObj]] = []
        self.policy_facts: dict[str, tuple[Any, list[JsonObj], str]] = {}  # key -> (value, evidence, entry ref)
        self.module_knowledge = module_id
        entries: list[JsonObj] = []
        self.units = self.knowledge_units()
        for unit in self.units:
            entries.append(unit)
        # The model's policies: facts go to the entry that holds their quote; the rest become entries.
        for page, rec in self.records("policies"):
            ev = self.evidence(page, rec, "policy")
            if ev is None:
                continue
            topic = snake(rec.get("topic"))
            facts = self.policy_record_facts(page, rec, ev)
            question = clean_text(rec.get("question"))
            host = self.host_entry(entries, ev)
            if host is None and self.repeats_catalog(ev):
                continue  # a price-list row read as a policy: the catalog item already says it
            if host is None:
                answer = spoken(rec.get("answer"))
                if not answer or POINTER.fullmatch(answer):
                    continue
                host = {"_heading": topic.replace("_", " "), "_text": answer, "_page": page.url, "_evs": [ev], "_facts": {},
                        "_questions": [], "_safety": False, "_applies": [], "_topic": topic, "_model": True}
                entries.append(host)
            for key, value in facts.items():
                host["_facts"].setdefault(key, value)
                host.setdefault("_fact_quotes", {})[key] = (page, ev["quote"], topic)
            if question and len(question) >= 3 and question not in host["_questions"]:
                host["_questions"].append(question)
            host["_topic"] = host.get("_topic") or topic
            if rec.get("safety") is True:
                host["_safety"] = True
        # One subject, one topic: every statement of a section takes the topic the model gave any of them,
        # else the section's heading (the matcher ranks topics, so a split subject reads as ambiguous).
        section_topic: dict[str, str] = {}
        for e in entries:
            if e.get("_section") and e.get("_topic") and e["_section"] not in section_topic:
                section_topic[e["_section"]] = e["_topic"]
        for e in entries:
            if e.get("_section"):
                e["_topic"] = section_topic.get(e["_section"]) or snake(e["_heading"])
        merged = self.dedupe_entries(entries)
        built: list[tuple[JsonObj, list[JsonObj]]] = []
        taken: dict[str, JsonObj] = {}
        for e in merged:
            chunks = self.entries_from(e, taken)
            if chunks:
                built.append((e, chunks))
                taken.update({c["id"]: c for c in chunks})
        firsts = [(id(chunks[0]), words(chunks[0]["answer_spoken"])) for _, chunks in built]
        seen_answers: list[set[str]] = []
        for _, chunks in built:
            kept = [chunks[0]]
            for c in chunks[1:]:
                cw = words(c["answer_spoken"])
                others = seen_answers + [w for i, w in firsts if i != id(chunks[0])]
                if any(cw and 2 * len(cw & w) / (len(cw) + len(w)) >= 0.8 for w in others):
                    continue  # a continuation that only repeats another entry's answer
                kept.append(c)
            chunks[:] = kept
            seen_answers += [words(c["answer_spoken"]) for c in chunks]
        if sum(len(c) for _, c in built) > MAX_ENTRIES:
            # The schema holds 100 entries (and owner answers add some): keep the most informative statements.
            def weight(pair: tuple[JsonObj, list[JsonObj]]) -> tuple:
                """Specific statements first: a question's answer, a number, a 'not', a fact or a safety note."""
                e = pair[0]
                text = e["_text"]
                score = 2 * (e.get("_kind") in ("faq", "dl")) + bool(e["_facts"]) + e["_safety"] + bool(re.search(r"\d", text))
                score += bool(re.search(r"\b(no|not|never|don't|can't|won't|only)\b|n't\b", text, re.I)) + (len(text) >= 40)
                score -= e["_page"] == self.home and e.get("_kind") != "faq"
                return (score, not e.get("_model"), len(text))
            keep, count = set(), 0
            for pair in sorted(built, key=weight, reverse=True):
                if count + len(pair[1]) <= MAX_ENTRIES:
                    keep.add(id(pair[0]))
                    count += len(pair[1])
            for e, _ in built:
                if id(e) not in keep:
                    self.dropped.append({"page": e["_page"], "what": "policy entry (over the entry limit)", "quote": e["_text"][:200]})
            built = [pair for pair in built if id(pair[0]) in keep]
        out: dict[str, JsonObj] = OrderedDict()
        for e, chunks in built:
            entry = chunks[0]
            out[entry["id"]] = entry
            for extra in chunks[1:]:
                out[extra["id"]] = extra
                if e.get("_safety"):
                    self.safety_entries.append((f"{module_id}/{extra['id']}", extra["provenance"]["evidence"][0]))
            ref = f"{module_id}/{entry['id']}"
            for key, (page, quote, topic) in (e.get("_fact_quotes") or {}).items():
                if key not in entry.get("facts", {}):
                    continue
                self.add_fact(page, f"policy:{topic}:{key}", key, entry["facts"][key], quote,
                              [f"{ref}#facts.{key}", f"{ref}#answer_spoken"], f"{topic.replace('_', ' ')}: {key.replace('_', ' ')}")
                self.policy_facts.setdefault(key, (entry["facts"][key], [{"url": page.url, "quote": quote}], ref))
            if e.get("_safety") and all(r != ref for r, _ in self.safety_entries):
                self.safety_entries.append((ref, entry["provenance"]["evidence"][0]))
        if not out:
            return None
        unify_fact_types(out.values())
        for entry in out.values():
            for key, value in (entry.get("facts") or {}).items():
                self.declare(key, value)
        return {"type": "knowledge", "title": "Policies and FAQ", "entries": list(out.values())[:100]}

    @staticmethod
    def add_variants(entries: list[JsonObj], found: dict[str, list[str]], business: str = "") -> None:
        """Customer phrasings join each entry's question variants; one phrasing offered for two entries is
        ambiguous and kept by neither."""
        owners: dict[str, set[str]] = {}
        for eid, qs in found.items():
            for q in qs:
                owners.setdefault(norm_words(strip_name(q, business)), set()).add(eid)
        by_id = {e["id"]: e for e in entries}
        for e in entries:
            have = {norm_words(v) for v in e["question_variants"]}
            for q in found.get(e["id"], []):
                q = strip_name(q, business)
                key = norm_words(q)
                shared = owners.get(key, set())
                if key in have or len(e["question_variants"]) >= 8:
                    continue
                if len(shared) > 1 and len({by_id[i]["topic"] for i in shared if i in by_id}) > 1:
                    continue  # one phrasing for two different subjects is ambiguous: kept by neither
                e["question_variants"].append(q)
                have.add(key)

    def policy_record_facts(self, page: Page, rec: JsonObj, ev: JsonObj) -> JsonObj:
        facts: JsonObj = {}
        for f in rec.get("facts") or []:
            if isinstance(f, dict) and (key := snake(f.get("key"), "")):
                value = fact_value(f.get("value"))
                if value is None or value == "":
                    continue
                if isinstance(value, int) and not isinstance(value, bool) and not quote_supports_number(ev["quote"], value):
                    self.dropped.append({"page": page.url, "what": f"policy fact {key}={value} (number not in quote)", "quote": ev["quote"][:200]})
                    continue
                facts[key] = value
        return facts

    def repeats_catalog(self, ev: JsonObj) -> bool:
        q = norm_words(ev["quote"])
        for item in getattr(self, "catalog_evidence", []):
            if item["url"] == ev["url"]:
                t = norm_words(item["quote"])
                if q and t and (q in t or t in q):
                    return True
        return False

    def host_entry(self, entries: list[JsonObj], ev: JsonObj) -> JsonObj | None:
        """The section entry whose own words contain (or are contained in) a model quote."""
        q = norm_words(ev["quote"])
        best, best_overlap = None, 0.0
        for e in entries:
            if e.get("_page") != ev["url"] or e.get("_model"):
                continue
            t = norm_words(e["_text"])
            if q and (q in t or t in q):
                overlap = min(len(q), len(t)) / max(len(q), len(t))
                if overlap > best_overlap:
                    best, best_overlap = e, overlap
        return best

    def knowledge_units(self) -> list[JsonObj]:
        names = self.people_names()
        out = self.lede_units() + self.item_note_units()
        for page in self.pages:
            s = self.structures[page.url]
            for sec in s.sections:
                heading = clean_text(sec["question"], 200) or ""
                if not heading or REVIEW_HEADING.search(heading):
                    continue
                person = any(heading.lower() == n.lower() for n in names)
                if any(c in sec["answer"] for c in "★☆"):
                    continue
                if sec["headings"] and any(h.lower() in (n.lower() for n in names) for h in sec["headings"][:1]):
                    continue  # a person's own facts (works, specialties) belong to the staff record
                units = sec["units"] if sec["kind"] != "faq" else [" ".join(sec["units"])]
                if units and sec["kind"] == "section" and re.search(r"\d", heading) and not heading.endswith("?") and len(heading.split()) >= 3:
                    units = [f"{heading}: {units[0]}", *units[1:]]  # "Get in touch at least 72 hours ahead": the heading says it
                if person:
                    units = [" ".join(units)]  # a person's role and bio say who they are, as one statement
                units = merge_pronoun_units(units)
                lead = True
                for text in units:
                    text = clean_text(text, 2000) or ""
                    text = re.sub(r"\s+(?:see|read) (?:below|above|our [a-z ]+ (?:policy|page))\.?$", "", text, flags=re.I).strip()
                    if text.endswith("?") and not re.search(r"[.!]\s", text):
                        continue  # a question with no answer after it
                    if len(text) < 12 or self.lists_items(text) or self.item_card(text) or OPENING_HOURS.search(text) and DAY_AND_TIME.search(text):
                        continue
                    if POINTER.fullmatch(text):
                        continue  # "Please read our cancellation policy": it points elsewhere and says nothing itself
                    codes = re.findall(r"\b\d{5}\b", text)
                    if len(codes) >= 2 and all(c in getattr(self, "zone_of", {}) for c in codes):
                        continue  # a list of served postcodes: the service areas hold it
                    evs = self.unit_evidence(page.url, text, heading if sec["kind"] in ("faq", "dl") else None)
                    if not evs:
                        continue
                    out.append({"_heading": heading, "_question": heading if heading.endswith("?") else None, "_text": text,
                                "_page": page.url, "_evs": evs, "_facts": {}, "_questions": [], "_chain": sec["headings"],
                                "_safety": bool(SAFETY_WORDS.search(text)), "_kind": sec["kind"], "_topic": None,
                                "_section": f"{page.url}#{heading}" if sec["kind"] != "note" else None, "_lead": lead})
                    lead = False
        return out

    def item_note_units(self) -> list[JsonObj]:
        """A price row's details that its two-sentence description cannot hold ('Credited toward the repair if
        you approve it on the same visit.') are a statement about that item."""
        out = []
        for item_id, notes in getattr(self, "item_overflow", {}).items():
            for url, text in notes:
                evs = self.unit_evidence(url, text, None)
                if evs:
                    name = self.catalog_items.get(f"{self.catalog_id}/{item_id}", {}).get("name", item_id)
                    out.append({"_heading": name[:1].upper() + name[1:], "_question": None, "_text": text, "_page": url, "_evs": evs,
                                "_facts": {}, "_questions": [], "_chain": [], "_safety": bool(SAFETY_WORDS.search(text)), "_kind": "section",
                                "_topic": None, "_section": f"{url}#item:{item_id}", "_lead": True})
        return out

    def lede_units(self) -> list[JsonObj]:
        """What each page says first, under its headline (the home page's lede says what the business is)."""
        out = []
        for i, page in enumerate(self.pages):
            st = self.structures[page.url]
            heading = "About us" if i == 0 else (clean_text(st.product, 120) or self.page_label(page.url))
            lead = True
            for text in st.lede:
                text = clean_text(text, 2000) or ""
                if len(text) < 40 or OPENING_HOURS.search(text) and DAY_AND_TIME.search(text) or POINTER.fullmatch(text):
                    continue
                evs = self.unit_evidence(page.url, text, None)
                if evs:
                    out.append({"_heading": heading, "_question": None, "_text": text, "_page": page.url, "_evs": evs,
                                "_facts": {}, "_questions": [], "_chain": [], "_safety": bool(SAFETY_WORDS.search(text)), "_kind": "section",
                                "_topic": "about" if i == 0 else None, "_section": f"{page.url}#lede", "_lead": lead})
                    lead = False
        return out

    def unit_evidence(self, url: str, text: str, question: str | None) -> list[JsonObj]:
        """Quotes for a unit: the question and its answer together when they fit, else sentence groups."""
        evs: list[JsonObj] = []
        if question:
            ev = self.locate_ev(url, f"{question} {text}")
            if ev and len(ev["quote"]) >= len(text):
                return [ev]
        chunks, cur = [], ""
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if cur and len(cur) + len(sentence) > 450:
                chunks.append(cur)
                cur = sentence
            else:
                cur = f"{cur} {sentence}".strip()
        if cur:
            chunks.append(cur)
        for chunk in chunks[:10]:
            ev = self.locate_ev(url, chunk)
            if ev is None:
                # A list or a table read as one statement ("a; b; c"): each part is on the page by itself.
                parts = [p.strip(" .") for p in re.split(r";\s+|(?<=[.!?])\s+|(?<=[\w)]):\s+", chunk) if len(p.strip(" .")) >= 3]
                found = [self.locate_ev(url, p) for p in parts]
                if not parts or any(f is None for f in found):
                    return []
                evs += [f for f in found if f not in evs]
                continue
            evs.append(ev)
        return evs[:10]

    def lists_items(self, text: str) -> bool:
        """A paragraph that is really part of the price list (it names two or more catalog items with prices)."""
        prices = len(re.findall(r"[$₹]\s?\d", text))
        if prices < 2:
            return False
        names = [n for n in getattr(self, "item_names", []) if len(n) > 3 and n.lower() in text.lower()]
        return len(names) >= 2

    def item_card(self, text: str) -> bool:
        """'Women's cut & style 1 hr $78': a catalog item's own card, not a statement."""
        low = text.lower()
        return any(low.startswith(n.lower()) and re.match(r"\s*[^.]{0,20}?([$₹]\s?\d|\bfree\b)", low[len(n):])
                   for n in getattr(self, "item_names", []))

    def people_names(self) -> list[str]:
        names = []
        for page, rec in self.records("people"):
            if (n := clean_text(rec.get("name"))):
                names.append(n)
        for s in self.structures.values():
            names += [x["name"] for x in s.schedules]
        return list(dict.fromkeys(names))

    def dedupe_entries(self, entries: list[JsonObj]) -> list[JsonObj]:
        """One entry per statement: the same words on two pages (or a model answer repeating a section) merge."""
        out: list[JsonObj] = []
        for e in entries:
            twin = None
            for o in out:
                a, b = words(e["_text"]), words(o["_text"])
                sa, sb = words(spoken(e["_text"]) or ""), words(spoken(o["_text"]) or "")
                # A summary ("Order at least 72 hours before pickup.") inside a fuller answer to a question
                # ("How far ahead do I need to order? At least 72 hours before pickup. ...") is that answer.
                qa = a | words(e.get("_question") or "") | words(e["_heading"] if e.get("_kind") == "dl" else "")
                qb = b | words(o.get("_question") or "") | words(o["_heading"] if o.get("_kind") == "dl" else "")
                for small_set, big_set in ((qa, qb), (qb, qa)):
                    if len(small_set) >= 4 and small_set <= big_set and len(small_set) < len(big_set):
                        twin = o
                        break
                if twin is not None:
                    break
                small, big = sorted((a, b), key=len)
                nums_a, nums_b = set(re.findall(r"\d+", e["_text"])), set(re.findall(r"\d+", o["_text"]))
                same_numbers = bool(nums_a) and nums_a == nums_b
                if a and b and (2 * len(a & b) / (len(a) + len(b)) >= (0.75 if same_numbers else 0.8) or a == b or
                                sa and sb and 2 * len(sa & sb) / (len(sa) + len(sb)) >= 0.8 or
                                len(small) >= 6 and len(small & big) / len(small) >= 0.9):
                    twin = o  # the same statement, or one that says nothing the other does not
                    break
                # The model's short paraphrase of a statement the page section already holds is that statement.
                for m, u, mw, uw in ((e, o, a, b), (o, e, b, a)):
                    if m.get("_model") and not u.get("_model") and mw and len(mw & uw) / len(mw) >= 0.75:
                        twin = o
                        break
                if twin is not None:
                    break
            if twin is None:
                out.append(e)
                continue
            keep, other = (twin, e) if len(twin["_text"]) >= len(e["_text"]) else (e, twin)
            if keep.get("_model") and not other.get("_model"):
                keep, other = other, keep
            if keep is e:
                out[out.index(twin)] = e
            keep["_evs"] += [ev for ev in other["_evs"] if ev not in keep["_evs"]]
            keep["_questions"] += [q for q in other["_questions"] + ([other["_question"]] if other.get("_question") else []) if q not in keep["_questions"]]
            for k, v in other["_facts"].items():
                keep["_facts"].setdefault(k, v)
            keep.setdefault("_fact_quotes", {}).update({k: v for k, v in (other.get("_fact_quotes") or {}).items() if k not in keep.get("_fact_quotes", {})})
            keep["_safety"] = keep["_safety"] or other["_safety"]
            keep["_topic"] = keep.get("_topic") or other.get("_topic")
        return out

    def entries_from(self, e: JsonObj, existing: dict[str, JsonObj]) -> list[JsonObj]:
        """One entry per two spoken sentences: a long answer continues in further entries under the same
        question, so nothing it says is lost to the two-sentence limit."""
        first = self.entry_from(e, existing)
        if first is None:
            return []
        out = [first]
        rest = e["_text"][len(first["answer_spoken"]):].strip() if e["_text"].startswith(first["answer_spoken"]) else ""
        while rest and len(out) < 4:
            chunk = spoken(rest)
            if not chunk or not rest.startswith(chunk):
                break
            sub = {**e, "_text": rest, "_question": e.get("_question"), "_questions": list(e["_questions"]), "_facts": {}}
            entry = self.entry_from(sub, {**existing, **{x["id"]: x for x in out}})
            if entry is None:
                break
            entry.pop("answer_card", None)
            out.append(entry)
            rest = rest[len(chunk):].strip()
        if len(out) > 1:
            first.pop("answer_card", None)
        return out

    def entry_from(self, e: JsonObj, existing: dict[str, JsonObj]) -> JsonObj | None:
        answer = spoken(e["_text"])
        if not answer:
            return None
        heading = e["_heading"]
        topic = e.get("_topic") or snake(heading)
        base = slug(heading) if not e.get("_model") else slug(topic)
        entry_id = base[:64]
        if entry_id in existing:
            entry_id = f"{base[:48]}-{slug(' '.join(list(dict.fromkeys(re.findall(r'[a-z]{4,}', e['_text'].lower())))[:2]))}"[:64]
            n = 2
            while entry_id in existing:
                entry_id = f"{base[:56]}-{n}"
                n += 1
        variants = []
        for q in [e.get("_question"), *e["_questions"]]:
            if q and q not in variants:
                variants.append(q[:120])
        if not e.get("_question") and (e.get("_lead", True) or e.get("_kind") in ("faq", "dl")):
            # The heading asks for the section's first statement only; the rest answer narrower questions.
            variants.append(heading[:120] if len(heading) >= 3 else topic.replace("_", " "))
        first = re.split(r"(?<=[.!?])\s+", e["_text"])[0]
        if 12 <= len(first) <= 120 and first not in variants:
            variants.append(first)
        if not variants:
            variants.append(heading[:120] if len(heading) >= 3 else topic.replace("_", " "))
        entry: JsonObj = {"id": entry_id, "topic": topic[:40] or "general", "question_variants": variants[:8], "answer_spoken": answer,
                          "_heading": heading}
        if len(e["_text"]) > len(answer) + 2:
            entry["answer_card"] = e["_text"][:1000]
        facts = {k: v for k, v in e["_facts"].items()}
        if facts:
            entry["facts"] = facts
        applies = self.applies_to(e)
        if applies:
            entry["applies_to"] = applies[:20]
        entry["provenance"] = {"owner_confirmed": False, "evidence": e["_evs"][:10]}
        return entry

    def applies_to(self, e: JsonObj) -> list[str]:
        """Items an entry is about: those it names, or those listed under the heading it sits under."""
        catalog = getattr(self, "catalog_items", {})
        out = []
        text = f"{e['_heading']} {e['_text']}".lower()
        chain = [h.lower() for h in e.get("_chain") or []] + [e["_heading"].lower()]
        for ref, meta in catalog.items():
            name = meta["name"]
            if len(name) >= 4 and re.search(rf"(?<![a-z]){re.escape(name)}", text):
                out.append(ref)
            elif meta.get("category") and meta["category"] in chain:
                out.append(ref)
            elif meta.get("product_page") == e["_page"]:
                out.append(ref)
        return list(dict.fromkeys(out))

    def declare(self, key: str, value: Any) -> None:
        if key in self.vocab:
            return
        if isinstance(value, list) and value and all(v in DAYS for v in value):
            self.vocab[key] = {"label": key.replace("_", " ")[:120], "type": "multi_enum", "enum": list(DAYS), "claim_class": "availability"}
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
        self.approver_name = approver["spoken_name"]
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
        items = (catalog or {}).get("items", [])
        item_refs = [f"{catalog_id}/{i['id']}" for i in items]
        base_ev = [ev for k in ("custom_orders", "appointments", "quote_requests") for ev in self.offer_evidence(k)]
        info = modules.get("info")
        entries = {f"info/{e['id']}": e for e in (info or {}).get("entries", [])}
        forms = [f for s in self.structures.values() for f in s.forms]

        if ("custom_orders" in kinds and self.template not in ("salon", "home_services")) or (self.template == "bakery" and self.made_to_order):
            refs = [r for r, meta in getattr(self, "catalog_items", {}).items() if meta.get("product_page")] or self.made_to_order
            refs = refs or [r for r in item_refs if re.search(r"cake|custom|order", r)] or item_refs[:1]
            if refs:
                modules["custom-orders"] = self.custom_order_module(refs, catalog, entries)

        if "appointments" in kinds or self.template == "salon":
            if item_refs:
                modules["appointments"] = self.booking_module(catalog_id, items, entries)

        if "quote_requests" in kinds or self.template == "home_services":
            if item_refs:
                modules["quotes"] = self.quote_module(catalog_id, catalog, items, locations, entries, forms, base_ev)

    # ------------------------------------------------------------------ flows: shared pieces

    def contact_fields(self, *, email_required: bool | None) -> list[JsonObj]:
        fields = [
            {"key": "customer_name", "label": "Name", "type": "text", "required": True, "max_length": 80, "pii": True},
            {"key": "customer_phone", "label": "Phone number", "type": "phone", "required": True, "pii": True},
        ]
        if email_required is not None:
            fields.append({"key": "customer_email", "label": "Email", "type": "email", "required": email_required, "pii": True})
        return fields

    def unit_match(self, pattern: str) -> tuple[re.Match[str], JsonObj] | None:
        """The first statement on the site matching `pattern`, with its evidence."""
        for u in getattr(self, "units", []):
            m = re.search(pattern, f"{u['_heading']}: {u['_text']}", re.I)
            if m:
                return m, u["_evs"][0]
        return None

    def say_before_confirm(self, entries: dict[str, JsonObj], patterns: list[str]) -> list[str]:
        """Policy entries a customer hears before confirming (cancellation, deposit, how and when the business
        replies): the first entry whose answer matches each pattern."""
        out = []
        for slot in patterns:
            found: list[str] = []
            for pattern in slot.split(" || "):  # alternatives, most specific first
                found = [r for r, e in entries.items() if re.search(pattern, e["answer_spoken"], re.I) and r not in out]
                if found:
                    break
            found.sort(key=lambda r: (not re.search(pattern, entries[r]["topic"].replace("_", " "), re.I),
                                      -(len(entries[r].get("facts") or {}) + len(re.findall(r"\d+", entries[r]["answer_spoken"])))))
            if found:
                out.append(found[0])
        return out

    def approval_from_items(self, refs_reasons: list[tuple[str, JsonObj]], approver: str, decisions: list[str]) -> JsonObj | None:
        """Items that need a deposit the business takes when it confirms: the owner confirms each such request."""
        if not refs_reasons:
            return None
        ev = refs_reasons[0][1]
        reason = spoken(f"{ev['quote']} I'll send your request to {approver}.") or f"I'll send your request to {approver}."
        rule = {"when": "item", "op": "in", "value": list(dict.fromkeys(r for r, _ in refs_reasons))[:50],
                "reason_spoken": reason, "provenance": self.provenance([ev])}
        return {"module": "owner", "mode": "when_any_rule_matches", "rules": [rule], "decisions": decisions}

    # ------------------------------------------------------------------ custom orders

    def custom_order_module(self, refs: list[str], catalog: JsonObj | None, entries: dict[str, JsonObj]) -> JsonObj:
        approver = self.approver_name
        availability: JsonObj = {}
        ev = self.offer_evidence("custom_orders")
        for key, target in (("notice_minutes", "lead_time_minutes"), ("lead_time_minutes", "lead_time_minutes"), ("capacity_per_day", "capacity_per_day")):
            if key in self.policy_facts and target not in availability and (v := as_int(self.policy_facts[key][0], 1)):
                availability[target] = v
                ev += self.policy_facts[key][1]
        if "lead_time_minutes" not in availability and (hit := self.unit_match(r"\b(?:order|notice)\b[^.]{0,40}?\bat least (\d+|one|two|three|four|five|six|seven) (hours?|days?|weeks?)\b")):
            availability["lead_time_minutes"] = word_number(hit[0].group(1)) * {"hour": 60, "day": 1440, "week": 10080}[hit[0].group(2).rstrip("s")]
            ev.append(hit[1])
        window = self.unit_match(r"\bpick(?:ed)?[ -]?ups?\b[^.]{0,30}?((?:mon|tue|wed|thu|fri|sat|sun)[a-z]* (?:to|through|-|–) (?:mon|tue|wed|thu|fri|sat|sun)[a-z]*)[^.]{0,20}?(\d{1,2}(?::\d{2})?\s?[ap]m)\s*(?:and|to|-|–)\s*(\d{1,2}(?::\d{2})?\s?[ap]m)")
        if window:
            days = quote_days(window[0].group(1))
            opens, closes = to_hhmm(window[0].group(2)), to_hhmm(window[0].group(3))
            if days and opens and closes:
                availability["weekly"] = [{"days": [d for d in DAYS if d in days], "open": opens, "close": closes}]
                ev.append(window[1])
        availability["provenance"] = self.provenance(ev) or self.provenance([self.registry_anchor()])
        fields, groups = self.order_asks(refs, catalog)
        module: JsonObj = {
            "type": "custom_order",
            "title": "Custom orders",
            "items": refs[:50],
            "intake_fields": fields,
            "confirmation": {"before_commit": True, "idempotency": "required",
                             "read_back": ["item", "choices", *[f["key"] for f in fields if not f.get("pii")]][:12]},
            "availability": availability,
            "approval": self.order_approval(refs, catalog, groups) or
                        {"module": "owner", "mode": "always", "decisions": ["approve", "decline", "counter_offer", "request_info"]},
        }
        say = self.say_before_confirm(entries, [r"\bdeposit\b", r"\b(cancel|change)"])
        if say:
            module["confirmation"]["say_before_confirm"] = say
        if self.main_id:
            module["fulfillment"] = {"modes": ["pickup"], "location_ids": [self.main_id], "provenance": availability["provenance"]}
        return module

    def order_asks(self, refs: list[str], catalog: JsonObj | None) -> tuple[list[JsonObj], list[str]]:
        """Intake fields from what the site says to tell it ('Tell us: your pickup date, how many servings, ...').
        Asks that name the item's own choices (size, flavor) are the item's options, not fields."""
        items = {f"{(catalog or {}).get('_id', '')}{i['id']}": i for i in (catalog or {}).get("items", [])}
        group_words: set[str] = set()
        for it in (catalog or {}).get("items", []):
            for g in it.get("options", []):
                group_words |= words(g["name"])
        fields: list[JsonObj] = []
        contact = False
        for s in self.structures.values():
            for ask in s.asks:
                for text in ask["items"]:
                    t = text.lower()
                    w = words(t)
                    if re.search(r"\b(name|phone|number|email|contact)\b", t):
                        contact = True
                        continue
                    if w and w - {"any", "size", "flavor", "filling", "frosting", "extras"} <= group_words | {"any"} | words("and"):
                        continue
                    if w & group_words and re.search(r",| and ", t) and not re.search(r"\b(date|day|when|how many|servings)\b", t):
                        continue
                    label = re.sub(r"^(your|the|any|how many)\s+", "", text.strip(), flags=re.I)
                    label = label[:1].upper() + label[1:]
                    if re.search(r"\b(date|day)\b", t):
                        f = {"key": snake(label), "label": label[:120], "type": "date", "required": True}
                    elif re.search(r"\bhow many\b|\bnumber of\b|\bservings\b|\bguests\b|\bpeople\b", t):
                        f = {"key": snake(label), "label": label[:120], "type": "integer", "required": True, "min": 1}
                    else:
                        f = {"key": snake(label), "label": label[:120], "type": "text", "required": not t.startswith("any"), "max_length": 300}
                        noun = next(iter(sorted(words(label) - {"cake", "order"}, key=len, reverse=True)), "")
                        if noun and (hit := self.unit_match(rf"\b{re.escape(noun)}\w*\b[^.]{{0,25}}up to (\d+) characters")):
                            f["max_length"] = int(hit[0].group(1))
                            f["required"] = False
                        if re.search(r"\bmessage\b|\bnote\b|\binscription\b", t):
                            f["required"] = False
                    if f["key"] not in [x["key"] for x in fields]:
                        fields.append(f)
        if not fields:
            fields = [
                {"key": "pickup_date", "label": "Pickup date", "spoken_prompt": "What day would you like to pick it up?", "type": "date", "required": True},
                {"key": "details", "label": "Order details", "spoken_prompt": "Tell me what you'd like, including size and flavor.", "type": "long_text", "required": True, "max_length": 1000},
            ]
        if "servings" in self.policy_facts or self.servings_threshold():
            if not any(f["type"] == "integer" for f in fields):
                fields.append({"key": "servings", "label": "Number of servings", "type": "integer", "required": True, "min": 1})
        return fields + self.contact_fields(email_required=None), []

    def servings_threshold(self) -> tuple[int, JsonObj] | None:
        if "approval_threshold_servings" in self.policy_facts and (v := as_int(self.policy_facts["approval_threshold_servings"][0], 1)):
            return v, self.policy_facts["approval_threshold_servings"][1][0]
        hit = self.unit_match(r"\b(\d+) servings or more\b[^.]*\bapprov")
        return (int(hit[0].group(1)), hit[1]) if hit else None

    def order_approval(self, refs: list[str], catalog: JsonObj | None, _groups: list[str]) -> JsonObj | None:
        found = self.servings_threshold()
        if not found:
            return None
        threshold, ev = found
        servings_key = "servings"
        rules = [{"when": servings_key, "op": ">=", "value": threshold,
                  "reason_spoken": spoken(f"Orders of {threshold} servings or more need {self.approver_name}'s approval, so I'll send your request to {self.approver_name}.") or "This needs the owner's approval.",
                  "provenance": self.provenance([ev])}]
        big = []
        for it in (catalog or {}).get("items", []):
            ref = f"{self.catalog_id}/{it['id']}"
            if ref not in refs:
                continue
            for g in it.get("options", []):
                for c in g["choices"]:
                    serves = (c.get("attributes") or {}).get("serves")
                    if isinstance(serves, int) and serves >= threshold:
                        big.append(f"{ref}/{g['id']}/{c['id']}")
        if big:
            rules.append({"when": "choices", "op": "in", "value": big[:50],
                          "reason_spoken": spoken(f"That size serves {threshold} or more, so it needs {self.approver_name}'s approval. I'll send your request to {self.approver_name}.") or "This needs the owner's approval.",
                          "provenance": self.provenance([ev])})
        return {"module": "owner", "mode": "when_any_rule_matches", "rules": rules,
                "decisions": ["approve", "decline", "counter_offer", "request_info"]}

    # ------------------------------------------------------------------ bookings

    def booking_module(self, catalog_id: str, items: list[JsonObj], entries: dict[str, JsonObj]) -> JsonObj:
        resources = self.resources()
        by_first = {r["spoken_name"].lower(): r["id"] for r in resources}
        services = []
        for it in items:
            svc: JsonObj = {"item": f"{catalog_id}/{it['id']}"}
            staff = [by_first.get(n.split()[0].lower()) for n in self.item_staff.get(it["id"], [])]
            staff = [x for x in staff if x]
            if staff and len(staff) < len(resources):
                svc["resource_ids"] = list(dict.fromkeys(staff))
            services.append(svc)
        services = [s for s in services if any(i["id"] == s["item"].split("/")[1] and (i.get("duration_minutes") or i.get("options")) for i in items)] or services
        step = as_int(self.policy_facts.get("step_minutes", (None,))[0], 5)
        if step is None and (hit := self.unit_match(r"\bstart(?:s)? every (\d+) minutes\b")):
            step = int(hit[0].group(1))
        step = min(max(step or 15, 5), 480)
        ev = self.offer_evidence("appointments") or [self.registry_anchor()]
        availability: JsonObj = {"provenance": self.provenance(ev)}
        advance = as_int(self.policy_facts.get("max_advance_minutes", (None,))[0], 0)
        if not advance and (hit := self.unit_match(r"\bbook (?:up to )?(\d+|one|two|three|four|six|eight|twelve) (days?|weeks?|months?) (?:ahead|in advance)")):
            advance = word_number(hit[0].group(1)) * {"day": 1440, "week": 10080, "month": 43200}[hit[0].group(2).rstrip("s")]
            availability["provenance"] = self.provenance(ev + [hit[1]])
        if advance:
            availability["max_advance_minutes"] = advance
        email_required = None
        if self.unit_match(r"\bconfirm\w*\b[^.]{0,60}\bby email\b|\bemail\b[^.]{0,40}\bconfirm"):
            email_required = True
        fields = self.contact_fields(email_required=email_required if email_required is not None else False)
        if len(resources) > 1 and self.unit_match(r"\b(request|ask for|choose|pick)\b[^.]{0,30}\b(any|a)\b[^.]{0,20}\b(stylist|barber|therapist|staff|anyone)\b|\bask for anyone by name\b"):
            options = [{"value": r["id"], "label": (r.get("attributes") or {}).get("full_name") or r["spoken_name"]} for r in resources]
            options.append({"value": "any", "label": "No preference"})
            noun = self.staff_noun()
            fields.insert(0, {"key": snake(noun), "label": noun[:1].upper() + noun[1:],
                              "spoken_prompt": f"Would you like a particular {noun}, or the first one available?",
                              "type": "choice", "required": True, "options": options})
        for cond in self.required_conditions():
            fields.append(cond)
        if hit := self.unit_match(r"\b(?:please )?tell us (?:about )?(any [a-z ]+?) when you book\b"):
            label = hit[0].group(1).removeprefix("any ").strip()
            fields.append({"key": snake(label), "label": label[:1].upper() + label[1:120], "type": "long_text", "required": False, "max_length": 300, "pii": True})
        module: JsonObj = {
            "type": "booking",
            "title": "Appointments",
            "services": services,
            "slotting": {"mode": "interval", "step_minutes": step},
            "intake_fields": fields,
            "confirmation": {"before_commit": True, "idempotency": "required", "read_back": ["item", "choices", "when", "customer_name"]},
            "availability": availability,
        }
        say = self.say_before_confirm(entries, [r"\b(cancel|reschedul)", r"\bconfirm\w*\b.*\b(email|within)\b"])
        if say:
            module["confirmation"]["say_before_confirm"] = say
        if resources:
            module["resources"] = resources
        approval = self.approval_from_items(getattr(self, "deposit_items", []), self.approver_name,
                                            ["approve", "decline", "suggest_time", "request_info"])
        if approval:
            module["approval"] = approval
        if self.main_id:
            module["fulfillment"] = {"modes": ["on_site"], "location_ids": [self.main_id], "provenance": availability["provenance"]}
        return module

    def required_conditions(self) -> list[JsonObj]:
        """'A patch test is required if you're new to color with us, or if you've ever reacted to hair dye.':
        the business needs to know whether the condition holds, so it is a yes/no question when booking."""
        out = []
        for u in getattr(self, "units", []):
            m = re.search(r"\b(?:a|an|the) ([a-z -]{3,40}?) is required if (you(?:'re| are|'ve| have)\b[^.]{5,120})", u["_text"].replace("’", "'"), re.I)
            if not m:
                continue
            cond = re.sub(r"\b(if )?you(?:'re| are|'ve| have)\s+", "", m.group(2), flags=re.I).strip(" ,.")
            cond = re.sub(r",?\s+or\s+if\s+", ", or ", cond)
            label = (cond[:1].upper() + cond[1:]).rstrip("?") + "?"
            key = snake(" ".join(re.findall(r"[a-z]+", cond.lower())[:4]))
            if key and all(f["key"] != key for f in out):
                out.append({"key": key, "label": label[:120], "spoken_prompt": spoken(f"Is this you: {label}") or label[:120],
                            "type": "boolean", "required": False})
        return out

    def staff_noun(self) -> str:
        """What the business calls the people customers book ('stylist', 'therapist'): the last word most of
        their roles share."""
        roles = [str(rec.get("role") or "").lower() for _, rec in self.records("people")]
        nouns = [re.findall(r"[a-z]+", r)[-1] for r in roles if re.findall(r"[a-z]+", r)]
        nouns = [n for n in nouns if n not in ("owner", "director", "manager")]
        return max(set(nouns), key=nouns.count) if nouns else "staff member"

    def resources(self) -> list[JsonObj]:
        """Staff who take bookings: the people the site names, with the days each works when it says."""
        out: dict[str, JsonObj] = OrderedDict()
        schedules = {x["name"].lower(): x for s in self.structures.values() for x in s.schedules}
        works = {}
        for page in self.pages:
            for sec in self.structures[page.url].sections:
                if sec["kind"] == "dl" and re.fullmatch(r"(works?|working days|days|in on|available)", sec["question"].strip(), re.I) and sec["headings"]:
                    days = parse_days_text(sec["answer"])
                    if days:
                        works[sec["headings"][0].lower()] = (days, page.url, f"{sec['question']} {sec['answer']}")
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
            res: JsonObj = {"id": rid, "kind": snake(self.staff_noun(), "staff"), "spoken_name": name.split()[0], "provenance": {"owner_confirmed": False, "evidence": [ev]}}
            full = clean_text(rec.get("name"))
            attrs: JsonObj = {}
            if full and len(full.split()) > 1:
                attrs["full_name"] = full
            key = next((k for k in list(schedules) + list(works) if k.split()[0] == name.split()[0].lower() or k == (full or "").lower()), None)
            if key in works:
                days, url, quote = works[key]
                wev = self.locate_ev(url, quote)
                if wev:
                    attrs["works_days"] = days
                    res["provenance"]["evidence"].append(wev)
            elif key in schedules and schedules[key]["days"]:
                sch = schedules[key]
                url = next(u for u, st in self.structures.items() if sch in st.schedules)
                wev = self.locate_ev(url, sch["quote"])
                if wev:
                    attrs["works_days"] = sch["days"]
                    res["provenance"]["evidence"].append(wev)
            if attrs:
                res["attributes"] = attrs
                for k, v in attrs.items():
                    self.declare(k, v)
            out[rid] = res
        return list(out.values())

    # ------------------------------------------------------------------ quote requests

    def quote_module(self, catalog_id: str, catalog: JsonObj | None, items: list[JsonObj], locations: list[JsonObj],
                     entries: dict[str, JsonObj], forms: list[JsonObj], base_ev: list[JsonObj]) -> JsonObj:
        areas = [loc["id"] for loc in locations if loc["kind"] == "service_area"]
        ev = self.offer_evidence("quote_requests") or base_ev or [self.registry_anchor()]
        form = max(forms, key=lambda f: len(f["fields"]), default=None)
        refs = [f"{catalog_id}/{i['id']}" for i in items if i.get("options")] or [f"{catalog_id}/{i['id']}" for i in items]
        fields: list[JsonObj] = []
        windows: list[JsonObj] = []
        if form is not None:
            fields, refs, windows = self.form_fields(form, catalog_id, catalog, refs)
            fev = self.locate_ev(form["url"], form["quote"])
            if fev:
                ev = ev + [fev]
        if not fields:
            fields = [
                {"key": "description", "label": "Describe the job", "spoken_prompt": "What would you like done, and where?", "type": "long_text", "required": True, "max_length": 2000},
                {"key": "zip", "label": "ZIP code", "spoken_prompt": "What's the ZIP code where the work is?", "type": "text", "required": True, "max_length": 10},
                *self.contact_fields(email_required=None),
            ]
        module: JsonObj = {
            "type": "quote_request",
            "title": "Quote requests",
            "items": refs[:50],
            "intake_fields": fields,
            "confirmation": {"before_commit": True, "idempotency": "required",
                             "read_back": [f["key"] for f in fields if not f.get("pii")][:12] or ["item"]},
            "approval": {"module": "owner", "mode": "always", "decisions": ["quote", "request_info", "decline"]},
            "provenance": self.provenance(ev),
        }
        if windows:
            module["visit_windows"] = windows
        say = self.say_before_confirm(entries, [r"\b(don't|do not) book\b|\b(isn't|is not|not) a booking\b || \bnothing is scheduled\b|\bstarts with a (free )?quote",
                                                r"\b(repl(y|ies)|get back|respond)\w*\b[^.]{0,40}\bwithin\b"])
        if say:
            module["confirmation"]["say_before_confirm"] = say
        if areas:
            module["fulfillment"] = {"modes": ["at_customer"], "location_ids": areas, "provenance": self.provenance(ev)}
        return module

    def form_fields(self, form: JsonObj, catalog_id: str, catalog: JsonObj | None, refs: list[str]) -> tuple[list[JsonObj], list[str], list[JsonObj]]:
        """The site's own request form as intake fields: its labels, types, required marks and choices. A
        choice of job types names the catalog items the request is about; times in a choice of time windows
        are visit windows."""
        items = (catalog or {}).get("items", [])
        fields: list[JsonObj] = []
        windows: list[JsonObj] = []
        job_refs: list[str] = []
        for f in form["fields"]:
            key = snake(f["key"])
            label = clean_text(f["label"], 120) or key.replace("_", " ").capitalize()
            field: JsonObj = {"key": key, "label": label, "type": f["type"], "required": bool(f["required"])}
            if f["type"] in ("phone", "email") or key in ("name", "customer_name"):
                field["pii"] = True
            if f.get("max_length"):
                field["max_length"] = f["max_length"]
            if f["type"] == "text" and "max_length" not in field:
                field["max_length"] = 120 if field.get("pii") else 300
            if f["type"] == "long_text":
                field["max_length"] = 2000
            if f["type"] == "choice":
                options = []
                mapped = 0
                for opt in f["options"]:
                    olabel = clean_text(opt["label"], 120) or str(opt["value"])
                    match = next((i for i in items if same_product(olabel, i["name"]) or contains_product(olabel, i["name"]) or contains_product(i["name"], olabel)), None)
                    if match is not None:
                        value = match["id"]
                        mapped += 1
                    else:
                        value = slug(re.sub(r"\(.*?\)", "", olabel)) or slug(olabel)
                    options.append({"value": value[:64], "label": olabel})
                    span = re.search(r"(\d{1,2}(?::\d{2})?\s?[ap]?m?)\s*[–-]\s*(\d{1,2}(?::\d{2})?\s?[ap]m)", olabel)
                    if span:
                        start, end = to_hhmm(span.group(1), span.group(2)), to_hhmm(span.group(2))
                        if start and end:
                            windows.append({"id": value[:64], "spoken_name": spoken_name(re.sub(r"[()]", "", olabel).replace("–", " to ")) or value, "start": start, "end": end})
                if mapped >= max(2, len(options) // 2):
                    # A list of job types: every option is an item the request can be about.
                    for opt in options:
                        ref = f"{catalog_id}/{opt['value']}"
                        if not any(i["id"] == opt["value"] for i in items):
                            self.add_other_item(catalog, opt)
                        job_refs.append(ref)
                field["options"] = options
            fields.append(field)
        names = {"name": "customer_name"}
        for f in fields:
            if f["key"] in names:
                f["key"] = names[f["key"]]
        return fields, job_refs or refs, windows

    def add_other_item(self, catalog: JsonObj | None, opt: JsonObj) -> None:
        """A job type the form offers but the price list does not ('Something else'): quoted on request."""
        if catalog is None:
            return
        form_ev = next((self.locate_ev(f["url"], f["quote"]) for s in self.structures.values() for f in s.forms), None)
        catalog["items"].append({"id": opt["value"], "name": opt["label"], "spoken_name": spoken_name(opt["label"]) or opt["value"],
                                 "price": {"kind": "quote_only"},
                                 "provenance": self.provenance([form_ev or self.registry_anchor()])})

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

    def stale_entries(self, chosen: list[Fact], mentions: list[str]) -> list[str]:
        """Policy entries that repeat a disputed fact (an FAQ answer quoting it, or naming the disputed place):
        withheld with it, and corrected by the owner's answer."""
        out = []
        quotes = [norm_words(f.quote) for f in chosen]
        topics = {f.subject.split(":")[1] for f in chosen if f.subject.startswith("policy:")}
        numbers = [f.value for f in chosen if isinstance(f.value, int) and not isinstance(f.value, bool)]
        for e in getattr(self, "info_entries", []):
            ref = f"info/{e['id']}#answer_spoken"
            texts = [norm_words(ev["quote"]) for ev in e["provenance"]["evidence"]]
            overlap = any(q and t and (q in t or t in q) for q in quotes for t in texts)
            named = any(re.search(rf"(?<![\w]){re.escape(m)}(?![\w])", e["answer_spoken"], re.I) for m in mentions if len(m) >= 4)
            # Another statement of the same policy that gives one of the disputed numbers ("24 hours or more
            # before: no charge") is disputed with it.
            same_number = e["topic"] in topics and any(quote_supports_number(e["answer_spoken"], n) for n in numbers)
            if overlap or named or same_number:
                out.append(ref)
        return out[:8]

    def contradiction(self, facts: list[Fact], prompt: str, why: str, qid: str, value_key: str, shared_target: bool = False,
                      mentions: list[str] | None = None) -> bool:
        """One blocking question from facts on different pages with different values (invariant L10)."""
        chosen: list[Fact] = []
        for fact in facts:
            if all(f.source_url != fact.source_url and f.value != fact.value for f in chosen):
                chosen.append(fact)
        if len(chosen) < 2:
            return False
        targets = list(OrderedDict.fromkeys(t for f in chosen for t in f.targets))
        if not targets or (self.targeted(targets) and not shared_target):
            return False
        targets += [t for t in self.stale_entries(chosen, mentions or []) if t not in targets]
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
                                   f"q-{day.lower()}-close", "close_time", shared_target=True)
            else:
                described = " but ".join(f"your {self.page_label(f.source_url)} page says {f.value}" for f in self.distinct(facts))
                self.contradiction(facts, f"What are your hours on {day}s?",
                                   f"For {day} hours, {described}. Until you answer, the assistant won't give customers {day} hours.",
                                   f"q-{day.lower()}-hours", "hours", shared_target=True)

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
                               f"q-zone-{slug(place)}", "zone", mentions=[place])

    def deterministic_policy_conflicts(self) -> None:
        for prefix in ("policy:", "price:"):
            for subject, facts in self.by_subject(prefix).items():
                # Free text read two ways is usually one fact worded twice; only numbers, yes/no and
                # times are compared mechanically (the model review handles the rest).
                facts = [f for f in facts if isinstance(f.value, int | float | bool) or as_time(f.value) or prefix == "price:"]
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
            if any(f.subject.startswith("price:") for f in facts) and len({f.subject for f in facts}) > 1:
                continue  # two different items, not one price read two ways
            if len({f.key for f in facts}) > 1:
                continue  # different kinds of fact (a cutoff time vs a notice period) cannot contradict
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
        generic = 0
        for page, rec in self.records("gaps"):
            key = canonical_gap_key(snake(rec.get("key"), ""))
            question = clean_text(rec.get("question"), 300)
            if not key or not question or key in getattr(self, "policy_facts", {}):
                continue
            if (key in targets and targets[key] is None) or (key not in targets and (key not in GAP_KEYS or generic >= 3)):
                continue  # the model's free-form "gaps" are mostly noise; keep the known kinds
            ev = self.evidence(page, rec, f"gap {key}")
            if ev is None:
                continue
            if QUANTIFIED.search(ev["quote"]):
                continue  # the page does say how much (a "gap" whose own quote gives the number is not one)
            target = targets.get(key)
            if key in RESPONSE_KEYS and self.response_known:
                continue
            if target is None:
                if "info" not in modules:
                    continue
                target = "info#entries"
                generic += 1
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
            n = 2
            while key in out:
                key = f"web-{page.path_key}"[:60] + f"-{n}"
                n += 1
            source: JsonObj = {"kind": "web_page", "url": page.url, "title": clean_text(page.title) or page.url[:120],
                               "retrieved_at": page.retrieved_at, "content_sha256": page.sha256}
            canaries = self.canaries.get(page.url) or []
            if canaries:
                source["injection_canaries"] = [{"quote": c.quote, "reason": c.reason} for c in canaries]
            out[key] = source
        return out


GAP_KEYS = frozenset({
    "notice_minutes", "lead_time_minutes", "refund_notice_minutes", "change_notice_minutes", "deposit_percent",
    "deposit_cents", "late_cancel_fee_percent", "no_show_fee_percent", "walk_ins_accepted", "travel_fee_cents",
    "min_project_cents", "warranty_days", "arrival_window_minutes",
})
"""Gap keys worth a non-blocking owner question when the model flags them."""


def canonical_gap_key(key: str) -> str:
    if re.search(r"per_day|daily|_limit|capacity", key):
        return "capacity_per_day"
    if re.search(r"response|reply|turnaround", key):
        return "owner_reply_minutes"
    return key


TIME_KEY = re.compile(r"(_start|_end|_time|_opens?|_closes?)$")


def unify_fact_types(entries: Iterable[JsonObj]) -> None:
    """One vocabulary type per fact key (invariant L9): clock-time keys become HH:MM, and a key the
    model filled with mixed types is stored as text everywhere."""
    entries = list(entries)
    kinds: dict[str, set[type]] = {}
    for entry in entries:
        facts = entry.get("facts") or {}
        for key, value in list(facts.items()):
            if TIME_KEY.search(key) and isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2400:
                facts[key] = value = f"{value // 100:02d}:{value % 100:02d}" if value >= 100 else f"{value:02d}:00"
            kinds.setdefault(key, set()).add(bool if isinstance(value, bool) else int if isinstance(value, int) else float if isinstance(value, float) else str)
    for entry in entries:
        facts = entry.get("facts") or {}
        for key, value in list(facts.items()):
            seen = kinds[key]
            if seen == {int, float}:
                facts[key] = float(value)
            elif len(seen) > 1:
                facts[key] = str(value).lower() if isinstance(value, bool) else str(value)


GENERIC_CATEGORY = re.compile(
    r"\b(most (booked|popular|ordered)|what's in|whats in|what we fix|what it costs|our (most|favou?rite)|featured|"
    r"popular|best ?sellers?|highlights|at a glance|sizes and prices|prices?)\b",
    re.I,
)


MAX_ENTRIES = 94
QUANTIFIED = re.compile(r"\b\d+(?:\.\d+)?[\s-]*(?:minutes?|mins?|hours?|hrs?|days?|weeks?|months?)\b|\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b|"
                        r"\b(?:one|two|three|four|five|six|seven|eight|ten|twelve|twenty-four|forty-eight|seventy-two)[\s-]+(?:hours?|days?|weeks?)\b", re.I)
POINTER = re.compile(r"(?:[^.!?\d]{0,25}[.!]\s+|[^.?!]*\?\s*)?(?:please\s+)?(?:see|read|check|visit|find)\b[^.!?\d]*"
                     r"\b(policy|policies|page|below|above|here|details)\b[^.!?\d]*[.!]?", re.I)
WORK_LABEL = re.compile(r"\b(scheduled work|work|jobs?|visits?|service calls?|appointments?|on[- ]site)\b", re.I)
REVIEW_HEADING = re.compile(r"^\s*(customer |client |our |google |guest )?(reviews?|testimonials?|ratings?)\s*$|"
                            r"\bwhat (the |our )?(neighbou?rs|customers|clients|people|guests) (say|are saying)\b|\bkind words\b|^from (our )?customers$", re.I)
OPENING_HOURS = re.compile(r"\b(open|opens|close|closes|closed)\b[^.]{0,30}?\b\d{1,2}(:\d{2})?\s?(am|pm|a\.m\.|p\.m\.)|"
                           r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s*[:–-]?\s*\d{1,2}(:\d{2})?\s?(am|pm)?\s*(–|-|—|to)\s*\d{1,2}(:\d{2})?\s?(am|pm)", re.I)
DAY_AND_TIME = re.compile(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b.{0,40}?\b\d{1,2}(:\d{2})?\s?(am|pm|a\.m\.|p\.m\.)", re.I)
"""Opening times ("we close at 4:00 pm on Saturdays", "Tuesday 7:00 am to 3:00 pm") have one home: the hours."""
SAFETY_WORDS = re.compile(
    r"\b(allerg\w*|ingredients?|contains?|nut-free|nuts?|peanuts?|gluten\w*|celiac|coeliac|latex|fragrance\w*|ppd|"
    r"persulfates?|formaldehyde\w*|lead-safe|asbestos|hazard\w*|sensitivit\w*|reacted)\b",
    re.I,
)


def strip_name(question: str, business: str) -> str:
    """'Can I choose my stylist at Quillfern Hair Studio?' -> 'Can I choose my stylist?': a customer asking the
    business's assistant does not name the business, and the name only dilutes the match."""
    if not business:
        return question
    names = [re.escape(business), re.escape(business.split()[0])]
    for name in names:
        question = re.sub(rf"\s+(?:at|from|with|for|of|in|by)\s+(?:the\s+)?{name}(?:'s)?\b", "", question, flags=re.I)
        question = re.sub(rf"\b{name}(?:'s)?\s+", "", question, flags=re.I)
    return " ".join(question.split())


def norm_words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text).lower()))


def merge_pronoun_units(units: list[str]) -> list[str]:
    """'Gift cards come in any amount.' + 'They never expire.': a unit that starts with a pronoun continues
    the one before it."""
    out: list[str] = []
    for u in units:
        if out and re.match(r"^(they|it|this|these|those|that|she|he|its)\b", u.strip(), re.I) and len(out[-1]) + len(u) < 600:
            out[-1] = f"{out[-1]} {u}"
        else:
            out.append(u)
    return out


WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "twelve": 12}


def word_number(text: str) -> int:
    return int(text) if text.isdigit() else WORD_NUMBERS[text.lower()]


def to_hhmm(text: str, pm_hint: str | None = None) -> str | None:
    """'10:00 am' -> '10:00'; '2 pm' -> '14:00'; '8' with the hint '12 pm' -> '08:00'."""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s?([ap])?\.?m?\.?\s*", text.lower())
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap is None and pm_hint:
        hint = re.search(r"([ap])m", pm_hint.lower())
        ap = "a" if hint and hint.group(1) == "p" and h > 7 and h < 12 else (hint.group(1) if hint else None)
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def parse_days_text(text: str) -> list[str]:
    from askonce_onboarding.structure import parse_days

    return parse_days(text)


def postal_in_range(code: str, quote: str) -> bool:
    """'80002–80005' prints 80003 too."""
    if not code.isdigit():
        return False
    for a, b in re.findall(r"(\d{4,6})\s*[–-]\s*(\d{4,6})", quote):
        if len(a) == len(code) and int(a) <= int(code) <= int(b):
            return True
    return False


def base_place(name: str) -> str:
    """'Lakewood (northeast)' is printed as 'Lakewood (northeast)'; match on the name before the bracket."""
    return re.split(r"\s*\(", name)[0].strip()


def has_price_only(text: str) -> bool:
    return bool(re.fullmatch(r"\s*(from\s+)?[$₹]\s?[\d,.]+(\s*[-–]\s*[$₹]?[\d,.]+)?(\s+per\s+\w+)?\s*", text, re.I))


def parse_duration_only(text: str) -> bool:
    return bool(re.fullmatch(r"\s*(\d+\s*(hrs?|hours?|min(ute)?s?)\s*)+((to|-|–)\s*(\d+\s*(hrs?|hours?|min(ute)?s?)\s*)+)?(,.*)?", text, re.I))


def last_per(text: str) -> str | None:
    from askonce_onboarding.structure import PER_WORDS

    found = re.findall(r"\bper\s+(hr|hour|item|visit|person|dozen|slice|valve|tier|cake|night|session|class|job)\b", text.lower())
    return PER_WORDS[found[-1]] if found else None


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

