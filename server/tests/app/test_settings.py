"""Settings: env parsing, key-length rules and the prod guard against DEV-ONLY secrets."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from askonce_server.config import (
    DEV_ADMIN_TOKEN,
    DEV_ID_KEY,
    DEV_OWNER_LINK_KEY,
    DEV_REQUEST_STATE_KEY,
    MCP_PATH,
    SERVER_NAME,
    Settings,
    UnsafeSettings,
    host_pattern_problem,
    is_local_name,
    origin_pattern_problem,
)

REAL = {
    "request_state_keys": [SecretStr("prod-request-state-key-0123456789abcdef")],
    "owner_link_keys": [SecretStr("prod-owner-link-key-0123456789abcdef0123")],
    "id_key": SecretStr("prod-id-key-0123456789abcdef0123456789"),
}


PROD_BASE: dict[str, Any] = {
    "env": "prod",
    "storage": "dynamodb",
    "allowed_hosts": ["mcp.example.com"],
    "public_base_url": "https://mcp.example.com",
}


def prod(**overrides: Any) -> Settings:
    return Settings(**{**PROD_BASE, **REAL, **overrides})


def test_fixed_names() -> None:
    assert MCP_PATH == "/mcp" and SERVER_NAME == "askonce"


def test_dev_defaults_work_out_of_the_box() -> None:
    settings = Settings()
    assert settings.env == "dev" and settings.storage == "memory"
    assert (settings.host, settings.port) == ("127.0.0.1", 8000)  # dev binds loopback (published keys)
    assert settings.aws_region == "ap-southeast-2"
    assert settings.admin_enabled
    assert settings.request_state_key_bytes() == [DEV_REQUEST_STATE_KEY.encode()]


def test_prod_accepts_real_secrets_and_dynamodb() -> None:
    settings = prod()
    assert settings.env == "prod"
    assert not settings.admin_enabled  # the dev admin token disables the admin route in prod
    assert prod(admin_token=SecretStr("a-real-admin-token-0123456789abcdef")).admin_enabled
    assert settings.invariants_strict  # unset means strict in prod


def test_prod_refuses_a_short_admin_token() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        prod(admin_token=SecretStr("admin"))


def test_prod_refuses_lenient_invariants() -> None:
    """A live profile edited after certification (L7) must never be served in prod."""
    with pytest.raises(ValueError, match="strict"):
        prod(strict_invariants=False)
    assert prod(strict_invariants=True).invariants_strict
    assert not Settings().invariants_strict and Settings(strict_invariants=True).invariants_strict


@pytest.mark.parametrize(
    "hosts", [["localhost:*", "127.0.0.1:*"], ["[::1]:8000"], ["localhost"], ["app.localhost:*"]]
)
def test_prod_refuses_localhost_only_allowed_hosts(hosts: list[str]) -> None:
    """Behind any proxy the Host header is the public name: localhost-only lists answer 421 to every /mcp call."""
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        prod(allowed_hosts=hosts)
    assert prod(allowed_hosts=[*hosts, "mcp.example.com"]).allowed_hosts[-1] == "mcp.example.com"


@pytest.mark.parametrize(
    "deployed",
    [{"storage": "dynamodb"}, {"public_base_url": "https://mcp.example.com"}],
)
def test_dev_secrets_are_refused_when_the_deployment_looks_real_even_without_env_prod(deployed: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="DEV-ONLY"):
        Settings(**deployed)
    settings = Settings(**deployed, **REAL)  # env still "dev"
    assert settings.looks_deployed
    assert not settings.admin_enabled  # the published admin token is off as well
    assert settings.dev_secrets_in_use() == []


def test_local_dev_keeps_working_with_dev_secrets() -> None:
    for url in ("http://localhost:8000", "http://testserver", "http://127.0.0.1:9000"):
        settings = Settings(public_base_url=url)
        assert not settings.looks_deployed and settings.admin_enabled
        assert settings.dev_secrets_in_use() == ["request_state_keys", "owner_link_keys", "id_key"]
    assert DEV_ID_KEY in {Settings().id_key.get_secret_value()}


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_state_keys": [SecretStr(DEV_REQUEST_STATE_KEY)]},
        {"owner_link_keys": [*REAL["owner_link_keys"], SecretStr(DEV_OWNER_LINK_KEY)]},  # a dev key kept for rotation
        {"id_key": SecretStr("DEV-ONLY-id-derivation-key-0123456789abcdef01")},
    ],
)
def test_prod_refuses_any_dev_secret(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="DEV-ONLY"):
        prod(**overrides)


def test_prod_refuses_memory_storage() -> None:
    with pytest.raises(ValueError, match="dynamodb"):
        prod(storage="memory")


def test_dev_admin_token_is_fine_outside_prod() -> None:
    assert Settings(env="test", admin_token=SecretStr(DEV_ADMIN_TOKEN)).admin_enabled


def test_list_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = '["k1-0123456789abcdef0123456789abcd", "k2-0123456789abcdef0123456789abcd"]'
    monkeypatch.setenv("ASKONCE_OWNER_LINK_KEYS", keys)
    # Public CORS origins mean "deployed": every DEV-ONLY key is refused, not just the link key.
    monkeypatch.setenv("ASKONCE_REQUEST_STATE_KEYS", "env-request-state-key-0123456789abcdef")
    monkeypatch.setenv("ASKONCE_ID_KEY", "env-id-key-0123456789abcdef0123456789")
    monkeypatch.setenv("ASKONCE_CORS_ORIGINS", "https://owner.example, https://host.example")
    monkeypatch.setenv("ASKONCE_SWEEP_INTERVAL_S", "5")
    settings = Settings()
    assert [k.get_secret_value()[:2] for k in settings.owner_link_keys] == ["k1", "k2"]
    assert settings.cors_origins == ["https://owner.example", "https://host.example"]
    assert settings.sweep_interval_s == 5


def test_short_link_keys_are_refused() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Settings(owner_link_keys=[SecretStr("short")])
    with pytest.raises(ValueError):
        Settings(owner_link_keys=[])


def test_secrets_are_not_printed() -> None:
    text = repr(prod())
    for secret in REAL.values():
        values = secret if isinstance(secret, list) else [secret]
        for value in values:
            assert value.get_secret_value() not in text


# --------------------------------------------------------------------------- round 2: prod safety
# A deployment is classified fail-closed (any public name anywhere counts), prod refuses settings
# that would 421/403 every real request or mail localhost links, and errors never echo secrets.

SHORT_REAL_ADMIN = SecretStr("admin")
REAL_ADMIN = SecretStr("a-real-admin-token-0123456789abcdef")


@pytest.mark.parametrize(
    "public",
    [
        {"allowed_hosts": ["mcp.example.com"]},
        {"allowed_hosts": ["localhost:*", "192.168.1.50:8000"]},  # a LAN address is reachable too
        {"allowed_origins": ["http://localhost:*", "https://app.example.com"]},
        {"cors_origins": ["https://owner.example.com"]},
        {"cors_origins": ["*"]},  # allow-all counts as public
        {"public_base_url": "https://mcp.example.com"},
        {"storage": "dynamodb"},
    ],
)
def test_round2_prod_safety_public_allowed_host_or_origin_counts_as_deployed(public: dict[str, Any]) -> None:
    """SEC-5 R1: with ASKONCE_ENV forgotten, a public Host/Origin/CORS entry alone means "deployed"."""
    with pytest.raises(ValueError, match="DEV-ONLY"):
        Settings(**public)
    settings = Settings(**public, **REAL)
    assert settings.env == "dev" and settings.looks_deployed
    assert not settings.admin_enabled  # the published admin token is off
    assert settings.invariants_strict  # G5: a tampered live profile is rejected, not served


@pytest.mark.parametrize(
    "local",
    [
        {"allowed_hosts": ["testserver", "localhost:*"]},
        {"allowed_hosts": ["askonce:8000", "[::1]:8000", "app.localhost:*"]},
        {"cors_origins": ["http://localhost:6274", "http://askonce:8000"]},
        {"public_base_url": "http://testserver"},
        {"public_base_url": "http://askonce:8000"},
        {},
    ],
)
def test_round2_prod_safety_local_names_keep_dev_ergonomics(local: dict[str, Any]) -> None:
    settings = Settings(**local)
    assert not settings.looks_deployed
    assert settings.dev_secrets_in_use() == ["request_state_keys", "owner_link_keys", "id_key"]
    assert settings.admin_enabled and not settings.invariants_strict
    assert Settings(**local, admin_token=SHORT_REAL_ADMIN).admin_enabled  # short tokens are fine locally


@pytest.mark.parametrize(
    ("value", "local"),
    [
        ("localhost:*", True), ("127.0.0.1:*", True), ("[::1]:8000", True), ("testserver", True),
        ("http://localhost:6274", True), ("http://askonce:8000", True), ("x.localhost", True),
        ("mcp.example.com", False), ("10.0.0.5:8000", False), ("*", False), ("*.example.com", False),
        ("https://app.example.com", False), ("[2001:db8::1]:8000", False), ("http://[2001:db8::1]", False),
    ],
)
def test_round2_prod_safety_is_local_name(value: str, local: bool) -> None:
    assert is_local_name(value) is local


def test_round2_prod_safety_deployed_non_prod_refuses_short_admin_token() -> None:
    """SEC-5 R3: the 32-byte rule applies wherever the deployment looks real, not only in prod."""
    for deployed in ({"storage": "dynamodb"}, {"env": "test", "allowed_hosts": ["mcp.example.com"]}):
        with pytest.raises(ValueError, match="ADMIN_TOKEN must be at least 32 bytes"):
            Settings(**deployed, **REAL, admin_token=SHORT_REAL_ADMIN)
        assert Settings(**deployed, **REAL, admin_token=REAL_ADMIN).admin_enabled
        assert not Settings(**deployed, **REAL).admin_enabled  # the DEV token is simply off


def test_round2_prod_safety_deployed_non_prod_is_strict_by_default() -> None:
    """G5: a forgotten (or `test`) env on a real deployment still rejects tampered live profiles."""
    for deployed in (
        {"storage": "dynamodb"},
        {"env": "test", "public_base_url": "https://mcp.example.com"},
        {"env": "test", "cors_origins": ["https://owner.example.com"]},
    ):
        assert Settings(**deployed, **REAL).invariants_strict
        # An explicit choice still wins outside prod (tests, a staging run of the example profile).
        assert not Settings(**deployed, **REAL, strict_invariants=False).invariants_strict
    assert not Settings().invariants_strict  # local dev stays lenient (example-profile.json fails strict L7)


@pytest.mark.parametrize(
    ("hosts", "hint"),
    [
        (["*"], "bare '\\*'"),
        (["https://mcp.example.com"], "scheme"),
        (["*.example.com"], "wildcard"),
        (["mcp.example.com/"], "path"),
        (["mcp.example.com:443"], "only matches requests that name a port"),  # clients omit :443
        (["mcp.example.com:80"], "only matches requests that name a port"),
        (["mcp.example.com:*"], "only matches requests that name a port"),  # ':*' needs an explicit port
        (["mcp.example.com:*", "mcp.example.com:443"], "add 'mcp.example.com'"),
        (["mcp.example.com bad"], "not a host name"),
    ],
)
def test_round2_prod_safety_prod_refuses_malformed_allowed_hosts(hosts: list[str], hint: str) -> None:
    """F9: entries that pass "any non-loopback" but never match a Host header (421 on every /mcp)."""
    with pytest.raises(ValueError, match=f"ALLOWED_HOSTS entry .*{hint}"):
        prod(allowed_hosts=hosts)


def test_round2_prod_safety_well_formed_allowed_hosts_are_accepted() -> None:
    for hosts in (
        ["mcp.example.com"],
        ["mcp.example.com:8443"],  # a non-default port: clients send it
        ["mcp.example.com", "mcp.example.com:443"],  # explicit ports are harmless next to the bare name
        ["mcp.example.com", "mcp.example.com:*", "localhost:*"],
        ["10.0.0.12:8000", "mcp.example.com"],
    ):
        assert prod(allowed_hosts=hosts).allowed_hosts == hosts


@pytest.mark.parametrize("env", ["dev", "test"])
def test_round2_prod_safety_malformed_patterns_are_refused_in_every_env(env: str) -> None:
    """A pattern the transport can never match is a startup error everywhere, not a runtime 421/403."""
    with pytest.raises(ValueError, match="ALLOWED_HOSTS entry '\\*'"):
        Settings(env=env, allowed_hosts=["*"], **REAL)
    with pytest.raises(ValueError, match="ALLOWED_ORIGINS entry 'https://app.example.com/'"):
        Settings(env=env, allowed_origins=["https://app.example.com/"], **REAL)
    # An explicit default port is only a problem where clients reach the server through its public name.
    assert Settings(env=env, allowed_hosts=["localhost:443"]).allowed_hosts == ["localhost:443"]


@pytest.mark.parametrize(
    ("origins", "hint"),
    [
        (["*"], "bare '\\*'"),
        (["app.example.com"], "no scheme"),
        (["https://*.example.com"], "wildcard"),
        (["https://app.example.com/"], "path"),
        (["https://app.example.com:443"], "only matches requests that name a port"),
        (["https://app.example.com:*"], "add 'https://app.example.com'"),
    ],
)
def test_round2_prod_safety_prod_refuses_malformed_allowed_origins(origins: list[str], hint: str) -> None:
    with pytest.raises(ValueError, match=f"ALLOWED_ORIGINS entry .*{hint}"):
        prod(allowed_origins=origins, cors_origins=[])


def test_round2_prod_safety_local_any_port_patterns_stay_valid_in_prod() -> None:
    """`localhost:*` / `http://localhost:*` serve local probes and tools, which always send a port."""
    settings = prod(
        allowed_hosts=["mcp.example.com", "localhost:*"],
        allowed_origins=["http://localhost:*"],
        cors_origins=["http://localhost:5173"],
    )
    assert settings.allowed_origins == ["http://localhost:*"]
    assert prod(allowed_origins=["https://app.example.com", "https://app.example.com:*"], cors_origins=[])


def test_round2_prod_safety_pattern_problem_helpers() -> None:
    assert host_pattern_problem("mcp.example.com") is None
    assert host_pattern_problem("[::1]:*") is None
    assert host_pattern_problem("localhost:*") is None
    assert origin_pattern_problem("https://app.example.com") is None
    assert origin_pattern_problem("http://localhost:*") is None
    assert origin_pattern_problem("http://[::1]:5173") is None


def test_round2_prod_safety_prod_requires_public_https_base_url() -> None:
    """Owner approval links carry bearer tokens and must reach the owner's phone."""
    for url in ("http://localhost:8000", "http://mcp.example.com", "https://localhost", "https://testserver", ""):
        with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
            prod(public_base_url=url)
    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        Settings(**{**PROD_BASE, **REAL, "public_base_url": "http://localhost:8000"})
    assert prod(public_base_url="https://mcp.example.com").public_base_url == "https://mcp.example.com"
    assert prod(public_base_url="https://shop.example.com/owners").public_base_url.endswith("/owners")


def test_round2_prod_safety_prod_refuses_cors_origin_rejected_by_transport() -> None:
    """F9: prod used to log this and then answer the browser host and MCP Apps with 403."""
    with pytest.raises(ValueError, match=r"CORS_ORIGINS \['https://host.example.com'\] are not admitted"):
        prod(cors_origins=["https://host.example.com"])
    settings = prod(cors_origins=["https://host.example.com"], allowed_origins=["https://host.example.com"])
    assert settings.cors_origins == ["https://host.example.com"]
    assert prod(cors_origins=["*"]).cors_origins == ["*"]  # allow-all CORS is not a transport mismatch
    # Outside prod the mismatch is only logged (app/main.py), so local experiments keep working.
    assert Settings(cors_origins=["http://localhost:6274"], allowed_origins=["http://127.0.0.1:*"])


def test_round2_prod_safety_prod_reports_every_problem_at_once() -> None:
    with pytest.raises(ValueError) as caught:
        Settings(env="prod", admin_token=SHORT_REAL_ADMIN, allowed_hosts=["*"])
    text = str(caught.value)
    for needle in ("DEV-ONLY", "dynamodb", "ALLOWED_HOSTS entry '*'", "PUBLIC_BASE_URL", "32 bytes"):
        assert needle in text, needle
    (error,) = caught.value.errors(include_input=False)  # type: ignore[attr-defined]
    cause = error["ctx"]["error"]
    assert isinstance(cause, UnsafeSettings) and len(cause.problems) == 5
    with pytest.raises(ValueError, match="DEV-ONLY") as plain:
        Settings(env="prod")
    for needle in ("dynamodb", "ALLOWED_HOSTS", "PUBLIC_BASE_URL"):
        assert needle in str(plain.value), needle
    with pytest.raises(UnsafeSettings) as direct:  # validate_for_prod lists them too
        Settings().validate_for_prod()
    assert len(direct.value.problems) == 4


def test_round2_prod_safety_validation_errors_never_echo_secret_input() -> None:
    """Startup errors go to stderr/CloudWatch: they must not carry the raw input (secrets)."""
    marker = "SECRET-MARKER-" + "D" * 40
    cases: list[dict[str, Any]] = [
        {"env": "prod", "storage": "memory", **REAL, "admin_token": SecretStr(marker)},
        {"env": "prod", **REAL, "admin_token": SecretStr(marker[:20])},  # short -> refused
        {"owner_link_keys": [SecretStr(marker[:20])]},  # field-level error (too short)
        {"request_state_keys": [SecretStr(marker[:20])]},
        {"id_key": SecretStr(marker), "storage": "dynamodb"},  # model-level error, secret elsewhere in input
    ]
    for case in cases:
        with pytest.raises(ValueError) as caught:
            Settings(**case)
        for rendering in (str(caught.value), repr(caught.value)):
            assert marker[:20] not in rendering
            assert "input_value" not in rendering


def test_round2_prod_safety_bind_host_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-5 R2: dev/test bind loopback (the DEV-ONLY keys are published); prod binds every interface."""
    assert Settings().host == "127.0.0.1"
    assert Settings(env="test").host == "127.0.0.1"
    assert prod().host == "0.0.0.0"
    assert prod(host="10.0.0.7").host == "10.0.0.7"  # explicit wins
    assert Settings(host="0.0.0.0").host == "0.0.0.0"  # e.g. the image's ASKONCE_HOST in a dev container
    monkeypatch.setenv("ASKONCE_ENV", "prod")
    monkeypatch.setenv("ASKONCE_STORAGE", "dynamodb")
    monkeypatch.setenv("ASKONCE_ALLOWED_HOSTS", "mcp.example.com")
    monkeypatch.setenv("ASKONCE_PUBLIC_BASE_URL", "https://mcp.example.com")
    monkeypatch.setenv("ASKONCE_REQUEST_STATE_KEYS", "env-request-state-key-0123456789abcdef")
    monkeypatch.setenv("ASKONCE_OWNER_LINK_KEYS", "env-owner-link-key-0123456789abcdef0123")
    monkeypatch.setenv("ASKONCE_ID_KEY", "env-id-key-0123456789abcdef0123456789")
    assert Settings().host == "0.0.0.0"  # env from the environment, host unset
    monkeypatch.setenv("ASKONCE_HOST", "127.0.0.1")
    assert Settings().host == "127.0.0.1"
