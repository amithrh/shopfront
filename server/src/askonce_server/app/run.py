"""Container entrypoint: `python -m askonce_server` (AgentCore Runtime contract: 0.0.0.0:8000/mcp).

Binds `ASKONCE_HOST`: 127.0.0.1 by default in dev/test (the DEV-ONLY keys are published), 0.0.0.0 in
prod and in the container image (which sets ASKONCE_HOST=0.0.0.0).

Settings that must not start (see `config.Settings.problems`) end the process with exit code 2 and one
line per problem on stderr: no traceback, and never the raw (secret) input.

Access logging stays on, but every handler redacts `?token=` values (owner link tokens are bearer
credentials). Runs uvicorn with one worker (legacy sessions are per-process; 2026-07-28 traffic is stateless and
scales by replicas), `proxy_headers=True` and `forwarded_allow_ips` from `FORWARDED_ALLOW_IPS` (default
"127.0.0.1"), and `timeout_keep_alive` above the load balancer's idle timeout (ALB default 60 s).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import NoReturn

import uvicorn
from pydantic import ValidationError

from askonce_server.app.main import create_app
from askonce_server.config import Settings, UnsafeSettings
from askonce_server.observability import configure_logging, uvicorn_log_config

logger = logging.getLogger("askonce.run")

KEEP_ALIVE_TIMEOUT_S = 75
"""Must exceed the load balancer's idle timeout (ALB default 60 s) to avoid 502s on reused connections."""

GRACEFUL_SHUTDOWN_S = 20
"""How long open SSE streams get to finish on SIGTERM before uvicorn closes them."""

EXIT_BAD_SETTINGS = 2
"""Process exit code when the settings refuse to start (a configuration error, not a crash)."""


def settings_problems(exc: ValidationError) -> list[str]:
    """One readable line per problem in a Settings ValidationError, without any input value."""
    lines: list[str] = []
    for error in exc.errors(include_url=False, include_input=False):
        cause = (error.get("ctx") or {}).get("error")
        if isinstance(cause, UnsafeSettings):
            lines.extend(cause.problems)
            continue
        message = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
        field = ".".join(str(part) for part in error.get("loc", ()))
        lines.append(f"ASKONCE_{field.upper()}: {message}" if field else message)
    return lines


def refuse_to_start(exc: ValidationError) -> NoReturn:
    """Print every settings problem to stderr (CloudWatch picks it up) and exit with code 2."""
    problems = settings_problems(exc)
    print("AskOnce refuses to start:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    sys.stderr.flush()
    raise SystemExit(EXIT_BAD_SETTINGS)


def binds_loopback_only(host: str) -> bool:
    """True for a bind address only this machine can reach (`0.0.0.0` / `::` reach every interface)."""
    return host in ("localhost", "::1") or host.startswith("127.")


def startup_warnings(settings: Settings) -> list[str]:
    """Settings that start but deserve a line in the log."""
    warnings: list[str] = []
    dev_secrets = settings.dev_secrets_in_use()
    if dev_secrets and not binds_loopback_only(settings.host):
        warnings.append(
            f"binding {settings.host} with DEV-ONLY secrets ({', '.join(dev_secrets)}): anyone who can reach "
            "this port can sign owner links with the published keys; bind 127.0.0.1 (unset ASKONCE_HOST) "
            "or publish the container port on loopback only (-p 127.0.0.1:8000:8000)"
        )
    if settings.invariants_strict and settings.profile_snapshot_dir is None:
        warnings.append(
            "ASKONCE_PROFILE_SNAPSHOT_DIR is unset: invariant L3 (quotes must appear in the cited page "
            "snapshot) is skipped on this server; the certifier must have checked it"
        )
    return warnings


def main() -> None:
    """Read settings, configure logging, build the app, serve."""
    try:
        settings = Settings()
    except ValidationError as exc:
        refuse_to_start(exc)
    configure_logging(settings.log_level)
    for warning in startup_warnings(settings):
        logger.warning(warning)
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
        timeout_keep_alive=KEEP_ALIVE_TIMEOUT_S,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
        workers=1,
        server_header=False,
        log_level=settings.log_level.lower(),
        log_config=uvicorn_log_config(),  # access log lines carry owner link tokens: redact them
    )
