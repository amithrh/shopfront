"""Spoken formatting (`tools.speech`): every formatter's output is plain, spoken-safe English."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from askonce_server.domain.models import PriceQuote
from askonce_server.tools import speech
from askonce_server.tools.schemas import check_spoken

TODAY = date(2026, 10, 14)  # a Wednesday


@pytest.mark.parametrize(
    ("cents", "currency", "expected"),
    [
        (4800, "USD", "$48"),
        (450, "USD", "$4.50"),
        (5, "USD", "$0.05"),
        (120000, "USD", "$1,200"),
        (0, "USD", "$0"),
        (1999, "EUR", "€19.99"),
        (4800, "CHF", "48 CHF"),
        (-500, "USD", "minus $5"),
    ],
)
def test_money(cents: int, currency: str, expected: str) -> None:
    assert speech.money(cents, currency) == expected


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ({"kind": "fixed", "amount_cents": 4800, "currency": "USD"}, "$48"),
        ({"kind": "fixed", "amount_cents": 4800, "currency": "USD", "per": "each"}, "$48"),
        ({"kind": "fixed", "amount_cents": 3300, "currency": "USD", "per": "dozen"}, "$33 per dozen"),
        ({"kind": "from", "amount_cents": 5600, "currency": "USD"}, "from $56"),
        ({"kind": "range", "amount_cents": 8000, "max_cents": 12000, "currency": "USD"}, "$80 to $120"),
        ({"kind": "range", "amount_cents": 12500, "max_cents": 14500, "currency": "USD", "per": "hour"}, "$125 to $145 per hour"),
        ({"kind": "free"}, "free"),
        ({"kind": "quote_only"}, None),
        (None, None),
    ],
)
def test_price_from_profile_objects(price: dict[str, object] | None, expected: str | None) -> None:
    assert speech.price(price) == expected


def test_price_reads_domain_models() -> None:
    quote = PriceQuote(kind="from", currency="USD", amount_cents=17500)
    assert speech.price(quote) == "from $175"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 10, 14), "today"),
        (date(2026, 10, 15), "tomorrow"),
        (date(2026, 10, 17), "Saturday"),
        (date(2026, 10, 20), "Tuesday"),
        (date(2026, 10, 21), "Wednesday, October 21"),
        (date(2026, 10, 10), "Saturday, October 10"),
        (date(2027, 10, 1), "Friday, October 1, 2027"),
    ],
)
def test_day_relative(value: date, expected: str) -> None:
    assert speech.day(value, today=TODAY) == expected


def test_day_absolute_without_today_is_stable() -> None:
    assert speech.day(date(2026, 10, 18)) == "Sunday, October 18"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (time(10, 0), "10 am"),
        (time(13, 30), "1:30 pm"),
        (time(12, 0), "noon"),
        (time(0, 0), "midnight"),
        (time(0, 15), "12:15 am"),
        (time(12, 5), "12:05 pm"),
    ],
)
def test_clock_time(value: time, expected: str) -> None:
    assert speech.clock_time(value) == expected


def test_hhmm_allows_24_00() -> None:
    assert speech.hhmm("24:00") == "midnight"
    assert speech.hhmm("07:30") == "7:30 am"


def test_when() -> None:
    start = datetime(2026, 10, 17, 10, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert speech.when(start, today=TODAY) == "Saturday at 10 am"
    assert speech.when(start) == "Saturday, October 17 at 10 am"


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(1, "1 minute"), (45, "45 minutes"), (60, "1 hour"), (90, "1 hour 30 minutes"), (1440, "24 hours"), (2880, "2 days")],
)
def test_duration(minutes: int, expected: str) -> None:
    assert speech.duration(minutes) == expected


@pytest.mark.parametrize(
    ("items", "expected"),
    [([], ""), (["a"], "a"), (["a", "b"], "a and b"), (["a", "b", "c"], "a, b and c"), (["a", "", "c"], "a and c")],
)
def test_join(items: list[str], expected: str) -> None:
    assert speech.join(items) == expected


def test_join_with_or() -> None:
    assert speech.join(["tea", "coffee"], conjunction="or") == "tea or coffee"


@pytest.mark.parametrize(
    ("token", "expected"),
    [("tree_nuts", "tree nuts"), ("soybeans", "soy"), ("milk", "milk"), ("sesame", "sesame"), ("new_thing", "new thing")],
)
def test_allergen(token: str, expected: str) -> None:
    assert speech.allergen(token) == expected


@pytest.mark.parametrize(
    ("token", "expected"),
    [("gluten_free", "gluten-free"), ("vegan", "vegan"), ("dairy_free", "dairy-free"), ("low_carb_free", "low carb-free"), ("keto_friendly", "keto friendly")],
)
def test_dietary(token: str, expected: str) -> None:
    assert speech.dietary(token) == expected


def test_phone() -> None:
    assert speech.phone("+15035550147") == "503-555-0147"
    assert speech.phone("+442071234567") == "442071234567"


def test_hours_groups_days_and_split_shifts() -> None:
    weekly = [
        {"days": ["tue", "wed", "thu", "fri"], "open": "07:00", "close": "15:00"},
        {"days": ["sat"], "open": "08:00", "close": "18:00"},
        {"days": ["sun"], "open": "08:00", "close": "14:00"},
    ]
    assert speech.hours(weekly) == "Tuesday to Friday, 7 am to 3 pm; Saturday, 8 am to 6 pm; Sunday, 8 am to 2 pm"
    split = [
        {"days": ["mon", "wed"], "open": "09:00", "close": "12:00"},
        {"days": ["mon", "wed"], "open": "13:00", "close": "17:00"},
        {"days": ["sat"], "open": "10:00", "close": "24:00"},
    ]
    assert speech.hours(split) == "Monday and Wednesday, 9 am to noon and 1 pm to 5 pm; Saturday, 10 am to midnight"


def test_sentences_drops_trailing_parts_that_do_not_fit() -> None:
    long_part = "word " * 30
    out = speech.sentences("First one", long_part, "Second", limit=60)
    assert out == "First one."
    assert speech.sentences(None, "", "Hello") == "Hello."
    assert speech.sentences() == ""


def test_sentences_keeps_the_tail() -> None:
    out = speech.sentences("x" * 100, "y" * 100, tail="Want me to ask Rosa?", limit=140)
    assert out.endswith("Want me to ask Rosa?")
    assert len(out) <= 140


def test_sentences_truncates_an_overlong_first_sentence_on_a_word_boundary() -> None:
    out = speech.sentences("lorem ipsum " * 40)
    assert len(out) <= speech.SPEAK_LIMIT
    assert out.endswith("…")
    check_spoken(out)


@pytest.mark.parametrize(
    "dirty",
    [
        "See https://example.com/menu for details",
        "Visit www.example.com today",
        "Line one\nline two",
        "**Bold** and <b>tags</b> and `code`",
        "gluten_free | vegan # ~",
    ],
)
def test_clean_makes_text_speakable(dirty: str) -> None:
    out = speech.sentences(dirty)
    check_spoken(out)
    assert "http" not in out and "www." not in out and "_" not in out


def test_every_formatter_output_passes_check_spoken() -> None:
    samples = [
        speech.money(123456, "USD"),
        speech.price({"kind": "range", "amount_cents": 8000, "max_cents": 12000, "currency": "USD", "per": "per_person"}),
        speech.day(date(2026, 12, 25), today=TODAY),
        speech.when(datetime(2026, 10, 15, 13, 30)),
        speech.duration(135),
        speech.join([speech.allergen(a) for a in ("tree_nuts", "soybeans", "peanuts")]),
        speech.join([speech.dietary(t) for t in ("gluten_free", "dairy_free", "nut_free")]),
        speech.hours([{"days": ["mon", "tue", "wed", "thu", "fri"], "open": "07:30", "close": "17:30"}]),
        speech.phone("+17205550142"),
    ]
    for sample in samples:
        assert sample
        check_spoken(sample)


def test_round2_misc_low_digits_are_read_one_by_one_in_groups() -> None:
    assert speech.digits("04973073") == "0 4 9 7, 3 0 7 3"
    assert speech.digits("0497-3073") == "0 4 9 7, 3 0 7 3"
    assert speech.digits("123456", group=3) == "1 2 3, 4 5 6"
    check_spoken(speech.sentences(f"Your booking code is {speech.digits('04973073')}"))
