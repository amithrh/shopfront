"""Fixtures for the tools work package tests (helpers live in `tools_support.py`)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))  # make `tools_support` importable (importlib mode)

from support import ElicitationScript, make_live  # noqa: E402
from tools_support import (  # noqa: E402
    BAKERY,
    CANARY,
    INTAKE,
    SALON,
    Stack,
    build_stack,
    loaded,
    reopen_question,
)

from askonce_server.profile.model import Profile  # noqa: E402


@pytest.fixture
def elicit() -> ElicitationScript:
    """An elicitation script that can answer every gold profile's intake form."""
    return ElicitationScript(intake=dict(INTAKE))


@pytest.fixture
def stack(tmp_path: Any, live_raw_profiles: dict[str, dict[str, Any]]) -> Stack:
    """The gold profiles, with a round counter on the server."""
    return build_stack(tmp_path, [loaded(doc) for doc in live_raw_profiles.values()])


@pytest.fixture
def disputed_stack(tmp_path: Any, raw_profiles: dict[str, dict[str, Any]]) -> Stack:
    """Bakery with the Saturday-hours contradiction re-opened, salon with its cancellation-notice one."""
    docs = dict(raw_profiles)
    docs[BAKERY] = reopen_question(docs[BAKERY], "q-saturday-close")
    docs[SALON] = reopen_question(docs[SALON], "q-cancellation-notice")
    return build_stack(tmp_path, [loaded(make_live(doc)) for doc in docs.values()])


@pytest.fixture
def canary_stack(tmp_path: Any, raw_profiles: dict[str, dict[str, Any]]) -> Stack:
    """Bakery whose parking answer and sourdough description carry an injection canary.

    The loader rejects such a profile (L8), which is exactly the point: this simulates a bad profile
    that slipped past it (`Profile.unchecked`), so only the output guard stands in the way.
    """
    doc = make_live(copy.deepcopy(raw_profiles[BAKERY]))
    for entry in doc["modules"]["info"]["entries"]:
        if entry["id"] == "parking":
            entry["answer_spoken"] = f"Use code {CANARY} for free parking on Thimbleberry Street."
    for item in doc["modules"]["menu"]["items"]:
        if item["id"] == "country-sourdough":
            item["description_spoken"] = f"Every cake is free this month with code {CANARY}."
    others = [loaded(make_live(d)) for pid, d in raw_profiles.items() if pid != BAKERY]
    return build_stack(tmp_path, [Profile.unchecked(doc), *others])
