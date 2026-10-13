"""Background work run inside the FastAPI lifespan: the approval-expiry sweeper.

Every `settings.sweep_interval_s` seconds: `OwnerService.expire_due()`. Exceptions are logged and the
loop continues. Safe with several replicas: each expiry is a CAS transition, so two sweepers racing on
the same commitment apply it once (the loser sees ConcurrencyConflict/InvalidState and skips).

The first sweep runs immediately (after `initial_delay_s`), so requests that expired while no replica
was running are handled at startup. Cancellation (the lifespan's task group) stops the loop cleanly.
"""

from __future__ import annotations

import logging
from typing import Protocol

import anyio

from askonce_server.observability import ATTR_OUTCOME, mark_error, set_attributes, span

logger = logging.getLogger("askonce.sweeper")


class ExpirySweepable(Protocol):
    """The part of `OwnerService` the sweeper needs."""

    async def expire_due(self) -> int: ...


async def sweep_once(owner: ExpirySweepable) -> int | None:
    """One sweep. Returns how many commitments were handled, or None if the sweep failed (logged)."""
    with span("askonce.owner.expire_due") as current:
        try:
            handled = await owner.expire_due()
        except Exception:  # noqa: BLE001 - a failing sweep must never kill the loop
            logger.exception("approval-expiry sweep failed; will retry on the next interval")
            mark_error(current, "sweep failed")
            return None
        set_attributes(current, **{ATTR_OUTCOME: "ok", "askonce.expired_count": handled})
    if handled:
        logger.info("approval-expiry sweep handled %d request(s)", handled)
    return handled


async def run_sweeper(owner: ExpirySweepable, *, interval_s: float, initial_delay_s: float = 0.0) -> None:
    """Loop forever (cancel the enclosing task group to stop)."""
    if interval_s <= 0:
        raise ValueError("interval_s must be positive")
    if initial_delay_s > 0:
        await anyio.sleep(initial_delay_s)
    while True:
        await sweep_once(owner)
        await anyio.sleep(interval_s)
