"""Route Bedrock Converse calls for a logical role across an ordered list of models.

Why: a new account is capped at about 10 requests per minute per model, so one model id cannot carry
onboarding, the certifier and the simulated customers at once. The router spreads calls across the
`au.` and `global.` Claude inference profiles and Amazon Nova, without ever going over a per-model
budget on purpose:

  * every model has a token bucket (`rpm` per minute, `burst` tokens; defaults 9 RPM for Claude and
    18 for Nova, burst 1, so any 60-second window holds at most rpm + 1 calls);
  * a call goes to the first model of the role that has a token now; if none has one, the router
    waits for the earliest token (bounded by `max_wait_s`);
  * a `ThrottlingException` (and the other "slow down" errors) cools that model down with
    exponential backoff and full jitter, then the call fails over to the next model;
  * a model that cannot be used at all (access denied, unknown or unsupported model id) is parked for
    `unavailable_cooldown_s` and the call fails over; transient server errors fail over too;
  * a request the model rejects as invalid raises at once (it would fail on every model);
  * `metrics` counts calls, successes, throttles and errors per model;
  * every call is priced into a persistent spend ledger with a hard cap (default $5.00): before a
    call, its worst-case cost is reserved, and `BudgetExceeded` is raised instead of calling when the
    cap would be passed (see `askonce_bedrock.ledger`).

Only boto3 is needed. The router is synchronous and thread-safe (Strands calls models from worker
threads); from asyncio use `await asyncio.to_thread(router.converse, ...)`.

    router = BedrockRouter.from_env()          # region ap-southeast-2 unless AWS_REGION says otherwise
    reply = router.converse("extract", messages=[{"role": "user", "content": [{"text": "Hi"}]}])
    reply.text, reply.model_id, router.metrics.snapshot()
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from askonce_bedrock.ledger import Reservation, SpendLedger, worst_case_tokens

logger = logging.getLogger("askonce.bedrock")

DEFAULT_REGION = "ap-southeast-2"

SONNET_AU = "au.anthropic.claude-sonnet-4-6"
SONNET_GLOBAL = "global.anthropic.claude-sonnet-4-6"
HAIKU_AU = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
HAIKU_GLOBAL = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
NOVA_LITE_APAC = "apac.amazon.nova-lite-v1:0"
NOVA_2_LITE_GLOBAL = "global.amazon.nova-2-lite-v1:0"
NOVA_PRO_APAC = "apac.amazon.nova-pro-v1:0"

ALL_MODELS: tuple[str, ...] = (
    SONNET_AU,
    SONNET_GLOBAL,
    HAIKU_AU,
    HAIKU_GLOBAL,
    NOVA_LITE_APAC,
    NOVA_2_LITE_GLOBAL,
    NOVA_PRO_APAC,
)
"""Every model id / inference profile the default roles use (smoke-tested one call each)."""

DEFAULT_ROLES: dict[str, tuple[str, ...]] = {
    # Website extraction and contradiction finding: the strongest models first.
    "extract": (SONNET_AU, SONNET_GLOBAL, NOVA_PRO_APAC, HAIKU_AU, HAIKU_GLOBAL),
    # The reference voice host: quality, then speed.
    "host": (SONNET_AU, SONNET_GLOBAL, HAIKU_AU, HAIKU_GLOBAL, NOVA_PRO_APAC),
    # Simulated customers for the certifier: many short calls.
    "simulate": (HAIKU_AU, HAIKU_GLOBAL, NOVA_LITE_APAC, NOVA_2_LITE_GLOBAL, NOVA_PRO_APAC),
    # Anything where cost matters more than nuance.
    "cheap": (NOVA_LITE_APAC, NOVA_2_LITE_GLOBAL, HAIKU_AU, HAIKU_GLOBAL),
}

DEFAULT_CLAUDE_RPM = 9.0
DEFAULT_NOVA_RPM = 18.0

THROTTLE_CODES = frozenset(
    {
        "ThrottlingException",
        "TooManyRequestsException",
        "ServiceQuotaExceededException",
        "RequestLimitExceeded",
        "ModelNotReadyException",
    }
)
"""Errors that mean "this model, not now": cool it down and fail over."""

TRANSIENT_CODES = frozenset(
    {"ServiceUnavailableException", "InternalServerException", "ModelTimeoutException", "ModelErrorException"}
)
"""Server-side failures worth trying on the next model."""

UNAVAILABLE_CODES = frozenset({"AccessDeniedException", "ResourceNotFoundException", "UnrecognizedClientException"})
"""The model cannot be used by this account (or does not exist): park it and fail over."""

_MODEL_PROBLEM_HINTS = (
    "model identifier",
    "inference profile",
    "on-demand throughput",
    "isn't supported",
    "not supported",
    "access to the model",
)
"""Phrases that make a `ValidationException` a model problem (fail over) rather than a bad request (raise)."""


def default_rpm(model_id: str, *, claude_rpm: float = DEFAULT_CLAUDE_RPM, nova_rpm: float = DEFAULT_NOVA_RPM) -> float:
    """9 RPM for Claude, 18 for Nova, Claude's budget for anything else (the conservative one)."""
    return nova_rpm if ".nova" in model_id or model_id.startswith("amazon.nova") else claude_rpm


class RouterError(Exception):
    """Base class for router failures."""


class RouterExhausted(RouterError):
    """No model of the role answered within `max_wait_s` (all throttled, parked or failing)."""

    def __init__(self, role: str, attempts: Sequence[Attempt]) -> None:
        self.role = role
        self.attempts = list(attempts)
        tried = "; ".join(f"{a.model_id}: {a.detail or a.outcome}" for a in attempts) or "no model had capacity"
        super().__init__(f"Bedrock role {role!r} exhausted ({tried})")


@dataclass(frozen=True)
class Attempt:
    model_id: str
    outcome: str
    """`ok`, `throttled`, `unavailable`, or the error code of a transient failure."""
    latency_s: float
    detail: str | None = None
    """`<code>: <message>` of the error, for a failed attempt."""


@dataclass(frozen=True)
class RouterResult:
    """One routed call: the raw Converse response plus which model answered."""

    response: dict[str, Any]
    model_id: str
    role: str
    attempts: tuple[Attempt, ...]
    cost_usd: float | None = None
    """Estimated cost of the successful call (None when the router has no ledger)."""

    @property
    def text(self) -> str:
        """The concatenated text blocks of the reply (empty for a tool-use-only reply)."""
        content = ((self.response.get("output") or {}).get("message") or {}).get("content") or ()
        return "".join(block.get("text", "") for block in content if isinstance(block, dict))


_EPSILON = 1e-9
"""Float slack: a bucket refilled for exactly 60/rpm seconds holds a whole token."""


class TokenBucket:
    """`rate_per_s` tokens per second up to `capacity`. Thread-safe; the clock is injectable."""

    def __init__(self, rpm: float, *, burst: float = 1.0, clock: Callable[[], float] = time.monotonic) -> None:
        if rpm <= 0 or burst < 1:
            raise ValueError("rpm must be > 0 and burst >= 1")
        self.rate_per_s = rpm / 60.0
        self.capacity = float(burst)
        self._clock = clock
        self._tokens = float(burst)
        self._stamp = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._stamp) * self.rate_per_s)
        self._stamp = now

    def try_acquire(self) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= 1.0 - _EPSILON:
                self._tokens = max(0.0, self._tokens - 1.0)
                return True
            return False

    def wait_time(self) -> float:
        """Seconds until a token is available (0 if one is available now)."""
        with self._lock:
            self._refill()
            return 0.0 if self._tokens >= 1.0 - _EPSILON else (1.0 - self._tokens) / self.rate_per_s


@dataclass
class ModelStats:
    calls: int = 0
    successes: int = 0
    throttles: int = 0
    errors: int = 0
    unavailable: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0


class Metrics:
    """Per-model counters. `snapshot()` is a plain dict (log it, or put it on a dashboard)."""

    def __init__(self) -> None:
        self._stats: dict[str, ModelStats] = {}
        self._lock = threading.Lock()

    def record(self, model_id: str, outcome: str, latency_s: float, usage: Mapping[str, Any] | None = None) -> None:
        with self._lock:
            stats = self._stats.setdefault(model_id, ModelStats())
            stats.calls += 1
            stats.latency_s += latency_s
            if outcome == "ok":
                stats.successes += 1
                stats.input_tokens += int((usage or {}).get("inputTokens") or 0)
                stats.output_tokens += int((usage or {}).get("outputTokens") or 0)
            elif outcome == "throttled":
                stats.throttles += 1
            elif outcome == "unavailable":
                stats.unavailable += 1
            else:
                stats.errors += 1

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        with self._lock:
            return {model: dict(vars(stats)) for model, stats in self._stats.items()}

    def get(self, model_id: str) -> ModelStats:
        with self._lock:
            return ModelStats(**vars(self._stats.get(model_id, ModelStats())))


@dataclass
class RouterConfig:
    """Roles, per-model budgets and retry timing. `from_env` reads overrides (see `BedrockRouter`)."""

    roles: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULT_ROLES))
    rpm: dict[str, float] = field(default_factory=dict)
    """Per-model RPM overrides; models not listed get `default_rpm`."""
    claude_rpm: float = DEFAULT_CLAUDE_RPM
    nova_rpm: float = DEFAULT_NOVA_RPM
    burst: float = 1.0
    max_wait_s: float = 60.0
    """Longest a call waits overall for capacity before `RouterExhausted`."""
    backoff_base_s: float = 2.0
    backoff_max_s: float = 60.0
    unavailable_cooldown_s: float = 600.0

    def rpm_for(self, model_id: str) -> float:
        return self.rpm.get(model_id) or default_rpm(model_id, claude_rpm=self.claude_rpm, nova_rpm=self.nova_rpm)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RouterConfig:
        """Overrides, all optional:

        * `ASKONCE_BEDROCK_ROLE_<ROLE>`: comma-separated model ids (e.g. `ASKONCE_BEDROCK_ROLE_HOST`);
          a new role name adds a role;
        * `ASKONCE_BEDROCK_CLAUDE_RPM`, `ASKONCE_BEDROCK_NOVA_RPM`: the family defaults;
        * `ASKONCE_BEDROCK_RPM`: per-model, `model_id=rpm` pairs separated by commas;
        * `ASKONCE_BEDROCK_BURST`, `ASKONCE_BEDROCK_MAX_WAIT_S`.
        """
        env = os.environ if env is None else env
        config = cls()
        for key, value in env.items():
            if key.startswith("ASKONCE_BEDROCK_ROLE_") and value.strip():
                config.roles[key.removeprefix("ASKONCE_BEDROCK_ROLE_").lower()] = _split(value)
        if env.get("ASKONCE_BEDROCK_CLAUDE_RPM"):
            config.claude_rpm = float(env["ASKONCE_BEDROCK_CLAUDE_RPM"])
        if env.get("ASKONCE_BEDROCK_NOVA_RPM"):
            config.nova_rpm = float(env["ASKONCE_BEDROCK_NOVA_RPM"])
        for pair in _split(env.get("ASKONCE_BEDROCK_RPM", "")):
            model_id, sep, value = pair.rpartition("=")
            if not sep or not model_id:
                raise ValueError(f"ASKONCE_BEDROCK_RPM entry {pair!r} is not model_id=rpm")
            config.rpm[model_id.strip()] = float(value)
        if env.get("ASKONCE_BEDROCK_BURST"):
            config.burst = float(env["ASKONCE_BEDROCK_BURST"])
        if env.get("ASKONCE_BEDROCK_MAX_WAIT_S"):
            config.max_wait_s = float(env["ASKONCE_BEDROCK_MAX_WAIT_S"])
        return config


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def error_code(exc: BaseException) -> str | None:
    """The AWS error code of a botocore `ClientError` (duck-typed: no botocore import needed)."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        return str(code) if code else None
    return None


def _message(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Message") or "")
    return str(exc)


def classify(exc: BaseException) -> str:
    """`throttled`, `unavailable`, `error` (transient: fail over) or `fatal` (raise now)."""
    code = error_code(exc)
    if code is None:
        # Connection resets and read timeouts (botocore exceptions without a response) are transient.
        return "error" if type(exc).__module__.startswith(("botocore", "urllib3")) else "fatal"
    if code in THROTTLE_CODES:
        return "throttled"
    if code in UNAVAILABLE_CODES:
        return "unavailable"
    if code in TRANSIENT_CODES:
        return "error"
    if code == "ValidationException" and any(hint in _message(exc).lower() for hint in _MODEL_PROBLEM_HINTS):
        return "unavailable"
    return "fatal"


Operation = Callable[[Any, str], dict[str, Any]]
"""`op(client, model_id) -> response`: one Bedrock runtime call for one model."""

_DEFAULT_LEDGER: Any = object()
"""Sentinel: build the ledger from the environment (`SpendLedger.from_env`)."""


class BedrockRouter:
    """Routes calls for a role across its models (see module docstring)."""

    def __init__(
        self,
        client: Any,
        config: RouterConfig | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
        ledger: SpendLedger | None = _DEFAULT_LEDGER,
    ) -> None:
        """`ledger`: the spend ledger and budget cap; by default `SpendLedger.from_env()` (file
        `ASKONCE_BEDROCK_LEDGER`, cap `ASKONCE_BEDROCK_BUDGET_USD`, $5.00). Pass `None` to run
        without one (no cap!)."""
        self.client = client
        self.config = config or RouterConfig()
        self.metrics = Metrics()
        self.ledger: SpendLedger | None = SpendLedger.from_env() if ledger is _DEFAULT_LEDGER else ledger
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._buckets: dict[str, TokenBucket] = {}
        self._cool_until: dict[str, float] = {}
        self._strikes: dict[str, int] = {}

    @classmethod
    def from_env(
        cls,
        *,
        session: Any = None,
        region: str | None = None,
        config: RouterConfig | None = None,
        ledger: SpendLedger | None = _DEFAULT_LEDGER,
    ) -> BedrockRouter:
        """A router over a fresh `bedrock-runtime` client (botocore retries off: the router retries).

        Region: `region`, else `AWS_REGION` / `AWS_DEFAULT_REGION`, else ap-southeast-2. Credentials come
        from `session` (a `boto3.Session`) or the default chain (`AWS_PROFILE`). `ledger`: as in `__init__`
        (default: `SpendLedger.from_env()`, the $5.00 cap)."""
        import boto3
        from botocore.config import Config

        region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION
        session = session or boto3.Session()
        client = session.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(retries={"total_max_attempts": 1, "mode": "standard"}, read_timeout=120, connect_timeout=10),
        )
        return cls(client, config or RouterConfig.from_env(), ledger=ledger)

    # -- capacity --------------------------------------------------------------------------

    def bucket(self, model_id: str) -> TokenBucket:
        with self._lock:
            found = self._buckets.get(model_id)
            if found is None:
                found = TokenBucket(self.config.rpm_for(model_id), burst=self.config.burst, clock=self._clock)
                self._buckets[model_id] = found
            return found

    def _cooling(self, model_id: str) -> float:
        """Seconds left in this model's cooldown (0 when usable)."""
        with self._lock:
            return max(0.0, self._cool_until.get(model_id, 0.0) - self._clock())

    def _cool(self, model_id: str, seconds: float) -> None:
        with self._lock:
            self._cool_until[model_id] = max(self._cool_until.get(model_id, 0.0), self._clock() + seconds)

    def _backoff(self, model_id: str) -> float:
        """Exponential backoff with full jitter for this model's consecutive throttles."""
        with self._lock:
            strikes = self._strikes.get(model_id, 0) + 1
            self._strikes[model_id] = strikes
        ceiling = min(self.config.backoff_max_s, self.config.backoff_base_s * 2 ** (strikes - 1))
        return self.config.backoff_base_s / 2 + self._rng.uniform(0, ceiling)

    def _reset_strikes(self, model_id: str) -> None:
        with self._lock:
            self._strikes.pop(model_id, None)

    def models(self, role: str) -> tuple[str, ...]:
        try:
            models = self.config.roles[role]
        except KeyError:
            raise KeyError(f"unknown Bedrock role {role!r}; known: {sorted(self.config.roles)}") from None
        if not models:
            raise ValueError(f"Bedrock role {role!r} has no models")
        return models

    # -- calls -----------------------------------------------------------------------------

    def run(
        self,
        role: str,
        op: Operation,
        *,
        max_wait_s: float | None = None,
        worst_case: tuple[int, int] | None = None,
    ) -> RouterResult:
        """Run `op(client, model_id)` on the first model of `role` with capacity, failing over on
        throttles, unusable models and transient errors. Raises `RouterExhausted`, or the error of a
        request that is invalid in itself.

        With a ledger, `worst_case` = (input tokens, output tokens) is reserved against the budget
        before each attempt (priced for that attempt's model); `BudgetExceeded` is raised, without
        calling, if the cap would be passed. Without `worst_case` only an exhausted budget blocks the
        call. Successful calls are recorded from the response `usage`."""
        models = self.models(role)
        deadline = self._clock() + (self.config.max_wait_s if max_wait_s is None else max_wait_s)
        attempts: list[Attempt] = []
        while True:
            for model_id in models:
                if self._cooling(model_id) > 0 or not self.bucket(model_id).try_acquire():
                    continue
                reservation: Reservation | None = None
                if self.ledger is not None:
                    # Raises BudgetExceeded (nothing sent) when the cap would be passed.
                    reservation = self.ledger.reserve(model_id, *(worst_case or (0, 0)))
                started = self._clock()
                try:
                    response = op(self.client, model_id)
                except Exception as exc:  # noqa: BLE001 - classified below; fatal ones re-raised
                    if reservation is not None:
                        self.ledger.release(reservation)  # type: ignore[union-attr]
                    elapsed = self._clock() - started
                    kind = classify(exc)
                    if kind == "fatal":
                        self.metrics.record(model_id, error_code(exc) or type(exc).__name__, elapsed)
                        raise
                    self.metrics.record(model_id, kind, elapsed)
                    outcome = kind if kind != "error" else (error_code(exc) or "error")
                    detail = f"{error_code(exc) or type(exc).__name__}: {_message(exc)}"[:300]
                    attempts.append(Attempt(model_id, outcome, elapsed, detail))
                    if kind == "throttled":
                        self._cool(model_id, self._backoff(model_id))
                    elif kind == "unavailable":
                        self._cool(model_id, self.config.unavailable_cooldown_s)
                    logger.warning("bedrock %s on %s (role %s): %s", kind, model_id, role, error_code(exc) or exc)
                    continue
                except BaseException:  # KeyboardInterrupt / cancellation: free the held budget, then stop
                    if reservation is not None:
                        self.ledger.release(reservation)  # type: ignore[union-attr]
                    raise
                elapsed = self._clock() - started
                self._reset_strikes(model_id)
                usage = response.get("usage") if isinstance(response, dict) else None
                self.metrics.record(model_id, "ok", elapsed, usage)
                cost = self.ledger.settle(reservation, model_id, usage) if self.ledger is not None else None
                attempts.append(Attempt(model_id, "ok", elapsed))
                return RouterResult(
                    response=response, model_id=model_id, role=role, attempts=tuple(attempts), cost_usd=cost
                )
            wait = self._next_capacity(models)
            now = self._clock()
            if wait is None or now + wait > deadline:
                raise RouterExhausted(role, attempts)
            self._sleep(max(wait, 0.01))

    def _next_capacity(self, models: Iterable[str]) -> float | None:
        """Seconds until some model of the role could take a call (None if all are parked as unusable
        for longer than any wait makes sense)."""
        waits = [max(self._cooling(m), self.bucket(m).wait_time()) for m in models]
        best = min(waits) if waits else None
        return None if best is None or math.isinf(best) else best

    def converse(
        self,
        role: str,
        messages: Sequence[Mapping[str, Any]],
        *,
        system: Sequence[Mapping[str, Any]] | str | None = None,
        inference_config: Mapping[str, Any] | None = None,
        tool_config: Mapping[str, Any] | None = None,
        max_wait_s: float | None = None,
        **extra: Any,
    ) -> RouterResult:
        """`bedrock-runtime.converse` for `role`. `system` may be a plain string. `extra` passes other
        Converse fields through (e.g. `additionalModelRequestFields`)."""
        request: dict[str, Any] = {"messages": list(messages), **extra}
        if system:
            request["system"] = [{"text": system}] if isinstance(system, str) else list(system)
        if inference_config:
            request["inferenceConfig"] = dict(inference_config)
        if tool_config:
            request["toolConfig"] = dict(tool_config)

        def op(client: Any, model_id: str) -> dict[str, Any]:
            return client.converse(modelId=model_id, **request)

        return self.run(role, op, max_wait_s=max_wait_s, worst_case=worst_case_tokens(request))
