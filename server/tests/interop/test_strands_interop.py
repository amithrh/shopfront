"""Strands interop: run `strands_tasks_interop.py` under the onboarding venv against a live Shopfront server.

Strands 1.57.1 ships its own SEP-2663 client (on mcp 2.1.1), written independently of `mcp_tasks`. The
script places a big custom-cake order that becomes an owner task, the owner approves over the signed REST
API, and Strands polls the task to completion (both `call_tool_sync` and `submit_tool_sync`/`get_task_sync`).

Skips when the onboarding venv (or strands in it) is missing. Runs in a subprocess so the two `mcp`
versions never share an interpreter.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from e2e_support import ADMIN_TOKEN, BAKERY, Shop, injection_canaries, loaded, running_shop

from shopfront_server.config import REPO_ROOT

ONBOARDING_PYTHON = REPO_ROOT / "onboarding" / ".venv" / "bin" / "python"
SCRIPT = Path(__file__).with_name("strands_tasks_interop.py")


def _strands_available() -> bool:
    if not ONBOARDING_PYTHON.exists():
        return False
    probe = subprocess.run(  # noqa: S603 - fixed interpreter path, fixed arguments
        [str(ONBOARDING_PYTHON), "-I", "-c", "import strands.tools.mcp as m; m.TasksConfig"],
        capture_output=True,
        timeout=60,
        check=False,
    )
    return probe.returncode == 0


pytestmark = pytest.mark.skipif(not _strands_available(), reason="onboarding venv with strands is not available")


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
        [str(ONBOARDING_PYTHON), str(SCRIPT), shop.url, ADMIN_TOKEN],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr[-4000:]}"
    summary = json.loads(completed.stdout.strip().splitlines()[-1])
    print(json.dumps(summary))  # visible with -rP
    assert summary["ok"] is True
    assert summary["tools"] == 14
    assert summary["call_tool_sync"]["status"] == "approved"
    submitted = summary["submit_tool_sync"]
    assert submitted["handle"] == "MCPCreateTaskResult" and submitted["handle_status"] == "working"
    assert submitted["final_status"] == "completed" and submitted["result_status"] == "approved"
    assert submitted["reference"] != summary["call_tool_sync"]["reference"]
    assert summary["confirmations"] == 2  # one MRTR confirmation per order, answered by Strands' callback
    assert len(shop.notifier.messages_for(BAKERY)) == 2
