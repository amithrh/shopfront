"""Strands interop: run `strands_tasks_interop.py` with Strands against a live AskOnce server.

Strands 1.57.1 ships its own SEP-2663 client (on mcp 2.1.1), written independently of `mcp_tasks`. The
script places a big custom-cake order that becomes an owner task, the owner approves over the signed REST
API, and Strands polls the task to completion (both `call_tool_sync` and `submit_tool_sync`/`get_task_sync`).

The client runs in its own pinned, isolated environment (`uv run --isolated --with strands-agents==1.57.1
--with mcp==2.1.1`, cached by uv after the first run), in a subprocess so the two `mcp` versions never share
an interpreter. Skips only when `uv` is not installed (or cannot build that environment, e.g. offline on a
cold cache).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from e2e_support import ADMIN_TOKEN, BAKERY, Shop, injection_canaries, loaded, running_shop

from askonce_server.config import REPO_ROOT

SCRIPT = Path(__file__).with_name("strands_tasks_interop.py")
STRANDS_PINS = ("strands-agents==1.57.1", "mcp==2.1.1")


def _strands_python() -> list[str] | None:
    """The command that starts a Python with Strands' own SEP-2663 client, or None if unavailable."""
    uv = shutil.which("uv")
    if uv is None:
        return None
    command = [uv, "run", "--isolated", "--no-project", *(a for pin in STRANDS_PINS for a in ("--with", pin)), "python"]
    probe = subprocess.run(  # noqa: S603 - fixed arguments
        [*command, "-I", "-c", "import strands.tools.mcp as m; m.TasksConfig"],
        capture_output=True,
        timeout=300,
        check=False,
        cwd=REPO_ROOT,
    )
    return command if probe.returncode == 0 else None


STRANDS_PYTHON = _strands_python()


pytestmark = pytest.mark.skipif(STRANDS_PYTHON is None, reason="uv is not installed (or offline with a cold uv cache)")


@pytest.fixture
def shop(
    tmp_path: Path, raw_profiles: dict[str, dict[str, Any]], live_raw_profiles: dict[str, dict[str, Any]]
) -> Iterator[Shop]:
    if BAKERY not in live_raw_profiles:
        pytest.skip("gold bakery profile not present")
    profiles = [loaded(doc) for doc in live_raw_profiles.values()]
    with running_shop(tmp_path, profiles, injection_canaries(raw_profiles)) as running:
        yield running


def test_strands_client_completes_an_owner_approved_task(shop: Shop) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [*(STRANDS_PYTHON or ()), str(SCRIPT), shop.url, ADMIN_TOKEN],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr[-4000:]}"
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    print(json.dumps(summary))  # visible with -rP
    assert summary["ok"] is True
    assert summary["tools"] == 16
    assert summary["call_tool_sync"]["status"] == "approved"
    submitted = summary["submit_tool_sync"]
    assert submitted["handle"] == "MCPCreateTaskResult" and submitted["handle_status"] == "working"
    assert submitted["final_status"] == "completed" and submitted["result_status"] == "approved"
    assert submitted["reference"] != summary["call_tool_sync"]["reference"]
    assert summary["confirmations"] == 2  # one MRTR confirmation per order, answered by Strands' callback
    assert len(shop.notifier.messages_for(BAKERY)) == 2
