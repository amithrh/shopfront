"""HMAC-signed, expiring owner links (the owner page has no login; the link is the credential).

Token format: `<base64url(claims JSON)>.<base64url(HMAC-SHA256(key, claims part))>` with claims
`{"v":1,"biz":<business_id>,"scope":"inbox"|"decide","cid":<commitment_id>|null,"exp":<epoch s>,"kid":<n>}`.

* Keys come from `Settings.owner_link_keys` (first signs, all verify: rotation without breaking links).
  The MAC is checked against every configured key *before* the claims are parsed, so no
  unauthenticated JSON is ever interpreted. `kid` is a 32-bit fingerprint of the signing key, recorded
  for audit only (it is not an index, so prepending a new key never invalidates old links).
* `verify` uses `hmac.compare_digest`, checks `exp` against the injected clock, the business id and the
  scope. An `inbox` token also authorises decisions on that business's commitments (the owner page holds
  one token); a `decide` token authorises exactly one commitment (deep links in notifications).
* Failures raise `errors.AuthError` with a generic message (no oracle about which check failed).

CLI (dev): `python -m shopfront_server.app.links mint --business thimbleberry-bakehouse [--ttl 604800]` prints an
inbox URL signed with the configured key (`--commitment <id>` prints a single-request decision URL).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import re
import sys
from collections.abc import Sequence
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shopfront_server.clock import Clock, SystemClock
from shopfront_server.config import MIN_KEY_BYTES, Settings
from shopfront_server.errors import AuthError

LinkScope = Literal["inbox", "decide"]

TOKEN_VERSION = 1
MAX_TOKEN_LENGTH = 1024
"""Longer tokens are rejected before any decoding (a signed token is ~200 characters)."""

AUTH_FAILED = "Invalid or expired link"
"""The only message any verification failure carries."""

_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_KID_DOMAIN = b"shopfront/owner-link-kid/v1|"


class LinkClaims(BaseModel):
    """The signed payload of an owner link token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    v: int = TOKEN_VERSION
    biz: str = Field(min_length=1, max_length=200)
    scope: LinkScope
    cid: str | None = Field(default=None, max_length=200)
    exp: int = Field(ge=0)
    """Expiry, epoch seconds (UTC)."""
    kid: int = 0
    """Fingerprint of the signing key (audit only)."""


def key_fingerprint(key: bytes) -> int:
    """32-bit, domain-separated fingerprint of a link key (safe to log; reveals nothing usable)."""
    return int.from_bytes(hashlib.sha256(_KID_DOMAIN + key).digest()[:4], "big")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    if not _B64URL.fullmatch(text):
        raise ValueError("not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not base64url") from exc


def _mac(key: bytes, signed_part: str) -> bytes:
    return hmac.new(key, signed_part.encode("ascii"), hashlib.sha256).digest()


class OwnerLinkSigner:
    """Signs and verifies owner link tokens and builds the owner page URLs that carry them."""

    def __init__(self, keys: Sequence[bytes], *, clock: Clock, default_ttl_s: int, public_base_url: str) -> None:
        if not keys:
            raise ValueError("at least one owner link key is required")
        for key in keys:
            if len(key) < MIN_KEY_BYTES:
                raise ValueError(f"owner link keys must be at least {MIN_KEY_BYTES} bytes")
        if default_ttl_s <= 0:
            raise ValueError("default_ttl_s must be positive")
        self._keys = tuple(bytes(k) for k in keys)
        self._clock = clock
        self._default_ttl_s = default_ttl_s
        self._base_url = public_base_url.rstrip("/")

    @property
    def signing_kid(self) -> int:
        """Fingerprint of the key that signs new links (the first configured key)."""
        return key_fingerprint(self._keys[0])

    def _now_s(self) -> int:
        return int(self._clock.now().timestamp())

    def sign(
        self, *, business_id: str, scope: LinkScope, commitment_id: str | None = None, ttl_s: int | None = None
    ) -> str:
        """A token for `business_id`. `decide` needs `commitment_id`; `inbox` must not have one."""
        if not business_id:
            raise ValueError("business_id is required")
        if scope == "decide" and not commitment_id:
            raise ValueError("a decide link needs a commitment_id")
        if scope == "inbox" and commitment_id is not None:
            raise ValueError("an inbox link is not bound to a commitment")
        ttl = self._default_ttl_s if ttl_s is None else ttl_s
        if ttl <= 0:
            raise ValueError("ttl_s must be positive")
        claims = LinkClaims(
            biz=business_id, scope=scope, cid=commitment_id, exp=self._now_s() + ttl, kid=self.signing_kid
        )
        payload = json.dumps(claims.model_dump(), separators=(",", ":"), sort_keys=True).encode()
        signed_part = _b64encode(payload)
        return f"{signed_part}.{_b64encode(_mac(self._keys[0], signed_part))}"

    def _authenticate(self, token: str) -> LinkClaims:
        """Check the MAC against every key, then parse the claims. Raises AuthError on any failure."""
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH or token.count(".") != 1:
            raise AuthError(AUTH_FAILED)
        signed_part, signature_part = token.split(".")
        try:
            signature = _b64decode(signature_part)
            _b64decode(signed_part)
        except ValueError:
            raise AuthError(AUTH_FAILED) from None
        matched = False
        for key in self._keys:  # every key is checked: timing does not reveal which one matched
            matched |= hmac.compare_digest(_mac(key, signed_part), signature)
        if not matched:
            raise AuthError(AUTH_FAILED)
        try:
            claims = LinkClaims.model_validate(json.loads(_b64decode(signed_part)))
        except (ValueError, ValidationError):
            raise AuthError(AUTH_FAILED) from None
        if claims.v != TOKEN_VERSION:
            raise AuthError(AUTH_FAILED)
        return claims

    def verify(self, token: str, *, business_id: str, commitment_id: str | None = None) -> LinkClaims:
        """Valid for this business (and, for decide tokens, this commitment). Raises errors.AuthError."""
        claims = self._authenticate(token)
        if claims.exp <= self._now_s():
            raise AuthError(AUTH_FAILED)
        if not hmac.compare_digest(claims.biz.encode(), business_id.encode()):
            raise AuthError(AUTH_FAILED)
        if claims.scope == "inbox":
            if claims.cid is not None:
                raise AuthError(AUTH_FAILED)
        elif commitment_id is None or claims.cid is None or not hmac.compare_digest(
            claims.cid.encode(), commitment_id.encode()
        ):
            raise AuthError(AUTH_FAILED)
        return claims

    def inbox_url(self, business_id: str, *, ttl_s: int | None = None) -> str:
        """`<public_base_url>/owner/<business_id>/inbox?token=...` (the owner page reads the token)."""
        token = self.sign(business_id=business_id, scope="inbox", ttl_s=ttl_s)
        return f"{self._base_url}/owner/{quote(business_id, safe='')}/inbox?token={token}"

    def decision_url(self, business_id: str, commitment_id: str, *, ttl_s: int | None = None) -> str:
        """`<public_base_url>/owner/<business_id>/requests/<commitment_id>?token=...` (one request only)."""
        token = self.sign(business_id=business_id, scope="decide", commitment_id=commitment_id, ttl_s=ttl_s)
        return (
            f"{self._base_url}/owner/{quote(business_id, safe='')}/requests/{quote(commitment_id, safe='')}"
            f"?token={token}"
        )


def signer_from_settings(settings: Settings, *, clock: Clock) -> OwnerLinkSigner:
    """The signer every replica builds from the same settings (so links verify on any replica)."""
    return OwnerLinkSigner(
        settings.owner_link_key_bytes(),
        clock=clock,
        default_ttl_s=settings.owner_link_ttl_s,
        public_base_url=settings.public_base_url,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m shopfront_server.app.links",
        description="Mint owner links signed with SHOPFRONT_OWNER_LINK_KEYS (base URL: SHOPFRONT_PUBLIC_BASE_URL).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    mint = commands.add_parser("mint", help="print an owner inbox link (or a single-request decision link)")
    mint.add_argument("--business", required=True, help="business (profile) id, e.g. thimbleberry-bakehouse")
    mint.add_argument("--ttl", type=int, default=None, help="lifetime in seconds (default SHOPFRONT_OWNER_LINK_TTL_S)")
    mint.add_argument("--commitment", default=None, help="mint a decision link for this one commitment id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """`mint` sub-command (see module docstring)."""
    args = _parser().parse_args(argv)
    try:
        settings = Settings()
        signer = signer_from_settings(settings, clock=SystemClock())
        if args.commitment:
            url = signer.decision_url(args.business, args.commitment, ttl_s=args.ttl)
        else:
            url = signer.inbox_url(args.business, ttl_s=args.ttl)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
