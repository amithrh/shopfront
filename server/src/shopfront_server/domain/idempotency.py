"""Deterministic idempotency keys.

    key = "idem_" + sha256(JCS({business_id, module_id, operation, principal, payload}))

`payload` holds the *normalised* request (item ref, choices, slot start in UTC ISO, day, party size,
visit window, normalised intake values, question text). Normalising first means "Saturday 10am" and
"2026-10-17T10:00" from two retries hash identically.

How long a request dedupes:

  * A *live* commitment (open, confirmed or approved) dedupes for its whole life: its commitment id
    is derived from the key (`ids.commitment_id`, HMAC), and both stores refuse a second commitment
    with that id. The `IDEM#<key>` marker written with it carries a TTL (`IDEMPOTENCY_TTL_S`), but that
    TTL only bounds the marker row; nothing relies on it for semantics.
  * A *settled* commitment (`models.SETTLED_STATUSES`) is never replayed: an identical request is a new
    attempt under the next `generation_key`, up to `MAX_GENERATIONS` attempts.

Who counts as the same customer: the principal keeps two authenticated customers apart. Without auth
every caller is "anonymous", so the payload must tell customers apart by itself: transactional intake
always carries a phone number or email address (`intake.normalize_intake`, loader invariant L11), and
an owner question (or a module that collects no contact details at all) carries a per-call nonce. A
replay to an anonymous caller is redacted anyway (no reference, task id or task handle).

The payload must only contain what the customer asked for, never values derived from current
capacity (such as an automatically assigned stylist): a retry after the first attempt committed would
otherwise compute a different key and book twice.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from shopfront_server.canonical import sha256_hex
from shopfront_server.domain.models import IdempotencyRecord

KEY_PREFIX = "idem_"

MAX_GENERATIONS = 32
"""How many attempts one identical request gets (each settled one, cancelled, declined, quoted..., frees
the next). Past that the request is refused (`too_many_attempts`), never answered with an old outcome."""


def idempotency_key(
    *, business_id: str, module_id: str, operation: str, principal: str, payload: Mapping[str, Any]
) -> str:
    """Stable key for a request (see module docstring). `payload` must be JSON-compatible.

    For convenience, dates become ISO strings, aware datetimes UTC ISO strings, tuples and sets lists
    (sets sorted) and integral Decimals ints, so callers can pass plan values directly.
    """
    body = {
        "business_id": business_id,
        "module_id": module_id,
        "operation": operation,
        "principal": principal,
        "payload": _jsonable(payload),
    }
    return KEY_PREFIX + sha256_hex(body)


def generation_key(key: str, generation: int) -> str:
    """The key of the `generation`-th attempt at one request (0 is the request's own key).

    A request whose commitment was settled (`models.SETTLED_STATUSES`: cancelled, declined, expired,
    counter-offered, answered with another time, quoted, answered) can be made again: the new attempt
    is a new generation with its own derived commitment and task ids, while retries of a *live*
    attempt keep deduplicating on its key.
    """
    if generation < 0:
        raise ValueError("generation must be >= 0")
    return key if generation == 0 else f"{key}~{generation}"


def new_record(key: str, *, business_id: str, commitment_id: str, now: datetime, ttl: timedelta) -> IdempotencyRecord:
    """The marker written with the commitment."""
    if ttl <= timedelta(0):
        raise ValueError("idempotency TTL must be positive")
    return IdempotencyRecord(
        key=key, business_id=business_id, commitment_id=commitment_id, created_at=now, expires_at=now + ttl
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("idempotency payload datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value
