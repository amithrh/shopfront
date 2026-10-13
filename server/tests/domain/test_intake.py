"""Intake normalisation: one table per field type, plus the spec conversion and the batch rules."""

from __future__ import annotations

from datetime import date, time
from typing import Any

import pytest

from askonce_server.domain.intake import (
    country_calling_code,
    field_specs,
    nonce_payload,
    normalize_intake,
    normalize_phone,
    normalize_value,
)
from askonce_server.domain.planning import IntakeFieldSpec
from askonce_server.profile.model import Profile

US = "en-US"


def spec(type_: str, **kwargs: Any) -> IntakeFieldSpec:
    return IntakeFieldSpec(key=kwargs.pop("key", "f"), label=kwargs.pop("label", "Field"), type=type_, **kwargs)  # type: ignore[arg-type]


CHOICE = spec("choice", options=(("weekday-morning", "Weekday morning"), ("first-available", "First available")))
MULTI = spec("multi_choice", options=(("text", "Text"), ("email", "Email"), ("phone", "Phone call")))

VALID: list[tuple[IntakeFieldSpec, Any, Any]] = [
    (spec("text", max_length=30), "  Happy\n  birthday   Sam ", "Happy birthday Sam"),
    (spec("text"), "Café", "Café"),  # NFC
    (spec("text"), 42, "42"),
    (spec("long_text", max_length=300), "line one\n\nline two", "line one line two"),
    (spec("integer", min=1), "30", 30),
    (spec("integer", min=1), 30.0, 30),
    (spec("integer", min=1, max=100), " 7 ", 7),
    (spec("number", min=0), "2.5", 2.5),
    (spec("number"), 3, 3.0),
    (spec("boolean"), "yes", True),
    (spec("boolean"), "No", False),
    (spec("boolean"), "true", True),
    (spec("boolean"), 0, False),
    (spec("boolean"), True, True),
    (CHOICE, "weekday-morning", "weekday-morning"),
    (CHOICE, "WEEKDAY MORNING", "weekday-morning"),
    (CHOICE, "first available", "first-available"),
    (MULTI, ["Email", "text", "email"], ["email", "text"]),
    (MULTI, "phone call, text", ["phone", "text"]),
    (spec("date"), "2026-10-18", "2026-10-18"),
    (spec("date"), date(2026, 10, 18), "2026-10-18"),
    (spec("time"), "9:05", "09:05"),
    (spec("time"), "14:30:00", "14:30"),
    (spec("time"), time(7, 0), "07:00"),
    (spec("email", pii=True), " Sam.Lee@Example.COM ", "sam.lee@example.com"),
    (spec("phone", pii=True), "(217) 555-0100", "+12175550100"),
    (spec("phone", pii=True), "217.555.0100", "+12175550100"),
    (spec("phone", pii=True), "1 217 555 0100", "+12175550100"),
    (spec("phone", pii=True), "+1 217 555 0100", "+12175550100"),
    (spec("phone", pii=True), "+44 20 7946 0958", "+442079460958"),
    (spec("phone", pii=True), "0044 20 7946 0958", "+442079460958"),
    (spec("phone", pii=True), 2175550100, "+12175550100"),
    (spec("money", min=0), 120, 12000),
    (spec("money", min=0), "120.5", 12050),
    (spec("money", min=0), "$1,240.99", 124099),
    (spec("money", min=0), 0.1, 10),
]

INVALID: list[tuple[IntakeFieldSpec, Any, str]] = [
    (spec("text", max_length=5), "too long for this", "at most 5"),
    (spec("text"), {"a": 1}, "expected text"),
    (spec("integer", min=1), "2.5", "whole number"),
    (spec("integer", min=1), 0, "at least 1"),
    (spec("integer", max=10), 11, "at most 10"),
    (spec("integer"), "thirty", "number"),
    (spec("integer"), True, "number"),
    (spec("number"), "nan", "finite"),
    (spec("boolean"), "maybe", "yes or no"),
    (CHOICE, "saturday morning", "listed options"),
    (MULTI, ["text", "fax"], "listed options"),
    (spec("date"), "18/10/2026", "date like"),
    (spec("date"), "2026-02-30", "date like"),
    (spec("time"), "25:00", "24-hour"),
    (spec("time"), "6pm", "24-hour"),
    (spec("email", pii=True), "sam at example.com", "email"),
    (spec("email", pii=True), "a@b@c.com", "email"),
    (spec("email", pii=True), "sam lee@example.com", "email"),
    (spec("phone", pii=True), "555-0100", "10-digit"),
    (spec("phone", pii=True), "call me", "digits"),
    (spec("phone", pii=True), "+1 555 0100", "10-digit number after"),
    (spec("phone", pii=True), "(017) 555-0100", "10-digit"),
    (spec("money", min=0), "12.345", "two decimal"),
    (spec("money", min=0), -5, "negative"),
    (spec("money", min=0, max=100), 150, "at most 100"),
]


@pytest.mark.parametrize(("field", "value", "expected"), VALID, ids=lambda v: repr(v)[:40])
def test_normalize_valid_values(field: IntakeFieldSpec, value: Any, expected: Any) -> None:
    assert normalize_value(field, value, locale=US) == expected


@pytest.mark.parametrize(("field", "value", "reason"), INVALID, ids=lambda v: repr(v)[:40])
def test_normalize_rejects_invalid_values(field: IntakeFieldSpec, value: Any, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        normalize_value(field, value, locale=US)


def test_errors_never_echo_the_value() -> None:
    secret = "sam@@example.com"
    with pytest.raises(ValueError) as caught:
        normalize_value(spec("email", pii=True), secret, locale=US)
    assert secret not in str(caught.value)


def test_phone_country_code_comes_from_the_locale() -> None:
    assert country_calling_code("en-US") == "1"
    assert country_calling_code("en_AU") == "61"
    assert country_calling_code("es-419") is None
    assert country_calling_code("en") is None
    assert normalize_phone("02 9374 4000", locale="en-AU") == "+61293744000"
    with pytest.raises(ValueError, match="country code"):
        normalize_phone("02 9374 4000", locale="en")


def test_field_specs_from_the_gold_bakery(bakery: Profile) -> None:
    specs = field_specs(bakery.intake_fields("custom-cakes"))
    by_key = {s.key: s for s in specs}
    assert [s.key for s in specs] == ["pickup_date", "servings", "dietary_needs", "message", "customer_name", "customer_phone"]
    assert by_key["pickup_date"].type == "date" and by_key["pickup_date"].required
    assert by_key["servings"].min == 1 and by_key["servings"].type == "integer"
    assert by_key["message"].max_length == 30 and not by_key["message"].required
    assert by_key["customer_phone"].pii and by_key["customer_phone"].type == "phone"
    assert by_key["servings"].spoken_prompt == "How many servings do you need?"


def test_field_specs_carry_choice_options(home: Profile) -> None:
    specs = {s.key: s for s in field_specs(home.intake_fields("quotes"))}
    assert ("weekday-morning", "Weekday morning (arrival 8 am–12 pm)") in specs["time_window"].options
    money = {s.key: s for s in field_specs(home.module("quotes")["approval"]["response_fields"])}["quoted_price"]
    assert money.type == "money" and money.min == 0


def test_field_specs_reject_unknown_types() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        field_specs([{"key": "x", "label": "X", "type": "colour"}])


def test_normalize_intake_drops_unknown_keys_and_lists_missing(bakery: Profile) -> None:
    specs = field_specs(bakery.intake_fields("custom-cakes"))
    values, problems, missing = normalize_intake(
        specs,
        {
            "servings": "30",
            "message": "  Happy   30th ",
            "customer_phone": "",  # blank = not supplied
            "admin": True,  # unknown: dropped
            "dietary_needs": None,
        },
        locale=bakery.locale,
    )
    assert values == {"servings": 30, "message": "Happy 30th"}
    assert problems == []
    assert [s.key for s in missing] == ["pickup_date", "customer_name", "customer_phone"]


def test_normalize_intake_reports_invalid_values_once(bakery: Profile) -> None:
    specs = field_specs(bakery.intake_fields("custom-cakes"))
    values, problems, missing = normalize_intake(
        specs,
        {"pickup_date": "next sunday", "servings": 0, "customer_name": "Sam", "customer_phone": "+12175550100"},
        locale=bakery.locale,
    )
    assert values == {"customer_name": "Sam", "customer_phone": "+12175550100"}
    assert sorted((p.code, p.field) for p in problems) == [("invalid_intake", "pickup_date"), ("invalid_intake", "servings")]
    assert missing == []  # invalid required fields are problems, not also missing


def test_normalize_intake_output_follows_profile_order(bakery: Profile) -> None:
    specs = field_specs(bakery.intake_fields("custom-cakes"))
    supplied = {"customer_name": "Sam", "servings": 12, "pickup_date": "2026-10-18"}
    values, _, _ = normalize_intake(specs, dict(reversed(list(supplied.items()))), locale=bakery.locale)
    assert list(values) == ["pickup_date", "servings", "customer_name"]


# ---------------------------------------------------------------------------- round 2: idempotency replay (SEC-3)

HOME_JOB = {
    "job_type": "leak-repair",
    "description": "Kitchen sink leaks.",
    "zip": "80211",
    "time_window": "weekday-morning",
    "name": "Sam",
}


def test_round2_idempotency_replay_a_request_must_carry_a_contact(home: Profile) -> None:
    """Kettlewick: phone and email are each optional and `name` is the only required personal field, so
    two different people named Sam sending the same job used to share one idempotency key (and one
    quote request), and a quote 'by text' could be sent with no phone number at all."""
    specs = field_specs(home.intake_fields("quotes"))
    by_key = {s.key: s for s in specs}
    assert not by_key["phone"].required and not by_key["email"].required

    _values, problems, missing = normalize_intake(specs, {**HOME_JOB, "contact_by": "text"}, locale=home.locale)
    assert problems == [] and [(s.key, s.required) for s in missing] == [("phone", True)]
    _values, _problems, missing = normalize_intake(specs, {**HOME_JOB, "contact_by": "phone"}, locale=home.locale)
    assert [s.key for s in missing] == ["phone"]
    _values, _problems, missing = normalize_intake(specs, {**HOME_JOB, "contact_by": "Email"}, locale=home.locale)
    assert [(s.key, s.required) for s in missing] == [("email", True)]  # the chosen channel's field
    _values, _problems, missing = normalize_intake(specs, HOME_JOB, locale=home.locale)
    assert [s.key for s in missing] == ["contact_by", "phone"]

    for contact in ({"phone": "303 555 0100"}, {"email": "sam@example.com"}):
        values, problems, missing = normalize_intake(specs, {**HOME_JOB, "contact_by": "text", **contact}, locale=home.locale)
        assert problems == [] and missing == [] and set(contact) <= set(values)
    _values, problems, missing = normalize_intake(specs, {**HOME_JOB, "contact_by": "text", "phone": "12"}, locale=home.locale)
    assert [(p.code, p.field) for p in problems] == [("invalid_intake", "phone")] and missing == []  # reported once


def test_round2_idempotency_replay_required_contacts_are_not_asked_twice(bakery: Profile, salon: Profile) -> None:
    _values, _problems, missing = normalize_intake(field_specs(bakery.intake_fields("custom-cakes")), {}, locale=bakery.locale)
    assert [s.key for s in missing].count("customer_phone") == 1
    _values, _problems, missing = normalize_intake(field_specs(salon.intake_fields("appointments")), {}, locale=salon.locale)
    assert [s.key for s in missing] == ["stylist", "customer_name", "customer_phone", "customer_email"]


def test_round2_idempotency_replay_the_nonce_only_keys_modules_without_a_contact(home: Profile) -> None:
    specs = field_specs(home.intake_fields("quotes"))
    assert nonce_payload(specs, "n0nce") == {}  # a contact value already tells customers apart
    assert nonce_payload(specs, None) == {}  # authenticated callers
    no_contact = [s for s in specs if s.type not in ("phone", "email")]
    assert nonce_payload(no_contact, "n0nce") == {"nonce": "n0nce"}
    values, problems, missing = normalize_intake(no_contact, {**HOME_JOB, "contact_by": "text"}, locale=home.locale)
    assert problems == [] and missing == [] and "phone" not in values
