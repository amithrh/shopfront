"""Identifier helpers.

Two kinds of ids:
  * random ids (`new_id`) for things nobody needs to recompute;
  * keyed derived ids (`IdFactory.derive`) for commitments and tasks: HMAC-SHA256 of the idempotency key
    under a server secret. They are deterministic (a retry on any replica computes the same id, which is
    what makes conditional puts deduplicate) and unguessable (a task id is a bearer capability).

A booking reference (`c_` + 26 base32 characters) cannot be said over the phone, so a voice customer is
told an 8-digit **booking code** instead (`spoken_reference`): the first 5 base32 characters of the id
(25 bits) written as a zero-padded decimal, e.g. `c_exyqratrum2x44qc423hhejvjm` -> `04973073`. Nothing
new is stored: `reference_prefix` turns what the customer says back into the id prefix (`c_exyqr`),
which the commitment store looks up by key prefix (`CommitmentStore.find_by_reference_prefix`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

_PREFIX_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz")
_B32_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
"""RFC 4648 base32, lower-cased as `_b32` writes it."""
BOOKING_CODE_CHARS = 5
"""Base32 characters of the commitment id a booking code carries (25 bits)."""
BOOKING_CODE_DIGITS = 8
"""32**5 - 1 = 33,554,431: every code fits in 8 decimal digits (and starts with 0-3)."""
_CODE_SPELLING = re.compile(r"[0-9][0-9\s.,-]*")
"""How a code may arrive: digits plus the separators people (and hosts) put between groups."""


def _b32(raw: bytes) -> str:
    return base64.b32encode(raw).decode().rstrip("=").lower()


def new_id(prefix: str, *, nbytes: int = 16) -> str:
    """Random id like `q_mfrggzdfmztwq2lk...` (128 bits by default)."""
    _check_prefix(prefix)
    return f"{prefix}_{_b32(secrets.token_bytes(nbytes))}"


def _check_prefix(prefix: str) -> None:
    if not prefix or not set(prefix) <= _PREFIX_ALPHABET or len(prefix) > 8:
        raise ValueError(f"bad id prefix {prefix!r}")


class IdFactory:
    """Derives stable, unguessable ids from idempotency keys."""

    def __init__(self, key: bytes) -> None:
        if len(key) < 32:
            raise ValueError("id derivation key must be at least 32 bytes")
        self._key = key

    def derive(self, prefix: str, idempotency_key: str, *, nbytes: int = 16) -> str:
        """`<prefix>_<base32(HMAC(key, prefix|idempotency_key))[:nbytes]>`; same inputs, same id."""
        _check_prefix(prefix)
        mac = hmac.new(self._key, f"{prefix}|{idempotency_key}".encode(), hashlib.sha256).digest()
        return f"{prefix}_{_b32(mac[:nbytes])}"

    def commitment_id(self, idempotency_key: str) -> str:
        """Id of the booking/order/quote/question created for this idempotency key (prefix `c`)."""
        return self.derive("c", idempotency_key)

    def task_id(self, idempotency_key: str) -> str:
        """MCP task id for this idempotency key (prefix `t`)."""
        return self.derive("t", idempotency_key)


def spoken_reference(commitment_id: str) -> str | None:
    """The 8-digit booking code of a commitment id ('c_exyqr...' -> '04973073'), or None for an id that
    is not a derived commitment id (prefix `c`, then at least 5 base32 characters)."""
    prefix, sep, body = commitment_id.partition("_")
    head = body[:BOOKING_CODE_CHARS]
    if prefix != "c" or not sep or len(head) < BOOKING_CODE_CHARS or any(ch not in _B32_ALPHABET for ch in head):
        return None
    value = 0
    for ch in head:
        value = value * 32 + _B32_ALPHABET.index(ch)
    return f"{value:0{BOOKING_CODE_DIGITS}d}"


def reference_prefix(said: str) -> str | None:
    """The commitment-id prefix (`c_` + 5 base32 characters) a spoken booking code stands for, or None
    when `said` is not a code.

    A code is exactly 8 digits, optionally grouped with spaces, dots, commas or dashes ('0497 3073',
    '0497-3073'), with a value below 32**5. Anything else (letters included) is not a code: a full
    reference can hold 8 digits of its own and must keep resolving as itself."""
    text = said.strip()
    if not _CODE_SPELLING.fullmatch(text):
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) != BOOKING_CODE_DIGITS:
        return None
    value = int(digits)
    if value >= 32**BOOKING_CODE_CHARS:
        return None
    chars: list[str] = []
    for _ in range(BOOKING_CODE_CHARS):
        value, rest = divmod(value, 32)
        chars.append(_B32_ALPHABET[rest])
    return "c_" + "".join(reversed(chars))
