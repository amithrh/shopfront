"""Idempotency keys are stable over normalised input and separate what must stay separate."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from askonce_server.domain.idempotency import idempotency_key, new_record


def key(**overrides: object) -> str:
    args: dict[str, object] = {
        "business_id": "thimbleberry-bakehouse",
        "module_id": "custom-cakes",
        "operation": "custom_order",
        "principal": "anonymous",
        "payload": {"item": "menu/custom-cake", "choices": {"size": "10in-round"}, "intake": {"servings": 30}},
    }
    args.update(overrides)
    return idempotency_key(**args)  # type: ignore[arg-type]


def test_key_is_stable_and_order_independent() -> None:
    reordered = {"intake": {"servings": 30}, "choices": {"size": "10in-round"}, "item": "menu/custom-cake"}
    assert key() == key() == key(payload=reordered)
    assert key().startswith("idem_") and len(key()) == len("idem_") + 64


@pytest.mark.parametrize(
    "change",
    [
        {"principal": "user-2"},
        {"business_id": "quillfern-salon"},
        {"module_id": "other"},
        {"operation": "book"},
        {"payload": {"item": "menu/custom-cake", "choices": {"size": "8in-round"}, "intake": {"servings": 30}}},
    ],
)
def test_key_separates_distinct_requests(change: dict[str, object]) -> None:
    assert key(**change) != key()


def test_equal_instants_hash_identically() -> None:
    local = datetime(2026, 10, 17, 10, 0, tzinfo=ZoneInfo("America/Chicago"))
    utc = local.astimezone(UTC)
    assert key(payload={"start": local}) == key(payload={"start": utc}) == key(payload={"start": "2026-10-17T15:00:00Z"})
    assert key(payload={"day": date(2026, 10, 18)}) == key(payload={"day": "2026-10-18"})
    assert key(payload={"tags": ("a", "b")}) == key(payload={"tags": ["a", "b"]})


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        key(payload={"start": datetime(2026, 10, 17, 10, 0)})


def test_new_record() -> None:
    now = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)
    record = new_record("idem_x", business_id="b", commitment_id="c_1", now=now, ttl=timedelta(hours=24))
    assert record.expires_at == now + timedelta(hours=24)
    assert (record.key, record.business_id, record.commitment_id) == ("idem_x", "b", "c_1")
    with pytest.raises(ValueError):
        new_record("idem_x", business_id="b", commitment_id="c_1", now=now, ttl=timedelta(0))
