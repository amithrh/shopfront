"""The approval-expiry sweeper loop."""

from __future__ import annotations

import logging

import anyio
import pytest

from askonce_server.app.background import run_sweeper, sweep_once

pytestmark = pytest.mark.anyio


class CountingOwner:
    def __init__(self, failures: int = 0, handled: int = 0) -> None:
        self.calls = 0
        self.failures = failures
        self.handled = handled
        self.reached = anyio.Event()
        self.target = 3

    async def expire_due(self) -> int:
        self.calls += 1
        if self.calls >= self.target:
            self.reached.set()
        if self.failures:
            self.failures -= 1
            raise RuntimeError("ProvisionedThroughputExceededException")
        return self.handled


async def test_sweeper_calls_expire_due_every_interval() -> None:
    owner = CountingOwner()
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: run_sweeper(owner, interval_s=0.01))
            await owner.reached.wait()
            tg.cancel_scope.cancel()
    assert owner.calls >= 3


async def test_first_sweep_runs_immediately() -> None:
    owner = CountingOwner()
    owner.target = 1
    with anyio.fail_after(1):
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: run_sweeper(owner, interval_s=3600))
            await owner.reached.wait()
            tg.cancel_scope.cancel()
    assert owner.calls == 1


async def test_initial_delay_is_honoured() -> None:
    owner = CountingOwner()
    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: run_sweeper(owner, interval_s=0.01, initial_delay_s=3600))
        await anyio.sleep(0.05)
        tg.cancel_scope.cancel()
    assert owner.calls == 0


async def test_sweeper_logs_failures_and_keeps_going(caplog: pytest.LogCaptureFixture) -> None:
    owner = CountingOwner(failures=2)
    owner.target = 4
    with caplog.at_level(logging.ERROR, logger="askonce.sweeper"), anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: run_sweeper(owner, interval_s=0.01))
            await owner.reached.wait()
            tg.cancel_scope.cancel()
    failures = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(failures) == 2 and failures[0].exc_info is not None


async def test_sweep_once_reports_what_happened(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="askonce.sweeper"):
        assert await sweep_once(CountingOwner(handled=2)) == 2
        assert await sweep_once(CountingOwner(failures=1)) is None
    assert "handled 2 request(s)" in caplog.text


async def test_interval_must_be_positive() -> None:
    with pytest.raises(ValueError):
        await run_sweeper(CountingOwner(), interval_s=0)
