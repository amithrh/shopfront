"""`canonical`: RFC 8785 serialisation and the L7 profile hash (kept in lockstep with the certifier)."""

from __future__ import annotations

import hashlib
from typing import Any

from shopfront_server.canonical import canonical_bytes, profile_sha256, sha256_hex
from shopfront_server.profile.model import Profile, freeze


def test_canonical_bytes_sort_keys_and_drop_whitespace() -> None:
    assert canonical_bytes({"b": 1, "a": [True, None, "é"]}) == '{"a":[true,null,"é"],"b":1}'.encode()
    assert sha256_hex({"b": 1, "a": 2}) == sha256_hex({"a": 2, "b": 1})
    assert sha256_hex({"a": 1}) == hashlib.sha256(b'{"a":1}').hexdigest()


def test_profile_sha_excludes_only_certification(example_raw: dict[str, Any]) -> None:
    base = profile_sha256(example_raw)
    assert profile_sha256({**example_raw, "certification": {"status": "draft", "runs": []}}) == base
    assert profile_sha256({**example_raw, "revision": 7}) != base
    assert profile_sha256({**example_raw, "x-note": "extensions are part of the hash"}) != base


def test_frozen_documents_hash_like_plain_ones(example_raw: dict[str, Any]) -> None:
    assert profile_sha256(freeze(example_raw)) == profile_sha256(example_raw)
    assert Profile.unchecked(example_raw).sha256 == profile_sha256(Profile.unchecked(example_raw).raw)
