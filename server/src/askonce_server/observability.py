"""OpenTelemetry hooks (API only; exporters are configured by the deployment, e.g. ADOT on AgentCore).

The MCP SDK already wraps every inbound request in a SERVER span named `tools/call <tool>`
(`mcp/server/_otel.py`). AskOnce adds INTERNAL child spans named `askonce.<area>.<op>` with
low-cardinality, PII-free attributes, so a certifier failure's `trace_id` leads straight to the
availability check, commit or owner decision that produced it.

Rules for callers:
  * attribute keys use the `askonce.` namespace (see the `ATTR_*` constants);
  * never put intake values, names, phone numbers, emails or free text in attributes; use `redact()`
    when logging intake dictionaries;
  * commitment ids, task ids and booking references are bearer capabilities while there is no MCP
    auth (they cancel a booking or poll a task): logs and spans carry `id_digest(id)`, never the id;
  * owner link tokens travel in URLs (`?token=`): `RedactSecretsFilter` rewrites them in every log
    record, including uvicorn's access log (`uvicorn_log_config`);
  * set `askonce.outcome` on every tool span (`ok`, `handoff`, `withheld`, `pending_owner`, ...).
"""

from __future__ import annotations

import copy
import functools
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from typing import Any, ParamSpec, TypeVar

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

TRACER_NAME = "askonce"

ATTR_BUSINESS = "askonce.business_id"
ATTR_MODULE = "askonce.module_id"
ATTR_TOOL = "askonce.tool"
ATTR_OUTCOME = "askonce.outcome"
ATTR_COMMITMENT = "askonce.commitment_id"
ATTR_TASK = "askonce.task_id"
ATTR_REPLAYED = "askonce.idempotent_replay"
ATTR_FACT_COUNT = "askonce.fact_count"
ATTR_PROTOCOL = "askonce.protocol_version"

_P = ParamSpec("_P")
_R = TypeVar("_R")

logger = logging.getLogger("askonce")

AttrValue = str | bool | int | float


def tracer() -> trace.Tracer:
    """The AskOnce tracer (resolved lazily so a provider installed later is honoured)."""
    return trace.get_tracer(TRACER_NAME)


def _clean(attrs: Mapping[str, Any]) -> dict[str, AttrValue]:
    return {k: v for k, v in attrs.items() if isinstance(v, str | bool | int | float)}


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Span]:
    """Start an INTERNAL span; exceptions are recorded and mark the span as an error."""
    with tracer().start_as_current_span(name, kind=SpanKind.INTERNAL, attributes=_clean(attributes)) as current:
        yield current


@asynccontextmanager
async def aspan(name: str, **attributes: Any) -> AsyncIterator[Span]:
    """Async-context-manager twin of `span` (identical semantics)."""
    with span(name, **attributes) as current:
        yield current


def traced(name: str, **static_attributes: Any) -> Callable[[Callable[_P, Awaitable[_R]]], Callable[_P, Awaitable[_R]]]:
    """Decorator wrapping an async function in `span(name)`.

    Keeps the wrapped signature intact (`functools.wraps`), which matters for MCP tool functions:
    the SDK builds the input schema from the signature.
    """

    def decorate(fn: Callable[_P, Awaitable[_R]]) -> Callable[_P, Awaitable[_R]]:
        @functools.wraps(fn)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with span(name, **static_attributes):
                return await fn(*args, **kwargs)

        return wrapper

    return decorate


def set_attributes(current: Span | None = None, **attributes: Any) -> None:
    """Set attributes on `current` (default: the active span), dropping non-primitive values."""
    (current or trace.get_current_span()).set_attributes(_clean(attributes))


def mark_error(current: Span | None, message: str) -> None:
    """Mark a span as failed without raising (for handled failures worth seeing in traces)."""
    (current or trace.get_current_span()).set_status(Status(StatusCode.ERROR, message))


def current_trace_id() -> str | None:
    """32-hex trace id of the active span (what certification failures record), or None."""
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


REDACTED = "[redacted]"


def redact(values: Mapping[str, Any], pii_keys: set[str] | frozenset[str]) -> dict[str, Any]:
    """Copy of `values` with every key in `pii_keys` replaced by a marker (for logs, never for storage)."""
    return {k: (REDACTED if k in pii_keys else v) for k, v in values.items()}


def id_digest(value: str | None) -> str | None:
    """A stable, non-reversible stand-in for a capability-bearing id in logs and spans (`h_` + 12 hex):
    correlates log lines without letting a log reader cancel a booking or poll a task."""
    if not value:
        return None
    return "h_" + hashlib.sha256(value.encode()).hexdigest()[:12]


_SECRET_PARAM = re.compile(r"(?i)([?&](?:token|access_token|auth)=)[^&\s\"']+")
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")


def redact_secrets(text: str) -> str:
    """`?token=...` / `&token=...` query values and bearer credentials replaced by `[redacted]`."""
    return _BEARER.sub(r"\1[redacted]", _SECRET_PARAM.sub(r"\1[redacted]", text))


class RedactSecretsFilter(logging.Filter):
    """Rewrites URL tokens and bearer credentials in a record's message and string arguments.

    Owner links carry a signed bearer token in the query string; uvicorn's access log prints the path
    with its query, so without this every opened owner link would put a 7-day credential in the logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact_secrets(a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: redact_secrets(v) if isinstance(v, str) else v for k, v in record.args.items()}
        return True


def uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's default logging config with `RedactSecretsFilter` on its default and access handlers."""
    from uvicorn.config import LOGGING_CONFIG

    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_secrets"] = {"()": f"{__name__}.RedactSecretsFilter"}
    for handler in config.get("handlers", {}).values():
        handler.setdefault("filters", []).append("redact_secrets")
    return config


def configure_logging(level: str) -> None:
    """Basic structured-ish logging for the container (stdout, picked up by CloudWatch), with
    `RedactSecretsFilter` on the root handlers."""
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactSecretsFilter) for f in handler.filters):
            handler.addFilter(RedactSecretsFilter())
