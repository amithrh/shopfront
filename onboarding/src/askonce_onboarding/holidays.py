"""Dates for 'closed on Thanksgiving Day, December 25 and January 1': the next time each one falls.

A website names its holiday closures without years ("We're closed on Thanksgiving Day"). The profile
needs dates, and the model guesses them (badly: one run put Thanksgiving 2026 on the 28th). Here each
closure the page names is resolved in code: a printed date ("November 26, 2026", "December 24 and 25")
is read as written, and a named public holiday is computed for the business's country, taking the
next occurrence on or after the day the page was read.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable

MONTHS = {m: i + 1 for i, m in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"))}
MONTH_RX = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


def _month(text: str) -> int:
    t = text.lower()
    return next(n for name, n in MONTHS.items() if name.startswith(t[:3]))


def nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The n-th `weekday` (0 = Monday) of a month; n = -1 is the last one."""
    if n > 0:
        first = dt.date(year, month, 1)
        return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def easter(year: int) -> dt.date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    day = (h + m - 7 * n + 114) % 31 + 1
    return dt.date(year, month, day)


Rule = Callable[[int], dt.date]
FIXED = lambda m, d: (lambda y: dt.date(y, m, d))  # noqa: E731

HOLIDAYS: dict[str, list[tuple[str, Rule]]] = {
    # name pattern -> per-country rule ("*" = every country)
    r"new year'?s day": [("*", FIXED(1, 1))],
    r"new year'?s eve": [("*", FIXED(12, 31))],
    r"christmas eve": [("*", FIXED(12, 24))],
    r"christmas(?: day)?": [("*", FIXED(12, 25))],
    r"boxing day": [("*", FIXED(12, 26))],
    r"thanksgiving(?: day)?": [("US", lambda y: nth_weekday(y, 11, 3, 4)), ("CA", lambda y: nth_weekday(y, 10, 0, 2))],
    r"memorial day": [("US", lambda y: nth_weekday(y, 5, 0, -1))],
    r"independence day|fourth of july|4th of july|july 4th": [("US", FIXED(7, 4)), ("IN", FIXED(8, 15))],
    r"labou?r day": [("US", lambda y: nth_weekday(y, 9, 0, 1)), ("CA", lambda y: nth_weekday(y, 9, 0, 1))],
    r"veterans day": [("US", FIXED(11, 11))],
    r"martin luther king(?: jr\.?)?(?: day)?|mlk day": [("US", lambda y: nth_weekday(y, 1, 0, 3))],
    r"presidents'? day": [("US", lambda y: nth_weekday(y, 2, 0, 3))],
    r"juneteenth": [("US", FIXED(6, 19))],
    r"good friday": [("*", lambda y: easter(y) - dt.timedelta(days=2))],
    r"easter monday": [("*", lambda y: easter(y) + dt.timedelta(days=1))],
    r"easter sunday|easter": [("*", easter)],
    r"australia day": [("AU", FIXED(1, 26))],
    r"anzac day": [("AU", FIXED(4, 25)), ("NZ", FIXED(4, 25))],
    r"waitangi day": [("NZ", FIXED(2, 6))],
    r"republic day": [("IN", FIXED(1, 26))],
    r"gandhi jayanti": [("IN", FIXED(10, 2))],
    r"canada day": [("CA", FIXED(7, 1))],
}


def next_on_or_after(rule: Rule, ref: dt.date) -> dt.date:
    for year in (ref.year, ref.year + 1):
        day = rule(year)
        if day >= ref:
            return day
    return rule(ref.year + 1)


def closure_dates(text: str, *, country: str, ref: dt.date) -> list[tuple[dt.date, dt.date, str]]:
    """Every closure a sentence names, as (first day, last day, name), in the order written."""
    found: list[tuple[int, dt.date, dt.date, str]] = []
    taken: list[tuple[int, int]] = []
    low = text.lower().replace("’", "'")
    for pattern, rules in HOLIDAYS.items():
        rule = next((r for c, r in rules if c == country), None) or next((r for c, r in rules if c == "*"), None)
        if rule is None:
            continue
        for m in re.finditer(rf"\b(?:{pattern})\b", low):
            if any(a <= m.start() < b for a, b in taken):
                continue
            day = next_on_or_after(rule, ref)
            name = text[m.start():m.end()]
            found.append((m.start(), day, day, name[:1].upper() + name[1:]))
            taken.append((m.start(), m.end()))
    # Printed dates: "November 26, 2026", "December 24 and 25", "January 1".
    for m in re.finditer(rf"\b{MONTH_RX}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?((?:\s*(?:,|and|&|-|–|to)\s*\d{{1,2}}(?:st|nd|rd|th)?)*)(?:,?\s+(\d{{4}}))?", low):
        if any(a <= m.start() < b for a, b in taken):
            continue
        month = _month(m.group(1))
        days = [int(m.group(2))] + [int(x) for x in re.findall(r"\d{1,2}", m.group(3) or "")]
        year = int(m.group(4)) if m.group(4) else None
        try:
            first = dt.date(year or ref.year, month, days[0])
            last = dt.date(year or ref.year, month, days[-1])
        except ValueError:
            continue
        if year is None and last < ref:
            first, last = first.replace(year=ref.year + 1), last.replace(year=ref.year + 1)
        if last < first:
            continue
        span = re.search(r"\b(and|&|-|–|to)\b|[-–]", m.group(3) or "")
        if len(days) > 1 and not span:
            continue
        found.append((m.start(), first, last, ""))
        taken.append((m.start(), m.end()))
    found.sort()
    out, seen = [], set()
    for _, first, last, name in found:
        if first in seen:
            continue
        seen.add(first)
        out.append((first, last, name))
    return out


def holiday_name(day: dt.date, country: str) -> str:
    """The public holiday that falls on `day`, if any ("Christmas Day" for December 25)."""
    for pattern, rules in HOLIDAYS.items():
        rule = next((r for c, r in rules if c == country), None) or next((r for c, r in rules if c == "*"), None)
        if rule is not None and rule(day.year) == day:
            name = pattern.split("|")[0].replace("(?: day)?", " day").replace("'?", "'")
            name = re.sub(r"\(\?:[^)]*\)\??", "", name).strip()
            return " ".join(w[:1].upper() + w[1:] for w in name.split())
    return ""
