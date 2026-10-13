"""Every test gets its own spend ledger file, so no test touches the real `.local/bedrock-spend.json`."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bedrock-spend.json"
    monkeypatch.setenv("ASKONCE_BEDROCK_LEDGER", str(path))
    monkeypatch.delenv("ASKONCE_BEDROCK_BUDGET_USD", raising=False)
    return path
