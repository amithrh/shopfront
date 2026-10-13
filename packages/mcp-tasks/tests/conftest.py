"""Shared pytest fixtures for mcp-tasks. Tests run on anyio's asyncio backend; no network beyond loopback."""

from __future__ import annotations

import pytest

from support import Harness, ManualClock


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def clock() -> ManualClock:
    """A frozen clock at 2026-10-14T16:00Z; advance it explicitly."""
    return ManualClock()


@pytest.fixture
def harness() -> Harness:
    """A fresh demo server on the wall clock, polled every 20 ms."""
    return Harness()
