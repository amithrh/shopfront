"""Canonical JSON (RFC 8785 / JCS) and hashing, used for idempotency keys and profile binding (L7)."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import rfc8785


def canonical_bytes(value: Any) -> bytes:
    """RFC 8785 canonical serialisation of a JSON-compatible value."""
    return rfc8785.dumps(value)


def sha256_hex(value: Any) -> str:
    """Hex SHA-256 of the canonical serialisation of `value`."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def profile_sha256(profile: Mapping[str, Any]) -> str:
    """Profile hash bound by certification runs (L7): sha256(JCS(profile without `certification`)).

    The onboarding agent and the certifier compute the same function; keep them in lockstep.
    """
    body = {k: v for k, v in profile.items() if k != "certification"}
    return sha256_hex(body)
