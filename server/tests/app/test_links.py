"""Owner link tokens: signing, verification, scope, expiry, rotation, URLs and the mint CLI."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from support import FIXED_NOW

from askonce_server.app.links import (
    AUTH_FAILED,
    MAX_TOKEN_LENGTH,
    OwnerLinkSigner,
    key_fingerprint,
    main,
)
from askonce_server.clock import FixedClock
from askonce_server.errors import AuthError

KEY_A = b"link-key-A-0123456789abcdef0123456789"
KEY_B = b"link-key-B-0123456789abcdef0123456789"
BIZ = "thimbleberry-bakehouse"
OTHER = "quillfern-salon"


def make_signer(clock: FixedClock, *keys: bytes, ttl: int = 3600) -> OwnerLinkSigner:
    return OwnerLinkSigner(keys or (KEY_A,), clock=clock, default_ttl_s=ttl, public_base_url="https://shop.example/")


@pytest.fixture
def signer(clock: FixedClock) -> OwnerLinkSigner:
    return make_signer(clock)


def token_of(url: str) -> str:
    return parse_qs(urlsplit(url).query)["token"][0]


def rejects(signer: OwnerLinkSigner, token: str, *, business_id: str = BIZ, commitment_id: str | None = None) -> None:
    with pytest.raises(AuthError) as info:
        signer.verify(token, business_id=business_id, commitment_id=commitment_id)
    assert str(info.value) == AUTH_FAILED  # one generic message for every failure


# --------------------------------------------------------------------------- round trip


def test_inbox_token_round_trip(signer: OwnerLinkSigner) -> None:
    token = signer.sign(business_id=BIZ, scope="inbox")
    claims = signer.verify(token, business_id=BIZ)
    assert claims.biz == BIZ and claims.scope == "inbox" and claims.cid is None and claims.v == 1
    assert claims.exp == int(FIXED_NOW.timestamp()) + 3600
    assert claims.kid == key_fingerprint(KEY_A) == signer.signing_kid


def test_token_is_url_safe_and_compact(signer: OwnerLinkSigner) -> None:
    token = signer.sign(business_id=BIZ, scope="decide", commitment_id="c_abc", ttl_s=60)
    assert token.count(".") == 1 and "=" not in token
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")
    assert len(token) < 300


def test_inbox_token_authorises_any_commitment_of_its_business(signer: OwnerLinkSigner) -> None:
    token = signer.sign(business_id=BIZ, scope="inbox")
    assert signer.verify(token, business_id=BIZ, commitment_id="c_any").scope == "inbox"


def test_explicit_ttl_overrides_default(signer: OwnerLinkSigner, clock: FixedClock) -> None:
    token = signer.sign(business_id=BIZ, scope="inbox", ttl_s=120)
    clock.advance(timedelta(seconds=119))
    signer.verify(token, business_id=BIZ)
    clock.advance(timedelta(seconds=1))
    rejects(signer, token)


# --------------------------------------------------------------------------- failures


def test_expired_token_is_rejected(signer: OwnerLinkSigner, clock: FixedClock) -> None:
    token = signer.sign(business_id=BIZ, scope="inbox")
    clock.advance(timedelta(seconds=3600))
    rejects(signer, token)


def test_wrong_business_is_rejected(signer: OwnerLinkSigner) -> None:
    token = signer.sign(business_id=BIZ, scope="inbox")
    rejects(signer, token, business_id=OTHER)
    rejects(signer, token, business_id=BIZ + "x")


def test_tampered_claims_are_rejected(signer: OwnerLinkSigner) -> None:
    token = signer.sign(business_id=OTHER, scope="inbox")
    payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["biz"] = BIZ
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    rejects(signer, f"{forged}.{signature}")


def test_tampered_signature_is_rejected(signer: OwnerLinkSigner) -> None:
    token = signer.sign(business_id=BIZ, scope="inbox")
    payload, signature = token.split(".")
    flipped = ("A" if signature[0] != "A" else "B") + signature[1:]
    rejects(signer, f"{payload}.{flipped}")
    rejects(signer, f"{payload}.")
    rejects(signer, payload)


@pytest.mark.parametrize(
    "token",
    ["", ".", "a.b.c", "not base64!.x", "x" * (MAX_TOKEN_LENGTH + 1), "e30.e30", "%%%.%%%"],
)
def test_malformed_tokens_are_rejected(signer: OwnerLinkSigner, token: str) -> None:
    rejects(signer, token)


def test_signature_from_another_key_is_rejected(clock: FixedClock) -> None:
    attacker = make_signer(clock, KEY_B)
    rejects(make_signer(clock, KEY_A), attacker.sign(business_id=BIZ, scope="inbox"))


def test_validly_signed_but_malformed_claims_are_rejected(signer: OwnerLinkSigner) -> None:
    import hashlib
    import hmac

    def forge(claims: dict[str, object]) -> str:
        part = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        mac = hmac.new(KEY_A, part.encode(), hashlib.sha256).digest()
        return f"{part}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"

    exp = int(FIXED_NOW.timestamp()) + 60
    assert signer.verify(forge({"v": 1, "biz": BIZ, "scope": "inbox", "cid": None, "exp": exp, "kid": 0}),
                         business_id=BIZ)
    rejects(signer, forge({"v": 2, "biz": BIZ, "scope": "inbox", "cid": None, "exp": exp, "kid": 0}))
    rejects(signer, forge({"v": 1, "biz": BIZ, "scope": "admin", "cid": None, "exp": exp, "kid": 0}))
    rejects(signer, forge({"v": 1, "biz": BIZ, "scope": "inbox", "cid": "c_x", "exp": exp, "kid": 0}))
    rejects(signer, forge({"v": 1, "biz": BIZ, "scope": "inbox", "exp": exp, "extra": 1}))
    rejects(signer, forge({"v": 1, "biz": BIZ, "scope": "inbox"}))
    rejects(signer, forge(["not", "an", "object"]))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- decide scope


def test_decide_token_is_bound_to_one_commitment(signer: OwnerLinkSigner) -> None:
    token = signer.sign(business_id=BIZ, scope="decide", commitment_id="c_one")
    assert signer.verify(token, business_id=BIZ, commitment_id="c_one").cid == "c_one"
    rejects(signer, token, commitment_id="c_two")
    rejects(signer, token, commitment_id=None)  # cannot read the whole inbox
    rejects(signer, token, business_id=OTHER, commitment_id="c_one")


def test_sign_validates_scope_arguments(signer: OwnerLinkSigner) -> None:
    with pytest.raises(ValueError):
        signer.sign(business_id=BIZ, scope="decide")
    with pytest.raises(ValueError):
        signer.sign(business_id=BIZ, scope="inbox", commitment_id="c_x")
    with pytest.raises(ValueError):
        signer.sign(business_id="", scope="inbox")
    with pytest.raises(ValueError):
        signer.sign(business_id=BIZ, scope="inbox", ttl_s=0)


def test_constructor_validates_keys_and_ttl(clock: FixedClock) -> None:
    with pytest.raises(ValueError):
        OwnerLinkSigner([], clock=clock, default_ttl_s=60, public_base_url="http://x")
    with pytest.raises(ValueError, match="32 bytes"):
        OwnerLinkSigner([b"short"], clock=clock, default_ttl_s=60, public_base_url="http://x")
    with pytest.raises(ValueError):
        OwnerLinkSigner([KEY_A], clock=clock, default_ttl_s=0, public_base_url="http://x")


# --------------------------------------------------------------------------- rotation


def test_key_rotation_first_signs_all_verify(clock: FixedClock) -> None:
    old = make_signer(clock, KEY_A)
    old_token = old.sign(business_id=BIZ, scope="inbox")

    rotated = make_signer(clock, KEY_B, KEY_A)  # new key prepended, old key kept for verification
    assert rotated.verify(old_token, business_id=BIZ).kid == key_fingerprint(KEY_A)
    new_token = rotated.sign(business_id=BIZ, scope="inbox")
    assert rotated.verify(new_token, business_id=BIZ).kid == key_fingerprint(KEY_B)
    rejects(old, new_token)  # replicas that do not know the new key yet reject its links

    retired = make_signer(clock, KEY_B)  # old key removed: its links stop working
    rejects(retired, old_token)
    assert retired.verify(new_token, business_id=BIZ)


def test_fingerprints_differ_per_key() -> None:
    assert key_fingerprint(KEY_A) != key_fingerprint(KEY_B)
    assert 0 <= key_fingerprint(KEY_A) < 2**32


# --------------------------------------------------------------------------- URLs


def test_inbox_url(signer: OwnerLinkSigner) -> None:
    url = signer.inbox_url(BIZ)
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"https://shop.example/owner/{BIZ}/inbox"
    assert signer.verify(token_of(url), business_id=BIZ).scope == "inbox"


def test_decision_url(signer: OwnerLinkSigner) -> None:
    url = signer.decision_url(BIZ, "c_abc", ttl_s=60)
    assert urlsplit(url).path == f"/owner/{BIZ}/requests/c_abc"
    claims = signer.verify(token_of(url), business_id=BIZ, commitment_id="c_abc")
    assert claims.scope == "decide" and claims.exp == int(FIXED_NOW.timestamp()) + 60


def test_urls_quote_path_segments(signer: OwnerLinkSigner) -> None:
    assert "/owner/a%2Fb/inbox" in signer.inbox_url("a/b")


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def link_env(monkeypatch: pytest.MonkeyPatch) -> str:
    key = "cli-test-link-key-0123456789abcdef0123"
    monkeypatch.setenv("ASKONCE_OWNER_LINK_KEYS", key)
    monkeypatch.setenv("ASKONCE_PUBLIC_BASE_URL", "https://owner.example")
    # A public base URL means "deployed": every DEV-ONLY key is refused, not just the link key.
    monkeypatch.setenv("ASKONCE_REQUEST_STATE_KEYS", "cli-test-request-state-key-0123456789abcdef")
    monkeypatch.setenv("ASKONCE_ID_KEY", "cli-test-id-key-0123456789abcdef0123456789")
    return key


def cli_signer(key: str) -> OwnerLinkSigner:
    from askonce_server.clock import SystemClock

    return OwnerLinkSigner([key.encode()], clock=SystemClock(), default_ttl_s=60, public_base_url="https://x")


def test_cli_mints_an_inbox_link(link_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mint", "--business", BIZ, "--ttl", "600"]) == 0
    url = capsys.readouterr().out.strip()
    assert url.startswith(f"https://owner.example/owner/{BIZ}/inbox?token=")
    assert cli_signer(link_env).verify(token_of(url), business_id=BIZ).scope == "inbox"


def test_cli_mints_a_decision_link(link_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mint", "--business", BIZ, "--commitment", "c_abc"]) == 0
    url = capsys.readouterr().out.strip()
    assert cli_signer(link_env).verify(token_of(url), business_id=BIZ, commitment_id="c_abc").scope == "decide"


def test_cli_reports_bad_input(link_env: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mint", "--business", BIZ, "--ttl", "0"]) == 2
    assert "ttl_s must be positive" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["mint"])  # --business is required


def test_cli_runs_as_a_module(link_env: str) -> None:
    import os
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "askonce_server.app.links", "mint", "--business", BIZ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ},
        timeout=60,
    )
    assert out.stdout.startswith(f"https://owner.example/owner/{BIZ}/inbox?token=")
