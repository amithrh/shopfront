"""A persistent Bedrock spend ledger with a hard budget cap (default $5.00 in total).

Every routed call is priced from the Converse response `usage` with a deliberately conservative
(slightly high) on-demand price table, and the running total is kept in a JSON file that survives
restarts and is shared by every process on the machine:

  * default file: `<repo root>/.local/bedrock-spend.json` (git-ignored); override with
    `ASKONCE_BEDROCK_LEDGER`;
  * cap: `ASKONCE_BEDROCK_BUDGET_USD` (default 5.00);
  * before a call, the router *reserves* a worst-case estimate for it (input characters / 3 tokens,
    plus `maxTokens` output tokens, priced for the model about to be called). If spent + in-flight
    reservations + that estimate would exceed the cap, `BudgetExceeded` is raised and nothing is called;
  * after the call, the reservation is replaced by the actual cost (or released if the call failed).

Concurrency: one `fcntl.flock` on a sidecar `.lock` file (plus a thread lock) around every
read-modify-write, and the JSON is written atomically (temp file + `os.replace`). Reservations left by
a process that died are dropped after `STALE_RESERVATION_S` or as soon as their pid is gone.

A ledger file that cannot be parsed raises `LedgerError` instead of being reset: silently starting
again from $0 would defeat the cap.

Report: `python -m askonce_bedrock.spend` (see `askonce_bedrock.spend`).
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import math
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # POSIX; on other platforms only the in-process lock applies.
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

DEFAULT_BUDGET_USD = 5.00
LEDGER_ENV = "ASKONCE_BEDROCK_LEDGER"
BUDGET_ENV = "ASKONCE_BEDROCK_BUDGET_USD"

CHARS_PER_TOKEN = 3
"""Worst-case input estimate: one token per 3 characters of request JSON (real text averages ~4)."""

DEFAULT_MAX_OUTPUT_TOKENS = 8192
"""Assumed output ceiling when a request sets no `inferenceConfig.maxTokens` (pass one to tighten it)."""

TOOL_OVERHEAD_TOKENS = 600
"""Extra input tokens assumed when a request carries a toolConfig (the provider's hidden tool prompt)."""

STALE_RESERVATION_S = 30 * 60
"""A reservation older than this is assumed to belong to a crashed process and is dropped."""


# --------------------------------------------------------------------------------------- prices

# USD per 1M tokens (input, output), on-demand Standard tier, rounded UP on purpose.
# Sources (checked 2026-10-13):
#   * Nova: AWS Price List API (pricing:GetProducts, ServiceCode AmazonBedrock, regionCode
#     ap-southeast-2): Nova Lite $0.063 / $0.252, Nova Pro $0.84 / $3.36, Nova 2.0 Lite
#     (cross-region global) $0.32 / $2.63.
#   * Claude: https://platform.claude.com/docs/en/build-with-claude/claude-on-amazon-bedrock-legacy and
#     https://www.cloudzero.com/blog/claude-on-aws-bedrock/ : Sonnet 4.6 $3 / $15 and Haiku 4.5 $1 / $5
#     on global endpoints; regional and geo (`au.`) endpoints cost 10% more ($3.30 / $16.50 and
#     $1.10 / $5.50). We charge every Claude profile the regional price, rounded up. Long-context
#     (>200K input tokens) Sonnet rates are higher; the router never sends prompts that large.
# Prompt-cache writes are charged at 1.25x the input price and cache reads at the full input price
# (the real read price is 0.1x): over-estimating keeps the cap honest.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "au.anthropic.claude-sonnet-4-6": (3.50, 17.00),
    "global.anthropic.claude-sonnet-4-6": (3.50, 17.00),
    "au.anthropic.claude-haiku-4-5-20251001-v1:0": (1.20, 6.00),
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": (1.20, 6.00),
    "apac.amazon.nova-lite-v1:0": (0.08, 0.32),
    "global.amazon.nova-2-lite-v1:0": (0.40, 3.00),
    "apac.amazon.nova-pro-v1:0": (1.00, 4.00),
}

UNKNOWN_MODEL_PRICE: tuple[float, float] = (15.00, 75.00)
"""Any model id not in the table is priced like the most expensive Claude tier ever listed."""

CACHE_WRITE_MULTIPLIER = 1.25


def price_for(model_id: str, prices: Mapping[str, tuple[float, float]] | None = None) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for `model_id`; unknown ids get `UNKNOWN_MODEL_PRICE`."""
    return (prices or PRICES_PER_MTOK).get(model_id, UNKNOWN_MODEL_PRICE)


def cost_usd(
    model_id: str,
    usage: Mapping[str, Any] | None,
    prices: Mapping[str, tuple[float, float]] | None = None,
) -> float:
    """Estimated cost of one call from its Converse `usage` block."""
    usage = usage or {}
    in_price, out_price = price_for(model_id, prices)
    input_tokens = int(usage.get("inputTokens") or 0)
    output_tokens = int(usage.get("outputTokens") or 0)
    cache_read = int(usage.get("cacheReadInputTokens") or 0)
    cache_write = int(usage.get("cacheWriteInputTokens") or 0)
    total = (
        (input_tokens + cache_read) * in_price
        + cache_write * in_price * CACHE_WRITE_MULTIPLIER
        + output_tokens * out_price
    )
    return total / 1_000_000


def worst_case_tokens(request: Mapping[str, Any]) -> tuple[int, int]:
    """(input, output) token ceilings for a Converse request (without `modelId`).

    Input: the request's JSON length / `CHARS_PER_TOKEN` (+ `TOOL_OVERHEAD_TOKENS` with tools).
    Output: `inferenceConfig.maxTokens`, else `DEFAULT_MAX_OUTPUT_TOKENS`."""
    body = {k: v for k, v in request.items() if k != "modelId"}
    chars = len(json.dumps(body, ensure_ascii=False, default=str))
    input_tokens = math.ceil(chars / CHARS_PER_TOKEN)
    if body.get("toolConfig"):
        input_tokens += TOOL_OVERHEAD_TOKENS
    max_tokens = (body.get("inferenceConfig") or {}).get("maxTokens")
    return input_tokens, int(max_tokens) if max_tokens else DEFAULT_MAX_OUTPUT_TOKENS


# --------------------------------------------------------------------------------------- errors


class LedgerError(RuntimeError):
    """The ledger file exists but cannot be read (never silently reset: that would lift the cap)."""


class BudgetExceeded(RuntimeError):
    """A call's worst-case cost would take the total past the budget cap; the call was not made."""

    def __init__(
        self, *, model_id: str, estimate_usd: float, spent_usd: float, reserved_usd: float, budget_usd: float
    ) -> None:
        self.model_id = model_id
        self.estimate_usd = estimate_usd
        self.spent_usd = spent_usd
        self.reserved_usd = reserved_usd
        self.budget_usd = budget_usd
        remaining = max(0.0, budget_usd - spent_usd - reserved_usd)
        super().__init__(
            f"Bedrock budget cap reached: spent ${spent_usd:.4f}"
            + (f" (+${reserved_usd:.4f} in flight)" if reserved_usd else "")
            + f" of ${budget_usd:.2f}, ${remaining:.4f} left, but this call to {model_id} could cost up to"
            f" ${estimate_usd:.4f}. Nothing was sent. Raise {BUDGET_ENV} or lower maxTokens to continue."
        )


# --------------------------------------------------------------------------------------- ledger


def default_ledger_path() -> Path:
    """`<repo root>/.local/bedrock-spend.json`: the repo root is the first ancestor (of this file, then
    of the working directory) holding `.git`; failing both, `~/.askonce/bedrock-spend.json`."""
    for start in (Path(__file__).resolve().parent, Path.cwd().resolve()):
        for folder in (start, *start.parents):
            if (folder / ".git").exists():
                return folder / ".local" / "bedrock-spend.json"
    return Path.home() / ".askonce" / "bedrock-spend.json"


@dataclass(frozen=True)
class Reservation:
    id: str
    model_id: str
    usd: float


def _empty() -> dict[str, Any]:
    return {"version": 1, "total_usd": 0.0, "calls": 0, "models": {}, "reservations": {}, "updated_at": None}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


class SpendLedger:
    """The spend file plus the cap. Thread- and process-safe on one machine (see module docstring)."""

    _thread_locks: dict[str, threading.Lock] = {}
    _thread_locks_guard = threading.Lock()

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        budget_usd: float = DEFAULT_BUDGET_USD,
        prices: Mapping[str, tuple[float, float]] | None = None,
        clock: Any = time.time,
    ) -> None:
        if budget_usd < 0:
            raise ValueError("budget_usd must be >= 0")
        self.path = Path(path) if path is not None else default_ledger_path()
        self.budget_usd = float(budget_usd)
        self.prices = dict(prices) if prices is not None else dict(PRICES_PER_MTOK)
        self._clock = clock
        key = str(self.path.resolve())
        with SpendLedger._thread_locks_guard:
            self._tlock = SpendLedger._thread_locks.setdefault(key, threading.Lock())

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SpendLedger:
        """`ASKONCE_BEDROCK_LEDGER` (file) and `ASKONCE_BEDROCK_BUDGET_USD` (cap), both optional."""
        env = os.environ if env is None else env
        path = env.get(LEDGER_ENV) or None
        budget = float(env[BUDGET_ENV]) if env.get(BUDGET_ENV) else DEFAULT_BUDGET_USD
        return cls(path, budget_usd=budget)

    # -- file plumbing ---------------------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._tlock, open(f"{self.path}.lock", "a+") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _empty()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or not isinstance(data.get("total_usd"), (int, float)):
                raise ValueError("missing total_usd")
        except ValueError as exc:
            raise LedgerError(
                f"cannot read the Bedrock spend ledger {self.path} ({exc}); fix or move it by hand"
            ) from None
        base = _empty()
        base.update(data)
        return base

    def _save(self, data: dict[str, Any]) -> None:
        data["updated_at"] = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
        fd, tmp = tempfile.mkstemp(prefix=".bedrock-spend.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

    def _purge_stale(self, data: dict[str, Any]) -> None:
        now = self._clock()
        reservations: dict[str, Any] = data.setdefault("reservations", {})
        for rid, entry in list(reservations.items()):
            pid = entry.get("pid")
            too_old = now - float(entry.get("at") or 0) > STALE_RESERVATION_S
            dead = isinstance(pid, int) and pid != os.getpid() and not _pid_alive(pid)
            if too_old or dead:
                del reservations[rid]

    @staticmethod
    def _reserved(data: Mapping[str, Any]) -> float:
        return sum(float(r.get("usd") or 0) for r in (data.get("reservations") or {}).values())

    # -- the cap ---------------------------------------------------------------------------

    def estimate(self, model_id: str, input_tokens: int, output_tokens: int) -> float:
        return cost_usd(model_id, {"inputTokens": input_tokens, "outputTokens": output_tokens}, self.prices)

    def reserve(self, model_id: str, input_tokens: int, output_tokens: int) -> Reservation:
        """Hold the worst-case cost of one call, or raise `BudgetExceeded` (nothing is held then)."""
        estimate = self.estimate(model_id, input_tokens, output_tokens)
        with self._locked():
            data = self._load()
            self._purge_stale(data)
            spent = float(data["total_usd"])
            reserved = self._reserved(data)
            committed = spent + reserved
            if committed + estimate > self.budget_usd + 1e-12 or committed >= self.budget_usd:
                raise BudgetExceeded(
                    model_id=model_id,
                    estimate_usd=estimate,
                    spent_usd=spent,
                    reserved_usd=reserved,
                    budget_usd=self.budget_usd,
                )
            reservation = Reservation(uuid.uuid4().hex, model_id, estimate)
            data["reservations"][reservation.id] = {
                "model_id": model_id,
                "usd": estimate,
                "pid": os.getpid(),
                "at": self._clock(),
            }
            self._save(data)
        return reservation

    def release(self, reservation: Reservation) -> None:
        """Drop a reservation whose call failed (a failed call is not billed)."""
        with self._locked():
            data = self._load()
            if data["reservations"].pop(reservation.id, None) is not None:
                self._save(data)

    def settle(self, reservation: Reservation | None, model_id: str, usage: Mapping[str, Any] | None) -> float:
        """Replace `reservation` (if any) with the call's actual cost; returns that cost."""
        cost = cost_usd(model_id, usage, self.prices)
        usage = usage or {}
        with self._locked():
            data = self._load()
            if reservation is not None:
                data["reservations"].pop(reservation.id, None)
            data["total_usd"] = float(data["total_usd"]) + cost
            data["calls"] = int(data.get("calls") or 0) + 1
            model = data["models"].setdefault(
                model_id, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
            )
            model["calls"] += 1
            model["input_tokens"] += int(usage.get("inputTokens") or 0) + int(usage.get("cacheReadInputTokens") or 0)
            model["input_tokens"] += int(usage.get("cacheWriteInputTokens") or 0)
            model["output_tokens"] += int(usage.get("outputTokens") or 0)
            model["cost_usd"] = float(model["cost_usd"]) + cost
            self._save(data)
        return cost

    def record(self, model_id: str, usage: Mapping[str, Any] | None) -> float:
        """Add a call made outside the router (no reservation)."""
        return self.settle(None, model_id, usage)

    # -- reporting -------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Totals for display: spent, reserved, remaining, budget, calls and per-model rows."""
        with self._locked():
            data = self._load()
            self._purge_stale(data)
        spent = float(data["total_usd"])
        reserved = self._reserved(data)
        return {
            "ledger": str(self.path),
            "budget_usd": self.budget_usd,
            "spent_usd": spent,
            "reserved_usd": reserved,
            "remaining_usd": max(0.0, self.budget_usd - spent - reserved),
            "calls": int(data.get("calls") or 0),
            "models": data.get("models") or {},
            "updated_at": data.get("updated_at"),
        }

    @property
    def spent_usd(self) -> float:
        return float(self.snapshot()["spent_usd"])
