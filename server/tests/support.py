"""Test helpers shared by every test module (importable as `support`; tests/ is on pythonpath)."""

from __future__ import annotations

import contextlib
import copy
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

import mcp_types as types
import pytest

from shopfront_server.canonical import profile_sha256
from shopfront_server.config import REPO_ROOT

T = TypeVar("T")

FIXED_NOW = datetime(2026, 10, 14, 16, 0, tzinfo=UTC)
"""Wednesday 14 Oct 2026, 16:00 UTC (morning in America/Chicago, Denver and Los_Angeles)."""

PROFILES_DIR = REPO_ROOT / "profiles"
EXAMPLE_PROFILE = REPO_ROOT / "schema" / "example-profile.json"
SCHEMA_PATH = REPO_ROOT / "schema" / "business-profile.schema.json"

TEST_KEY = "test-only-key-0123456789abcdef0123456789"


def pending(work_package: str, build: Callable[[], T]) -> T:
    """Run `build`; skip the test if it hits a skeleton (NotImplementedError)."""
    try:
        return build()
    except NotImplementedError:
        pytest.skip(f"pending work package: {work_package}")


def load_raw_profiles() -> dict[str, dict[str, Any]]:
    """Gold profiles keyed by profile_id; the example profile when /profiles is empty."""
    paths = sorted(PROFILES_DIR.glob("*.json")) or [EXAMPLE_PROFILE]
    out: dict[str, dict[str, Any]] = {}
    for path in paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
        out[doc["profile_id"]] = doc
    return out


def make_live(raw: dict[str, Any], *, run_id: str = "cert-test") -> dict[str, Any]:
    """A copy of `raw` promoted to `live`, satisfying schema rules R2 and the L7 binding.

    Open blocking questions become non-blocking (they stay open, so disputed facts are still withheld,
    which is what the statable-rule tests need). A passed run bound to the copy's sha256 is appended.
    """
    doc = copy.deepcopy(raw)
    for question in doc.get("open_questions", []):
        if question.get("status") == "open" and question.get("blocking"):
            question["blocking"] = False
    sha = profile_sha256(doc)
    run = {
        "id": run_id,
        "suite": {"id": doc.get("template", {}).get("id", "test"), "version": "1.0"},
        "profile_revision": doc["revision"],
        "profile_sha256": sha,
        "started_at": "2026-10-12T00:00:00Z",
        "finished_at": "2026-10-12T00:01:00Z",
        "result": "passed",
        "cases_total": 1,
        "cases_passed": 1,
        "failures": [],
    }
    certification = doc.setdefault("certification", {"status": "draft", "runs": []})
    certification["runs"] = [*certification.get("runs", []), run]
    certification["status"] = "live"
    certification["certified_run"] = run_id
    return doc


@dataclass
class ElicitationScript:
    """Scripted elicitation answers for in-process clients.

    Each incoming elicitation is recorded in `seen` (message + requested property names) and its full
    `requestedSchema` in `schemas` (same order). The answer is taken from `answers` (FIFO) if any are
    queued, else from `default`:
      * a dict -> accept with that content (filtered to the requested properties);
      * "decline" / "cancel" -> that action.
    The default accepts confirmations (`confirm: True`) and fills intake forms from `intake`.
    """

    intake: dict[str, Any] = field(default_factory=dict)
    answers: list[dict[str, Any] | str] = field(default_factory=list)
    seen: list[tuple[str, list[str]]] = field(default_factory=list)
    schemas: list[dict[str, Any]] = field(default_factory=list)

    def default(self, properties: list[str]) -> dict[str, Any]:
        if properties == ["confirm"]:
            return {"confirm": True}
        return {k: v for k, v in self.intake.items() if k in properties}

    async def __call__(self, context: Any, params: types.ElicitRequestParams) -> types.ElicitResult:
        schema = getattr(params, "requested_schema", None) or {}
        properties = sorted(schema.get("properties", {}))
        self.seen.append((params.message, properties))
        self.schemas.append(dict(schema))
        answer: dict[str, Any] | str = self.answers.pop(0) if self.answers else self.default(properties)
        if isinstance(answer, str):
            return types.ElicitResult(action=answer)  # type: ignore[arg-type]
        return types.ElicitResult(action="accept", content={k: v for k, v in answer.items() if k in properties})


def bind_localhost() -> socket.socket:
    """A listening-ready TCP socket bound to a free 127.0.0.1 port (pass it to `serve`)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    return sock


@contextlib.contextmanager
def serve(app: Any, sock: socket.socket | None = None) -> Iterator[str]:
    """Run an ASGI `app` under uvicorn (lifespan on) in a daemon thread; yields its base URL.

    `sock` lets the caller learn the port before building the app (e.g. for `public_base_url`).
    """
    import uvicorn

    sock = sock or bind_localhost()
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on", timeout_graceful_shutdown=2))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def apps_advertisement() -> Any:
    from mcp.client import advertise

    return advertise("io.modelcontextprotocol/ui", {"mimeTypes": ["text/html;profile=mcp-app"]})




def add_dispute(raw: dict[str, Any], *targets: str, question_id: str = "q-review-dispute") -> dict[str, Any]:
    """A copy of `raw` with an open, non-blocking contradiction on `targets` (allowed on a live profile
    by R2), so every overlapping fact is disputed (L6)."""
    doc = copy.deepcopy(raw)
    url = next(iter(doc["sources"].values()))["url"]
    doc.setdefault("open_questions", []).append(
        {
            "id": question_id,
            "kind": "contradiction",
            "targets": list(targets),
            "prompt": "Which of these is right?",
            "why": "Two of your pages disagree, so the assistant won't say either until you answer.",
            "candidates": [
                {"value": "a", "label": "The first reading", "evidence": {"url": url, "quote": "first"}},
                {"value": "b", "label": "The second reading", "evidence": {"url": url, "quote": "second"}},
            ],
            "blocking": False,
            "status": "open",
        }
    )
    return doc
