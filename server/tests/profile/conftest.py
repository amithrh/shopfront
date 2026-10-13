"""Fixtures for the profile work package tests.

Every document fixture is a fresh deep copy, so tests may mutate freely. `site_snapshots` builds an L3
snapshot directory from the demo sites (`sites/<business>/*.html`, named by the sha256 of their bytes,
exactly what onboarding records as `content_sha256`).
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from support import EXAMPLE_PROFILE, PROFILES_DIR, SCHEMA_PATH

from shopfront_server.config import REPO_ROOT
from shopfront_server.errors import ProfileInvalid
from shopfront_server.profile.loader import LoadedProfile, load_profile
from shopfront_server.profile.snapshots import DirectorySnapshotProvider, SnapshotProvider

GOLD_IDS = ("thimbleberry-bakehouse", "quillfern-salon", "kettlewick-home")
SITES_DIR = REPO_ROOT / "sites"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def _example_doc() -> dict[str, Any]:
    return _read(EXAMPLE_PROFILE)


@pytest.fixture(scope="session")
def _gold_docs() -> dict[str, dict[str, Any]]:
    return {path.stem: _read(path) for path in sorted(PROFILES_DIR.glob("*.json"))}


@pytest.fixture
def example_raw(_example_doc: dict[str, Any]) -> dict[str, Any]:
    """A mutable copy of schema/example-profile.json (status live, illustrative hashes)."""
    return copy.deepcopy(_example_doc)


@pytest.fixture
def gold_raw(_gold_docs: dict[str, dict[str, Any]]) -> Callable[[str], dict[str, Any]]:
    """`gold_raw("thimbleberry-bakehouse")` -> a mutable copy of profiles/thimbleberry-bakehouse.json (skips if absent)."""

    def get(name: str) -> dict[str, Any]:
        if name not in _gold_docs:
            pytest.skip(f"gold profile {name} not present yet")
        return copy.deepcopy(_gold_docs[name])

    return get


@pytest.fixture(scope="session")
def site_snapshots(tmp_path_factory: pytest.TempPathFactory) -> SnapshotProvider:
    """A DirectorySnapshotProvider over every demo-site page, keyed by sha256 of the raw bytes."""
    directory = tmp_path_factory.mktemp("snapshots")
    for page in SITES_DIR.glob("*/*.html"):
        digest = hashlib.sha256(page.read_bytes()).hexdigest()
        shutil.copyfile(page, directory / f"{digest}.html")
    return DirectorySnapshotProvider(directory)


def load(doc: dict[str, Any], *, strict: bool = False, snapshots: SnapshotProvider | None = None) -> LoadedProfile:
    """The real loader with the repo schema."""
    return load_profile(doc, schema_path=SCHEMA_PATH, strict=strict, snapshots=snapshots)


def error_invariants(
    doc: dict[str, Any], *, strict: bool = False, snapshots: SnapshotProvider | None = None
) -> set[str]:
    """The invariant ids of every error the loader reports (empty set if the document loads)."""
    try:
        load(doc, strict=strict, snapshots=snapshots)
    except ProfileInvalid as exc:
        return {v.invariant for v in exc.report.errors}  # type: ignore[attr-defined]
    return set()


def error_messages(
    doc: dict[str, Any], *, strict: bool = False, snapshots: SnapshotProvider | None = None
) -> list[str]:
    try:
        load(doc, strict=strict, snapshots=snapshots)
    except ProfileInvalid as exc:
        return [f"{v.invariant} {v.where} {v.message}" for v in exc.report.errors]  # type: ignore[attr-defined]
    return []


@pytest.fixture
def load_doc() -> Callable[..., LoadedProfile]:
    """`load_doc(doc, strict=False, snapshots=None)`: the real loader."""
    return load


@pytest.fixture
def errors_of() -> Callable[..., set[str]]:
    """`errors_of(doc, strict=False, snapshots=None)`: invariant ids of the loader's errors."""
    return error_invariants


@pytest.fixture
def messages_of() -> Callable[..., list[str]]:
    """`messages_of(doc, ...)`: '<invariant> <where> <message>' for each loader error."""
    return error_messages
