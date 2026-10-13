"""Intake field handling: profile `intake_fields` -> specs, and normalisation of customer-supplied values.

Normalisation rules (value in -> stored value):
  text / long_text  NFC, whitespace collapsed, <= max_length            -> str
  integer / number  int/float or numeric string, within min/max         -> int / float
  boolean           bool or yes/no/true/false strings                   -> bool
  choice            one option value (case-insensitive on value or label) -> option value
  multi_choice      list (or comma string) of option values               -> sorted unique list
  date              ISO `YYYY-MM-DD`                                     -> ISO string
  time              `HH:MM` 24h                                          -> `HH:MM`
  email             one `@`, no spaces, <= 254                           -> lowercased str
  phone             digits with optional +; national numbers get the business country code
                    (derived from business.locale, e.g. en-US -> +1)   -> E.164 str
  money             number in currency units                            -> integer cents

Unknown keys are dropped (never stored), so the model cannot smuggle extra fields into a commitment.
Blank values (None, empty or whitespace-only strings, empty lists) count as "not supplied".
A module with phone/email fields needs a value for at least one of them (`missing_contact`).

Error messages never echo the value (intake values may be PII); they only name the rule that failed.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, get_args

from pydantic import JsonValue

from shopfront_server.domain.planning import IntakeFieldSpec, PlanProblem
from shopfront_server.profile.model import CONTACT_FIELD_TYPES, JsonObj

FIELD_TYPES: frozenset[str] = frozenset(get_args(IntakeFieldSpec.model_fields["type"].annotation))
"""Every `field.type` the schema allows."""

MAX_EMAIL_LENGTH = 254

COUNTRY_CALLING_CODES: Mapping[str, str] = {
    "US": "1", "CA": "1", "PR": "1", "GB": "44", "UK": "44", "IE": "353", "AU": "61", "NZ": "64",
    "DE": "49", "AT": "43", "CH": "41", "FR": "33", "BE": "32", "NL": "31", "LU": "352", "ES": "34",
    "PT": "351", "IT": "39", "DK": "45", "SE": "46", "NO": "47", "FI": "358", "PL": "48", "MX": "52",
    "BR": "55", "AR": "54", "CL": "56", "CO": "57", "IN": "91", "JP": "81", "KR": "82", "CN": "86",
    "SG": "65", "HK": "852", "ZA": "27", "AE": "971", "IL": "972",
}  # fmt: skip
"""Region subtag of `business.locale` -> ITU calling code, for national phone numbers."""

_TRUE = frozenset({"true", "yes", "y", "1", "on"})
_FALSE = frozenset({"false", "no", "n", "0", "off"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::00)?$")
_PHONE_ALLOWED_RE = re.compile(r"^\+?[\d\s().\-/]+$")
_MONEY_STRIP_RE = re.compile(r"[\s,$€£¥]")


def field_specs(fields: Sequence[JsonObj]) -> tuple[IntakeFieldSpec, ...]:
    """Convert the profile's `intake_fields` (or `response_fields`) into specs, in profile order."""
    specs: list[IntakeFieldSpec] = []
    for field in fields:
        field_type = field.get("type")
        if field_type not in FIELD_TYPES:
            raise ValueError(f"unsupported intake field type {field_type!r}")
        key = str(field["key"])
        specs.append(
            IntakeFieldSpec(
                key=key,
                label=str(field.get("label") or key),
                type=field_type,
                required=bool(field.get("required", False)),
                spoken_prompt=field.get("spoken_prompt"),
                options=tuple(
                    (str(option["value"]), str(option.get("label") or option["value"]))
                    for option in field.get("options", ()) or ()
                ),
                min=_optional_float(field.get("min")),
                max=_optional_float(field.get("max")),
                max_length=field.get("max_length"),
                pii=bool(field.get("pii", False)),
            )
        )
    return tuple(specs)


def is_blank(value: Any) -> bool:
    """True for values that count as 'not supplied'."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list | tuple | set | frozenset | dict):
        return len(value) == 0
    return False


def normalize_value(spec: IntakeFieldSpec, value: Any, *, locale: str) -> JsonValue:
    """Normalise one value. Raises ValueError with a short reason when it is invalid."""
    if is_blank(value):
        raise ValueError("a value is required")
    match spec.type:
        case "text" | "long_text":
            return _text(spec, value)
        case "integer":
            return _integer(spec, value)
        case "number":
            return _number(spec, value)
        case "boolean":
            return _boolean(value)
        case "choice":
            return _choice(spec, value)
        case "multi_choice":
            return _multi_choice(spec, value)
        case "date":
            return _date(value)
        case "time":
            return _time(value)
        case "email":
            return _email(spec, value)
        case "phone":
            return normalize_phone(value, locale=locale)
        case "money":
            return _money(spec, value)
    raise ValueError(f"unsupported field type {spec.type!r}")  # pragma: no cover - Literal is exhaustive


def normalize_intake(
    specs: Sequence[IntakeFieldSpec], values: Mapping[str, Any], *, locale: str
) -> tuple[dict[str, JsonValue], list[PlanProblem], list[IntakeFieldSpec]]:
    """Normalise every supplied value.

    Returns `(normalised values, problems for invalid values, required specs still missing)`.
    Keys not in `specs` are ignored. Built-in keys handled by the tool signature (e.g. `party_size`
    when it is also an intake field) are accepted from `values` like any other field.

    A required field whose value is invalid is reported once, as a problem, not also as missing.
    Output order follows `specs` (profile order), so the result is deterministic.

    Contact rule: when the specs include a phone or email field, at least one of them must have a
    value, even if each is optional on its own (see `missing_contact`). The normalised intake is part
    of the idempotency key, and without auth the contact is what keeps two customers' otherwise
    identical requests apart (it is also how the owner reaches the customer).
    """
    normalised: dict[str, JsonValue] = {}
    problems: list[PlanProblem] = []
    invalid: set[str] = set()
    for spec in specs:
        if spec.key not in values or is_blank(values[spec.key]):
            continue
        try:
            normalised[spec.key] = normalize_value(spec, values[spec.key], locale=locale)
        except ValueError as exc:
            invalid.add(spec.key)
            problems.append(
                PlanProblem(code="invalid_intake", detail=f"{spec.label}: {exc}", field=spec.key)
            )
    missing = [spec for spec in specs if spec.required and spec.key not in normalised and spec.key not in invalid]
    contact = missing_contact(specs, normalised, invalid=invalid, missing=missing)
    if contact is not None:
        missing.append(contact)
    return normalised, problems, missing


def missing_contact(
    specs: Sequence[IntakeFieldSpec],
    normalised: Mapping[str, JsonValue],
    *,
    invalid: set[str] | frozenset[str] = frozenset(),
    missing: Sequence[IntakeFieldSpec] = (),
) -> IntakeFieldSpec | None:
    """The contact field to ask for, or None.

    None when the specs have no phone/email field, when one has a value (or an invalid value, already
    reported as a problem), or when one is already listed in `missing` (required on its own).
    Otherwise the field matching a chosen contact preference (a choice answered `email` -> the email
    field) or else the first phone field (else the first email field), marked required so the form
    insists on it."""
    contacts = [spec for spec in specs if spec.type in CONTACT_FIELD_TYPES]
    missing_keys = {spec.key for spec in missing}
    if not contacts or any(s.key in normalised or s.key in invalid or s.key in missing_keys for s in contacts):
        return None
    chosen = {value for spec in specs if spec.type == "choice" and isinstance(value := normalised.get(spec.key), str)}
    preferred = "email" if "email" in chosen else "phone"
    pick = next((spec for spec in contacts if spec.type == preferred), contacts[0])
    return pick if pick.required else pick.model_copy(update={"required": True})


def nonce_payload(specs: Sequence[IntakeFieldSpec], nonce: str | None) -> dict[str, str]:
    """`{"nonce": nonce}` for the idempotency payload when the module collects no contact field, else {}.

    Such a module (allowed only by a lenient load, loader invariant L11) gives an unauthenticated
    caller's request nothing that tells two strangers apart, so the caller's per-call nonce keeps them
    apart instead. The documented cost: a retry of the same call is then a new request (a booking
    retry finds its own slot taken, an order or quote retry is sent twice)."""
    if nonce is None or any(spec.type in CONTACT_FIELD_TYPES for spec in specs):
        return {}
    return {"nonce": nonce}


def country_calling_code(locale: str) -> str | None:
    """ITU calling code for the region of a BCP 47 locale (`en-US` -> `1`), or None if unknown."""
    parts = locale.replace("_", "-").split("-")
    for part in reversed(parts[1:]):
        if len(part) == 2 and part.isalpha():
            return COUNTRY_CALLING_CODES.get(part.upper())
    return None


def normalize_phone(value: Any, *, locale: str) -> str:
    """E.164 (`+12175550100`). National numbers get the locale's calling code; `00` means international."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValueError("expected a phone number")
    text = str(value).strip()
    if not _PHONE_ALLOWED_RE.match(text):
        raise ValueError("a phone number may only contain digits, spaces and + ( ) - .")
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        international = digits
    elif digits.startswith("00"):
        international = digits[2:]
    else:
        code = country_calling_code(locale)
        if code is None:
            raise ValueError("include the country code, starting with +")
        if code == "1":
            if len(digits) == 11 and digits.startswith("1"):
                digits = digits[1:]
            if len(digits) != 10 or digits[0] in "01":
                raise ValueError("expected a 10-digit number")
        elif digits.startswith("0"):
            digits = digits[1:]  # national trunk prefix
        international = code + digits
    if international.startswith("1") and len(international) != 11:
        raise ValueError("expected a 10-digit number after +1")
    if not 8 <= len(international) <= 15 or international.startswith("0"):
        raise ValueError("not a valid international phone number")
    return "+" + international


# ---------------------------------------------------------------------------- per-type helpers


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def clean_text(value: str) -> str:
    """NFC with every whitespace run collapsed to one space and the ends trimmed."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def _text(spec: IntakeFieldSpec, value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise ValueError("expected text")
    text = clean_text(str(value))
    if not text:
        raise ValueError("a value is required")
    if spec.max_length is not None and len(text) > spec.max_length:
        raise ValueError(f"must be at most {spec.max_length} characters")
    return text


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("expected a number")
    if isinstance(value, int | Decimal):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            number = Decimal(value.strip())
        except InvalidOperation:
            raise ValueError("expected a number") from None
    else:
        raise ValueError("expected a number")
    if not number.is_finite():
        raise ValueError("expected a finite number")
    return number


def _check_range(spec: IntakeFieldSpec, number: Decimal) -> None:
    if spec.min is not None and number < Decimal(repr(spec.min)):
        raise ValueError(f"must be at least {_fmt(spec.min)}")
    if spec.max is not None and number > Decimal(repr(spec.max)):
        raise ValueError(f"must be at most {_fmt(spec.max)}")


def _fmt(bound: float) -> str:
    return str(int(bound)) if float(bound).is_integer() else str(bound)


def _integer(spec: IntakeFieldSpec, value: Any) -> int:
    number = _decimal(value)
    if number != number.to_integral_value():
        raise ValueError("expected a whole number")
    _check_range(spec, number)
    return int(number)


def _number(spec: IntakeFieldSpec, value: Any) -> float:
    number = _decimal(value)
    _check_range(spec, number)
    return float(number)


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        token = value.strip().casefold()
        if token in _TRUE:
            return True
        if token in _FALSE:
            return False
    raise ValueError("expected yes or no")


def _option_key(text: str) -> str:
    return clean_text(text).casefold()


def _choice(spec: IntakeFieldSpec, value: Any) -> str:
    if not isinstance(value, str | int) or isinstance(value, bool):
        raise ValueError("expected one of the listed options")
    wanted = _option_key(str(value))
    for option_value, label in spec.options:
        if wanted in (_option_key(option_value), _option_key(label)):
            return option_value
    raise ValueError("not one of the listed options")


def _multi_choice(spec: IntakeFieldSpec, value: Any) -> list[JsonValue]:
    if isinstance(value, str):
        items: list[Any] = [part for part in value.split(",") if part.strip()]
    elif isinstance(value, list | tuple | set | frozenset):
        items = list(value)
    else:
        raise ValueError("expected a list of options")
    chosen = {_choice(spec, item) for item in items}
    if not chosen:
        raise ValueError("a value is required")
    out: list[JsonValue] = []
    out.extend(sorted(chosen))
    return out


def _date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str) and _DATE_RE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError:
            pass
    raise ValueError("expected a date like 2026-10-18")


def _time(value: Any) -> str:
    if isinstance(value, time):
        return f"{value.hour:02d}:{value.minute:02d}"
    if isinstance(value, str):
        match = _TIME_RE.match(value.strip())
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
            if hour < 24 and minute < 60:
                return f"{hour:02d}:{minute:02d}"
    raise ValueError("expected a 24-hour time like 14:30")


def _email(spec: IntakeFieldSpec, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected an email address")
    text = value.strip()
    limit = min(MAX_EMAIL_LENGTH, spec.max_length or MAX_EMAIL_LENGTH)
    local, _, domain = text.partition("@")
    if (
        text.count("@") != 1
        or any(ch.isspace() for ch in text)
        or not local
        or not domain
        or domain.startswith(".")
        or domain.endswith(".")
    ):
        raise ValueError("expected an email address like name@example.com")
    if len(text) > limit:
        raise ValueError(f"must be at most {limit} characters")
    return text.lower()


def _money(spec: IntakeFieldSpec, value: Any) -> int:
    if isinstance(value, str):
        value = _MONEY_STRIP_RE.sub("", value)
    number = _decimal(value)
    if number != number.quantize(Decimal("0.01")):
        raise ValueError("use at most two decimal places")
    if number < 0 and (spec.min is None or spec.min >= 0):
        raise ValueError("must not be negative")
    _check_range(spec, number)
    return int(number * 100)
