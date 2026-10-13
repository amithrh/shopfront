"""Runtime configuration, read from environment variables prefixed `SHOPFRONT_`.

Every secret has a **DEV-ONLY** default so `uv run` works out of the box, and dev binds 127.0.0.1 only.
The guard fails closed:
  * `prod` (`Settings.validate_for_prod`) refuses DEV-ONLY secrets, in-memory storage, lenient
    invariants, an `ALLOWED_HOSTS` list that cannot match the public Host header, a `PUBLIC_BASE_URL`
    that is not public https, CORS origins the transport would 403 and a short admin token;
  * whatever `SHOPFRONT_ENV` says, a deployment that *looks* real (`looks_deployed`: DynamoDB storage,
    or any public name in `PUBLIC_BASE_URL`, `ALLOWED_HOSTS`, `ALLOWED_ORIGINS` or `CORS_ORIGINS`)
    refuses DEV-ONLY secrets and short admin tokens, and checks profile invariants strictly;
  * everywhere, `ALLOWED_HOSTS` / `ALLOWED_ORIGINS` entries must be shapes the MCP transport can match.
Every problem is reported at once, in one error that never echoes the raw (secret) input.

List values accept JSON arrays or comma-separated strings, e.g.
`SHOPFRONT_ALLOWED_HOSTS='["mcp.example.com","localhost:*"]'` or `SHOPFRONT_ALLOWED_HOSTS=mcp.example.com,localhost:*`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
"""Repository root (…/Mission-hackathon), used for dev defaults only."""

MCP_PATH = "/mcp"
"""The MCP endpoint path. Fixed: AgentCore Runtime expects exactly `0.0.0.0:8000/mcp`."""

SERVER_NAME = "shopfront"
"""Fixed MCPServer name. It is the request-state audience, so every replica must use the same one."""

# DEV-ONLY secrets (published here). Refused in prod and in anything that looks deployed (see problems()).
DEV_REQUEST_STATE_KEY = "DEV-ONLY-request-state-key-0123456789abcdef"
DEV_OWNER_LINK_KEY = "DEV-ONLY-owner-link-key-0123456789abcdef0123"
DEV_ID_KEY = "DEV-ONLY-id-derivation-key-0123456789abcdef01"
DEV_ADMIN_TOKEN = "DEV-ONLY-admin-token"
_DEV_SECRETS = {DEV_REQUEST_STATE_KEY, DEV_OWNER_LINK_KEY, DEV_ID_KEY, DEV_ADMIN_TOKEN}

MIN_KEY_BYTES = 32
MIN_ADMIN_TOKEN_BYTES = 32

_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})  # noqa: S104

DEV_BIND_HOST = "127.0.0.1"
"""Dev binds loopback only: the DEV-ONLY keys are published, so nothing else may reach the server."""
PROD_BIND_HOST = "0.0.0.0"  # noqa: S104 - container entrypoint; AgentCore/ALB terminate in front of it
"""Prod (and the container image, which sets SHOPFRONT_HOST) binds every interface."""

_NAME = r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
_PORT = r"(?::(?:\*|[0-9]{1,5}))?"
_HOST_PATTERN = re.compile(rf"{_NAME}{_PORT}")
"""What the MCP transport can match against a Host header: `name`, `name:port` or `name:*`."""
_ORIGIN_PATTERN = re.compile(rf"[A-Za-z][A-Za-z0-9+.-]*://{_NAME}{_PORT}")
"""What it can match against an Origin header: `scheme://name`, `scheme://name:port` or `scheme://name:*`."""
_HOST_HINT = "use exact names such as 'mcp.example.com' ('name:*' additionally allows any explicit port)"
_ORIGIN_HINT = "use exact origins such as 'https://app.example.com' ('http://localhost:*' = any explicit port)"


def _hostname(value: str) -> str:
    """The host part of a Host-header pattern, origin or URL (`mcp.example.com:*` -> `mcp.example.com`)."""
    text = value.strip()
    if "://" in text:
        return (urlsplit(text).hostname or "").lower()
    if text.startswith("["):
        return text.split("]", 1)[0].lower() + "]"
    return text.rsplit(":", 1)[0].lower() if text.count(":") == 1 else text.lower()


def is_loopback(value: str) -> bool:
    """True for a localhost / loopback Host pattern, origin or URL."""
    host = _hostname(value)
    return host in _LOCAL_HOSTNAMES or host.endswith(".localhost")


def is_local_name(value: str) -> bool:
    """True for a Host pattern, origin or URL that only local tooling can use: loopback, or a
    single-label name (`testserver`, `shopfront:8000`, `http://localhost:6274`). Anything else (a
    dotted name, an IP address, a wildcard) is reachable from elsewhere."""
    host = _hostname(value)
    return not host or is_loopback(value) or not any(c in host for c in ".:*[")


def is_public_url(value: str) -> bool:
    """True for a URL on a non-local host (`https://mcp.example.com`), i.e. a real deployment."""
    return bool(_hostname(value)) and not is_local_name(value)


def origin_allowed(origin: str, patterns: Sequence[str]) -> bool:
    """The MCP transport's Origin rule, mirrored from `TransportSecurityMiddleware._validate_origin`:
    exact match, or a `scheme://host:*` pattern matching that host with any port."""
    for pattern in patterns:
        if origin == pattern:
            return True
        if pattern.endswith(":*") and origin.startswith(pattern[:-2] + ":"):
            return True
    return False


def _wildcard_misused(value: str) -> bool:
    """A `*` anywhere but a trailing `:*` (the transport's only wildcard)."""
    return "*" in value.removesuffix(":*")


def host_pattern_problem(value: str) -> str | None:
    """Why an `ALLOWED_HOSTS` entry can never match a Host header, or None if it can.

    The transport compares the Host header literally (or `name:*` = any explicit port): no scheme, no
    path, no `*.example.com` and no bare `*`."""
    if "://" in value:
        return "has a scheme (Host headers never do)"
    if value.strip() in ("*", ""):
        return "is not a host name (the transport has no allow-all; a bare '*' matches nothing)"
    if _wildcard_misused(value):
        return "uses a wildcard the transport does not support (only a trailing ':*', meaning any port)"
    if "/" in value:
        return "has a path (Host headers never do)"
    if not _HOST_PATTERN.fullmatch(value):
        return "is not a host name, host:port or host:*"
    return None


def origin_pattern_problem(value: str) -> str | None:
    """Why an `ALLOWED_ORIGINS` entry can never match a browser Origin header, or None if it can."""
    if value.strip() in ("*", ""):
        return "is not an origin (the transport has no allow-all; a bare '*' matches nothing)"
    if "://" not in value:
        return "has no scheme (browsers send 'https://name')"
    if _wildcard_misused(value):
        return "uses a wildcard the transport does not support (only a trailing ':*', meaning any port)"
    if "/" in value.split("://", 1)[1]:
        return "has a path or trailing '/' (browsers send scheme://host[:port] only)"
    if not _ORIGIN_PATTERN.fullmatch(value):
        return "is not scheme://host, scheme://host:port or scheme://host:*"
    return None


def _portless_twin(value: str, *, origin: bool) -> str | None:
    """For an entry that only matches requests naming a port (`name:*`, or a spelled-out default port
    such as `name:443` / `https://name:443`), the entry a client on the default port actually sends."""
    if value.endswith(":*"):
        return value[:-2]
    for scheme_prefix, port in (("https://", ":443"), ("http://", ":80")):
        if value.endswith(port) and (not origin or value.startswith(scheme_prefix)):
            return value[: -len(port)]
    return None


def _portless_problems(entries: Sequence[str], *, setting: str, origin: bool) -> Iterable[str]:
    """Public entries that a client on the default port can never match.

    Clients omit default ports (`Host: mcp.example.com`, `Origin: https://app.example.com`), and the
    transport's `name:*` needs an explicit port, so `mcp.example.com:443` or `mcp.example.com:*` alone
    answers every proxied request with 421 (403 for origins). Fine next to the bare entry."""
    for value in entries:
        bare = _portless_twin(value, origin=origin)
        if bare is None or bare in entries or is_local_name(value):
            continue
        yield (
            f"SHOPFRONT_{setting} entry {value!r} only matches requests that name a port, but clients on "
            f"the default port send none, so it matches nothing; add {bare!r}"
        )


def _split_list(value: Any) -> Any:
    """Allow `a,b,c` as well as a JSON array for list-valued settings."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("["):
            import json

            return json.loads(stripped)
        return [part.strip() for part in stripped.split(",") if part.strip()]
    return value


class UnsafeSettings(ValueError):
    """Settings that must not start. `problems` lists every reason; the message never holds a secret."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        count = len(self.problems)
        lines = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(f"Shopfront settings are unsafe ({count} problem{'s' if count != 1 else ''}):\n{lines}")

    def __reduce__(self) -> tuple[type[UnsafeSettings], tuple[list[str]]]:
        return (type(self), (self.problems,))


StrList = Annotated[list[str], NoDecode]
SecretList = Annotated[list[SecretStr], NoDecode]
PathList = Annotated[list[Path], NoDecode]


class Settings(BaseSettings):
    """All server settings. Construct directly in tests; `get_settings()` reads the environment."""

    # hide_input_in_errors: a validation error must never print the raw input (it holds the secrets).
    model_config = SettingsConfigDict(
        env_prefix="SHOPFRONT_", env_file=None, extra="ignore", frozen=True, hide_input_in_errors=True
    )

    env: Literal["dev", "test", "prod"] = "dev"

    # ---- HTTP / transport ---------------------------------------------------------------
    host: str = DEV_BIND_HOST
    """Bind address. Unset: 127.0.0.1 (dev/test, so the published DEV-ONLY keys never face a network),
    0.0.0.0 in prod. The container image sets SHOPFRONT_HOST=0.0.0.0 (AgentCore contract)."""
    port: int = 8000
    public_base_url: str = "http://localhost:8000"
    """Base of the owner links sent in notifications. Prod requires a public https URL."""
    allowed_hosts: StrList = Field(default_factory=lambda: ["localhost:*", "127.0.0.1:*"])
    """Exact Host values accepted by the MCP transport (`name:*` = any explicit port, so it never matches
    a portless Host). No scheme, no path, no wildcard names; checked by `host_pattern_problem`."""
    allowed_origins: StrList = Field(default_factory=lambda: ["http://localhost:*", "http://127.0.0.1:*"])
    """Browser origins accepted by the MCP transport's Origin check (`scheme://host[:port|:*]`)."""
    cors_origins: StrList = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])
    """Origins for CORSMiddleware (reference host, owner page). Must also be in allowed_origins (prod
    refuses one that is not; elsewhere it is logged)."""

    # ---- secrets -------------------------------------------------------------------------
    request_state_keys: SecretList = Field(default_factory=lambda: [SecretStr(DEV_REQUEST_STATE_KEY)])
    """Shared AES-GCM keys for MRTR requestState (first seals, all open). >= 32 bytes each."""
    request_state_ttl_s: float = 900.0
    """How long a customer may take to answer a confirmation/intake question (MRTR round trip)."""
    owner_link_keys: SecretList = Field(default_factory=lambda: [SecretStr(DEV_OWNER_LINK_KEY)])
    """HMAC keys for owner links (first signs, all verify). >= 32 bytes each."""
    id_key: SecretStr = SecretStr(DEV_ID_KEY)
    """HMAC key deriving commitment and task ids from idempotency keys (ids are bearer capabilities)."""
    admin_token: SecretStr = SecretStr(DEV_ADMIN_TOKEN)
    """Bearer token for the dev/admin link-minting endpoint. Disable by leaving the dev value in prod
    (or any deployment that looks real); a real token there must be at least 32 bytes."""

    # ---- storage ---------------------------------------------------------------------------
    storage: Literal["memory", "dynamodb"] = "memory"
    dynamodb_table: str = "shopfront"
    """Single table for profiles, directory, commitments, capacity counters and idempotency markers."""
    dynamodb_tasks_table: str = "shopfront-tasks"
    """Table owned by the reusable mcp-tasks store (hash key taskId, GSI byPrincipal)."""
    aws_region: str = "ap-southeast-2"
    dynamodb_endpoint_url: str | None = None
    profile_dirs: PathList = Field(default_factory=lambda: [REPO_ROOT / "profiles"])
    """Directories of profile JSON files loaded by the file/in-memory profile store."""
    fallback_profile: Path | None = REPO_ROOT / "schema" / "example-profile.json"
    """Loaded when no profile file is found in profile_dirs."""
    profile_schema_path: Path = REPO_ROOT / "schema" / "business-profile.schema.json"
    """The Business Profile JSON Schema (copied into the container image next to the code)."""
    profile_snapshot_dir: Path | None = None
    """Directory of page snapshots named `<content_sha256>.txt` for invariant L3; None = L3 skipped."""
    profile_cache_ttl_s: float = 30.0
    strict_invariants: bool | None = None
    """True: any L1-L11 violation rejects a profile. False (dev): L3/L7/L11 violations are warnings.
    Unset means strict in prod and in any deployment that looks real, lenient for local dev (see
    `invariants_strict`); prod refuses False."""

    # ---- tasks / approvals --------------------------------------------------------------------
    task_poll_interval_ms: int = 3000
    task_default_ttl_ms: int = 48 * 3600 * 1000
    keep_waiting_max_days: int = Field(default=30, ge=1)
    """The longest a request under `on_expiry: keep_waiting` waits for the owner. Its service time ends
    the wait sooner; requests without one (quote requests, owner questions) wait this long. After that it
    expires like `notify_customer`."""
    idempotency_ttl_s: int = 24 * 3600
    owner_link_ttl_s: int = 7 * 24 * 3600
    sweep_interval_s: float = 60.0

    # ---- observability ------------------------------------------------------------------------
    otel_service_name: str = "shopfront-mcp"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator(
        "allowed_hosts", "allowed_origins", "cors_origins", "request_state_keys", "owner_link_keys", "profile_dirs",
        mode="before",
    )
    @classmethod
    def _lists(cls, value: Any) -> Any:
        return _split_list(value)

    @field_validator("request_state_keys", "owner_link_keys")
    @classmethod
    def _key_length(cls, keys: list[SecretStr]) -> list[SecretStr]:
        if not keys:
            raise ValueError("at least one key is required")
        for key in keys:
            if len(key.get_secret_value().encode()) < MIN_KEY_BYTES:
                raise ValueError(f"keys must be at least {MIN_KEY_BYTES} bytes")
        return keys

    @model_validator(mode="before")
    @classmethod
    def _default_bind_host(cls, data: Any) -> Any:
        """Unset host: 0.0.0.0 in prod, loopback elsewhere (see `host`)."""
        if isinstance(data, dict) and "host" not in data and data.get("env") == "prod":
            return {**data, "host": PROD_BIND_HOST}
        return data

    @model_validator(mode="after")
    def _guard(self) -> Settings:
        problems = self.problems()
        if problems:
            raise UnsafeSettings(problems)
        return self

    def problems(self) -> list[str]:
        """Every reason these settings must not start (empty = fine), most important first."""
        found = [*self._pattern_problems()]
        if self.env == "prod":
            found = [*self._prod_problems(), *found]
        elif self.looks_deployed:
            # Fail closed: a deployment that forgot SHOPFRONT_ENV=prod must not run on published keys.
            found = [*self._deployed_problems(), *found]
        return found

    @property
    def looks_deployed(self) -> bool:
        """True when anything but `env` says this is a real deployment: DynamoDB storage, or a
        non-local name (dotted, an IP address or a wildcard) in the public base URL, allowed hosts,
        allowed origins or CORS origins. Loopback and single-label names (`testserver`) stay local."""
        if self.storage == "dynamodb" or is_public_url(self.public_base_url):
            return True
        names = (*self.allowed_hosts, *self.allowed_origins, *self.cors_origins)
        return any(not is_local_name(name) for name in names)

    def dev_secrets_in_use(self) -> list[str]:
        """Names of the settings that still hold a DEV-ONLY default (never the values)."""
        names: list[str] = []
        if any(k.get_secret_value() in _DEV_SECRETS for k in self.request_state_keys):
            names.append("request_state_keys")
        if any(k.get_secret_value() in _DEV_SECRETS for k in self.owner_link_keys):
            names.append("owner_link_keys")
        if self.id_key.get_secret_value() in _DEV_SECRETS:
            names.append("id_key")
        return names

    def _dev_secret_problem(self) -> str | None:
        names = self.dev_secrets_in_use()
        if not names:
            return None
        why = "SHOPFRONT_ENV=prod" if self.env == "prod" else "this looks like a real deployment"
        return (
            f"settings use a DEV-ONLY secret ({', '.join(names)}) although {why}; set SHOPFRONT_*_KEY(S) "
            "from a secret store (generate: python -c \"import secrets; print(secrets.token_urlsafe(48))\")"
        )

    def _weak_admin_token_problem(self) -> str | None:
        token = self.admin_token.get_secret_value()
        if token != DEV_ADMIN_TOKEN and len(token.encode()) < MIN_ADMIN_TOKEN_BYTES:
            return (
                f"SHOPFRONT_ADMIN_TOKEN must be at least {MIN_ADMIN_TOKEN_BYTES} bytes "
                "(or unset, which turns the admin route off)"
            )
        return None

    def _deployed_problems(self) -> list[str]:
        return [p for p in (self._dev_secret_problem(), self._weak_admin_token_problem()) if p]

    def _pattern_problems(self) -> list[str]:
        """Allow-list entries the MCP transport can never match (checked in every env)."""
        found = [
            f"SHOPFRONT_ALLOWED_HOSTS entry {host!r} {why}; {_HOST_HINT}"
            for host in self.allowed_hosts
            if (why := host_pattern_problem(host))
        ]
        found += [
            f"SHOPFRONT_ALLOWED_ORIGINS entry {origin!r} {why}; {_ORIGIN_HINT}"
            for origin in self.allowed_origins
            if (why := origin_pattern_problem(origin))
        ]
        return found

    def _prod_problems(self) -> list[str]:
        found = [p for p in (self._dev_secret_problem(),) if p]
        if self.storage != "dynamodb":
            found.append("prod requires SHOPFRONT_STORAGE=dynamodb (in-memory state is lost per replica)")
        if self.strict_invariants is False:
            found.append("prod requires strict profile invariants; unset SHOPFRONT_STRICT_INVARIANTS")
        if not any(not is_loopback(host) for host in self.allowed_hosts):
            found.append(
                "prod requires SHOPFRONT_ALLOWED_HOSTS to name the public host (e.g. mcp.example.com); "
                "the localhost-only default rejects every proxied /mcp request with 421"
            )
        found += _portless_problems(self.allowed_hosts, setting="ALLOWED_HOSTS", origin=False)
        found += _portless_problems(self.allowed_origins, setting="ALLOWED_ORIGINS", origin=True)
        if urlsplit(self.public_base_url).scheme != "https" or not is_public_url(self.public_base_url):
            found.append(
                f"prod requires SHOPFRONT_PUBLIC_BASE_URL to be the public https URL owners open "
                f"(e.g. https://mcp.example.com), not {self.public_base_url!r}: owner approval links "
                "are built from it and carry bearer tokens"
            )
        rejected = [o for o in self.cors_origins if o != "*" and not origin_allowed(o, self.allowed_origins)]
        if rejected:
            found.append(
                f"SHOPFRONT_CORS_ORIGINS {rejected} are not admitted by SHOPFRONT_ALLOWED_ORIGINS, so the "
                "MCP transport answers those browsers with 403; add them to SHOPFRONT_ALLOWED_ORIGINS"
            )
        if weak := self._weak_admin_token_problem():
            found.append(weak)
        return found

    def validate_for_prod(self) -> None:
        """Raise `UnsafeSettings` (a ValueError) listing every reason these settings cannot run in prod.

        Prod needs real secrets, DynamoDB storage, strict profile invariants (a profile edited after
        certification must never be served), an `allowed_hosts` entry that matches the public Host
        header (otherwise every /mcp request answers 421 while /health stays green), a public https
        `public_base_url` (owner links), CORS origins the transport admits and, when the admin route is
        on, an admin token of at least 32 bytes.
        """
        problems = [*self._prod_problems(), *self._pattern_problems()]
        if problems:
            raise UnsafeSettings(problems)

    @property
    def invariants_strict(self) -> bool:
        """Effective invariant policy: the explicit setting, else strict in prod and in anything that
        looks deployed (a forgotten SHOPFRONT_ENV must not serve a tampered live profile)."""
        if self.strict_invariants is not None:
            return self.strict_invariants
        return self.env == "prod" or self.looks_deployed

    @property
    def admin_enabled(self) -> bool:
        """The admin endpoint is off in prod, and in anything that looks deployed, unless a real token
        was configured (the DEV-ONLY token is published in this file)."""
        dev_token = self.admin_token.get_secret_value() == DEV_ADMIN_TOKEN
        return not (dev_token and (self.env == "prod" or self.looks_deployed))

    def request_state_key_bytes(self) -> list[bytes]:
        return [k.get_secret_value().encode() for k in self.request_state_keys]

    def owner_link_key_bytes(self) -> list[bytes]:
        return [k.get_secret_value().encode() for k in self.owner_link_keys]

    def id_key_bytes(self) -> bytes:
        return self.id_key.get_secret_value().encode()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings from the environment (cached)."""
    return Settings()
