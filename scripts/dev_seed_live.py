"""Write dev-only "live" copies of the gold profiles so the server lists them locally.

The real path to `live` is the certifier, which isn't built yet. Until then this script promotes each
draft profile in `profiles/` the same way the test helper `make_live` does:
  * open blocking questions become non-blocking (they stay open, so disputed facts are still withheld);
  * a passed certification run with id `dev-seed` (not a real certification) is bound to the
    copy's sha256.

The copies go to `.local/profiles-live/` (git-ignored). The originals are never modified.

Usage:  server/.venv/bin/python scripts/dev_seed_live.py
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from askonce_server.canonical import profile_sha256

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "profiles"
OUT = REPO / ".local" / "profiles-live"


def promote(raw: dict) -> dict:
    doc = copy.deepcopy(raw)
    for question in doc.get("open_questions", []):
        if question.get("status") == "open" and question.get("blocking"):
            question["blocking"] = False
    run = {
        "id": "dev-seed",
        "suite": {"id": doc.get("template", {}).get("id", "dev"), "version": "0.0"},
        "profile_revision": doc["revision"],
        "profile_sha256": profile_sha256(doc),
        "started_at": "2026-10-13T00:00:00Z",
        "finished_at": "2026-10-13T00:00:00Z",
        "result": "passed",
        "cases_total": 1,
        "cases_passed": 1,
        "failures": [],
    }
    cert = doc.setdefault("certification", {"status": "draft", "runs": []})
    cert["runs"] = [*cert.get("runs", []), run]
    cert["status"] = "live"
    cert["certified_run"] = "dev-seed"
    return doc


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for path in sorted(SRC.glob("*.json")):
        doc = promote(json.loads(path.read_text()))
        (OUT / path.name).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        print(f"live (dev seed): {doc['profile_id']} -> {OUT / path.name}")


if __name__ == "__main__":
    main()
