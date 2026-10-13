"""Spoken formatting. Pure functions; every output must pass `schemas.check_spoken`.

Conventions (en-US first; the profile `locale` selects variants later):
  money        4800 USD -> "$48", 450 -> "$4.50", from -> "from $48", range -> "$80 to $120"
  dates        2026-10-17 (relative to today in the business tz) -> "today", "tomorrow",
               "Saturday", or "Saturday, October 17" beyond 6 days
  times        10:00 -> "10 am", 13:30 -> "1:30 pm", 12:00 -> "noon"
  lists        ["a","b","c"] -> "a, b and c"
  allergens    tree_nuts -> "tree nuts", soybeans -> "soy"; dietary gluten_free -> "gluten-free"
  hours        weekly lines -> "Tuesday to Saturday, 7 am to 6 pm"
Speech never contains URLs, ids, refs or markup; truncation happens at sentence boundaries.

Inputs may be raw profile JSON (mappings) or pydantic models with the same field names (`PriceQuote`,
`PriceView`): `price()` reads either.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from typing import Any

from askonce_server.profile.model import JsonObj

SPEAK_LIMIT = 240

WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
"""Profile weekday tokens in `date.weekday()` order."""

DAY_NAMES: dict[str, str] = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}

_CURRENCY_SYMBOLS: dict[str, str] = {"USD": "$", "CAD": "$", "AUD": "$", "NZD": "$", "EUR": "€", "GBP": "£"}

_ALLERGENS: dict[str, str] = {
    "milk": "milk",
    "eggs": "eggs",
    "fish": "fish",
    "shellfish": "shellfish",
    "tree_nuts": "tree nuts",
    "peanuts": "peanuts",
    "wheat": "wheat",
    "soybeans": "soy",
    "sesame": "sesame",
}

_DIETARY: dict[str, str] = {
    "gluten_free": "gluten-free",
    "vegan": "vegan",
    "vegetarian": "vegetarian",
    "dairy_free": "dairy-free",
    "egg_free": "egg-free",
    "nut_free": "nut-free",
    "peanut_free": "peanut-free",
    "soy_free": "soy-free",
    "sugar_free": "sugar-free",
    "halal": "halal",
    "kosher": "kosher",
}

_PER_SILENT = frozenset({"each", "item_each"})

# Characters `check_spoken` rejects (same class as the profile schema's `spoken` pattern).
_MARKUP = re.compile(r"[<>{}\[\]|*#~`\\]")
_URL = re.compile(r"(https?:\S*|www\.\S*)", re.IGNORECASE)
_SPACES = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


# ---------------------------------------------------------------------------- cleaning


def clean(text: str) -> str:
    """Make arbitrary text speakable: no URLs, markup, underscores or newlines; NFC; single spaces.

    The building block of `sentences` and `guard.sanitize_speak`. Never raises; may return "".
    """
    out = unicodedata.normalize("NFC", text)
    out = _URL.sub(" ", out)
    out = out.replace("_", " ")
    out = _MARKUP.sub(" ", out)
    return _SPACES.sub(" ", out).strip()


def _sentence(text: str) -> str:
    """A cleaned sentence that ends with terminal punctuation."""
    out = clean(text)
    if out and out[-1] not in ".!?":
        out += "."
    return out


def capitalize(text: str) -> str:
    """Upper-case the first character only (spoken names stay as written otherwise)."""
    return text[:1].upper() + text[1:] if text else text


def truncate(text: str, limit: int = SPEAK_LIMIT) -> str:
    """Cap `text` at `limit` characters on a word boundary, ending with an ellipsis."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"


def sentences(*parts: str | None, limit: int = SPEAK_LIMIT, tail: str | None = None) -> str:
    """Join non-empty sentences, dropping trailing ones that would exceed `limit`.

    Each part is cleaned (see `clean`) and gets terminal punctuation. If even the first sentence is too
    long it is truncated on a word boundary. `tail` (e.g. "Want me to ask Rosa?") is always kept: room
    is reserved for it before the other parts are fitted. Returns "" only when every part is empty.
    """
    if tail:
        end = _sentence(tail)
        if end:
            head = sentences(*parts, limit=max(1, limit - len(end) - 1))
            return f"{head} {end}" if head else end[:limit]
    out = ""
    for part in parts:
        if not part:
            continue
        piece = _sentence(part)
        if not piece:
            continue
        candidate = f"{out} {piece}" if out else piece
        if len(candidate) > limit:
            if not out:
                return truncate(piece, limit)
            break
        out = candidate
    return out


def split_sentences(text: str) -> list[str]:
    """Split spoken text into sentences (used when a long verbatim answer must be shortened)."""
    return [s for s in _SENTENCE_END.split(text.strip()) if s]


# ---------------------------------------------------------------------------- money


def _field(obj: Any, key: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return obj.get(key)
    return getattr(obj, key, None)


def money(amount_cents: int, currency: str) -> str:
    """4800 USD -> "$48"; 450 -> "$4.50"; 120000 -> "$1,200"; unknown currency -> "48 CHF"."""
    sign = "minus " if amount_cents < 0 else ""
    cents = abs(int(amount_cents))
    whole, frac = divmod(cents, 100)
    number = f"{whole:,}" if frac == 0 else f"{whole:,}.{frac:02d}"
    symbol = _CURRENCY_SYMBOLS.get(currency.upper())
    if symbol is not None:
        return f"{sign}{symbol}{number}"
    return f"{sign}{number} {currency.upper()}"


def _per(per: Any) -> str:
    if not per or str(per) in _PER_SILENT:
        return ""
    return " per " + str(per).replace("_", " ")


def price(price: JsonObj | Any | None) -> str | None:
    """A profile `price` object as speech, or None for quote_only / missing.

    fixed -> "$48", from -> "from $48", range -> "$80 to $120", free -> "free"; a `per` other than
    "each" is appended ("$33 per dozen").
    """
    if price is None:
        return None
    kind = _field(price, "kind")
    currency = _field(price, "currency") or "USD"
    amount = _field(price, "amount_cents")
    per = _per(_field(price, "per"))
    if kind == "free":
        return "free"
    if kind == "quote_only" or amount is None:
        return None
    if kind == "fixed":
        return money(amount, currency) + per
    if kind == "from":
        return "from " + money(amount, currency) + per
    if kind == "range":
        top = _field(price, "max_cents")
        if top is None:
            return "from " + money(amount, currency) + per
        return f"{money(amount, currency)} to {money(top, currency)}{per}"
    return None


def price_delta(delta: JsonObj | Any | None) -> str | None:
    """An option's `price_delta` as speech: fixed 1200 per tier -> "$12 more per tier", a range ->
    "$10 to $20 more"; None for anything without a spoken amount (free, quote only, from)."""
    if delta is None:
        return None
    kind = _field(delta, "kind")
    currency = _field(delta, "currency") or "USD"
    amount = _field(delta, "amount_cents")
    per = _per(_field(delta, "per"))
    if amount is None or kind not in ("fixed", "range"):
        return None
    top = _field(delta, "max_cents") if kind == "range" else None
    if top is not None:
        return f"{money(amount, currency)} to {money(top, currency)} more{per}"
    if amount < 0:
        return f"{money(-amount, currency)} less{per}"
    return f"{money(amount, currency)} more{per}"


# ---------------------------------------------------------------------------- dates and times


def day(value: date, *, today: date | None = None) -> str:
    """'today', 'tomorrow', a weekday within 6 days, else 'Saturday, October 17'.

    With `today=None` the absolute form is always used, without a year (confirmation read-backs must
    not change wording at midnight, or the SDK discards the customer's answer). With `today`, the year
    is added for dates more than 300 days away.
    """
    absolute = f"{DAY_NAMES[WEEKDAYS[value.weekday()]]}, {value.strftime('%B')} {value.day}"
    if today is None:
        return absolute
    delta = (value - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if 1 < delta <= 6:
        return DAY_NAMES[WEEKDAYS[value.weekday()]]
    if abs(delta) > 300:
        return f"{absolute}, {value.year}"
    return absolute


def clock_time(value: time | datetime) -> str:
    """10:00 -> '10 am', 13:30 -> '1:30 pm', 12:00 -> 'noon', 00:00 -> 'midnight'."""
    hour, minute = value.hour, value.minute
    if minute == 0 and hour == 12:
        return "noon"
    if minute == 0 and hour == 0:
        return "midnight"
    suffix = "am" if hour < 12 else "pm"
    h12 = hour % 12 or 12
    return f"{h12} {suffix}" if minute == 0 else f"{h12}:{minute:02d} {suffix}"


def hhmm(text: str) -> str:
    """A profile `HH:MM` (24h, `24:00` allowed) as speech."""
    hour, minute = (int(p) for p in text.split(":", 1))
    if hour == 24:
        return "midnight"
    return clock_time(time(hour, minute))


def when(start: datetime, *, today: date | None = None) -> str:
    """'Saturday at 10 am' (relative to `today`), or 'Saturday, October 17 at 10 am' when `today` is None."""
    return f"{day(start.date(), today=today)} at {clock_time(start)}"


def duration(minutes: int) -> str:
    """45 -> '45 minutes', 60 -> '1 hour', 90 -> '1 hour 30 minutes', 2880 -> '2 days'."""
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} minute" + ("" if minutes == 1 else "s")
    if minutes % 1440 == 0 and minutes >= 2880:
        return f"{minutes // 1440} days"
    hours, rest = divmod(minutes, 60)
    head = f"{hours} hour" + ("" if hours == 1 else "s")
    return head if rest == 0 else f"{head} {rest} minutes"


# ---------------------------------------------------------------------------- lists and tokens


def join(items: Sequence[str], *, conjunction: str = "and") -> str:
    """['a'] -> 'a'; ['a','b'] -> 'a and b'; ['a','b','c'] -> 'a, b and c'."""
    parts = [i for i in items if i]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return f"{', '.join(parts[:-1])} {conjunction} {parts[-1]}"


def allergen(token: str) -> str:
    return _ALLERGENS.get(token, token.replace("_", " "))


def dietary(token: str) -> str:
    if token in _DIETARY:
        return _DIETARY[token]
    if token.endswith("_free"):
        return token[: -len("_free")].replace("_", " ") + "-free"
    return token.replace("_", " ")


def digits(code: str, *, group: int = 4) -> str:
    """A code read digit by digit in groups: '04973073' -> '0 4 9 7, 3 0 7 3' (a voice host reads
    'zero four nine seven, three zero seven three'). Non-digits are ignored."""
    only = re.sub(r"[^0-9]", "", code)
    return ", ".join(" ".join(only[i : i + group]) for i in range(0, len(only), group))


def phone(e164: str) -> str:
    """'+15035550147' -> '503-555-0147' (NANP); other numbers are spoken digit groups as stored."""
    digits = re.sub(r"\D", "", e164)
    if e164.startswith("+1") and len(digits) == 11:
        return f"{digits[1:4]}-{digits[4:7]}-{digits[7:]}"
    return e164.lstrip("+")


# ---------------------------------------------------------------------------- hours


def _day_runs(days: Sequence[str]) -> list[str]:
    """Group weekday tokens into runs: ['tue','wed','thu','sat'] -> ['Tuesday to Thursday', 'Saturday']."""
    indices = sorted({WEEKDAYS.index(d) for d in days if d in WEEKDAYS})
    runs: list[list[int]] = []
    for idx in indices:
        if runs and idx == runs[-1][-1] + 1:
            runs[-1].append(idx)
        else:
            runs.append([idx])
    out: list[str] = []
    for run in runs:
        first, last = DAY_NAMES[WEEKDAYS[run[0]]], DAY_NAMES[WEEKDAYS[run[-1]]]
        if len(run) == 1:
            out.append(first)
        elif len(run) == 2:
            out.extend([first, last])
        else:
            out.append(f"{first} to {last}")
    return out


def intervals_by_day(weekly: Sequence[JsonObj]) -> dict[str, list[tuple[str, str]]]:
    """Weekday token -> sorted (open, close) `HH:MM` pairs. Days without a line are absent (closed)."""
    by_day: dict[str, list[tuple[str, str]]] = {}
    for line in weekly:
        for token in line.get("days", ()):
            by_day.setdefault(str(token), []).append((str(line["open"]), str(line["close"])))
    return {d: sorted(v) for d, v in by_day.items()}


def interval(open_: str, close: str) -> str:
    return f"{hhmm(open_)} to {hhmm(close)}"


def hours(weekly: Sequence[JsonObj]) -> str:
    """'Tuesday to Friday, 7 am to 3 pm; Saturday, 8 am to 6 pm'. Split shifts: '9 am to noon and 1 pm to 5 pm'."""
    by_day = intervals_by_day(weekly)
    groups: list[tuple[tuple[tuple[str, str], ...], list[str]]] = []
    for token in WEEKDAYS:
        spans = tuple(by_day.get(token, ()))
        if not spans:
            continue
        for key, days in groups:
            if key == spans:
                days.append(token)
                break
        else:
            groups.append((spans, [token]))
    parts = []
    for spans, days in groups:
        parts.append(f"{join(_day_runs(days))}, {join([interval(o, c) for o, c in spans])}")
    return "; ".join(parts)
