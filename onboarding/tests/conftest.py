"""Shared test helpers: a fake model that answers from hand-written page extractions (no Bedrock)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).parent


class FakeModel:
    """Replies to page prompts from `pages[url]` and to the review prompt with `review`."""

    def __init__(self, pages: dict[str, Any], review: dict[str, Any] | None = None) -> None:
        self.pages = pages
        self.review = review or {"contradictions": []}
        self.calls: list[str] = []

    def complete(self, *, kind: str, system: str, user: str, max_tokens: int) -> str:
        self.calls.append(kind)
        if kind.startswith("review"):
            return json.dumps(self.review)
        if kind.startswith("variants"):
            return "{}"
        url = re.search(r"PAGE URL: (\S+)", user).group(1)
        return "```json\n" + json.dumps(self.pages.get(url, {})) + "\n```"


@pytest.fixture
def bakery_pages() -> dict[str, Any]:
    return json.loads((HERE / "fixtures" / "bakery_extraction.json").read_text(encoding="utf-8"))
