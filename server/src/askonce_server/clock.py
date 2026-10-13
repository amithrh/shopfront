"""Injectable time source. Domain code never calls `datetime.now()` directly, so tests are deterministic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Returns the current instant as a timezone-aware UTC datetime."""

    def now(self) -> datetime: ...


class SystemClock:
    """Wall-clock time."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A settable clock for tests. `advance()` moves it forward."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        self._now = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def set(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        self._now = at.astimezone(UTC)

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


def utc_iso(dt: datetime) -> str:
    """UTC timestamp in the profile schema's format (`YYYY-MM-DDTHH:MM:SSZ`)."""
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
