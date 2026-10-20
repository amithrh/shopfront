"""Certify the demo businesses with the real certifier and publish the live profiles the server serves.

For each profile in `profiles/` (the three demo businesses):
  1. snapshot its website from `sites/<profile id>/` exactly as fetched (pages are matched to the
     profile's sources by sha256, so every evidence quote is checked against the real page bytes);
  2. run the deterministic certifier (`askonce_onboarding.certify`): the real MCP server in process,
     every check decided in code on the tool-call trace, no model;
  3. only a CERTIFIED verdict goes live: the certified profile, with `certification.status` set to
     `live` (the owner's "Go live"; the run's sha256 still binds, it excludes the certification block),
     is written to `profiles/live/<profile id>.json`, and the verdict to
     `profiles/certification/<profile id>.json`.

Anything short of CERTIFIED stops with the failing checks and publishes nothing for that business.

    cd onboarding && uv run python ../scripts/certify_demo.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

from askonce_onboarding.certify import certify

REPO = Path(__file__).resolve().parents[1]
GOLD = REPO / "profiles"
SITES = REPO / "sites"
LIVE = GOLD / "live"
VERDICTS = GOLD / "certification"


def snapshot_site(profile: dict, site: Path, snapshots: Path) -> int:
    """Copy the site's pages the profile cites into `snapshots/<sha256>.html`; returns how many."""
    wanted = {s["content_sha256"] for s in profile.get("sources", {}).values() if s.get("content_sha256")}
    snapshots.mkdir(parents=True, exist_ok=True)
    found = 0
    for page in sorted(site.rglob("*.html")):
        digest = hashlib.sha256(page.read_bytes()).hexdigest()
        if digest in wanted:
            shutil.copyfile(page, snapshots / f"{digest}.html")
            found += 1
    return found


def main() -> int:
    LIVE.mkdir(exist_ok=True)
    VERDICTS.mkdir(exist_ok=True)
    failed = 0
    for path in sorted(GOLD.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        pid = doc["profile_id"]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "profile.json").write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            pages = snapshot_site(doc, SITES / pid, work / "snapshots")
            verdict = certify(work / "profile.json")
            summary = verdict.summary
            print(f"{pid}: {verdict.verdict} ({summary['passed']}/{summary['cases_total']} passed, "
                  f"{summary['skipped']} skipped, {pages} page snapshots)")
            if verdict.verdict != "CERTIFIED":
                failed += 1
                for case in verdict.cases:
                    if case["status"] != "passed":
                        print(f"   {case['status']}: {case['id']}: {case.get('detail')}")
                continue
            certified = json.loads((work / "profile.json").read_text(encoding="utf-8"))
            certified["certification"]["status"] = "live"
            (LIVE / f"{pid}.json").write_text(json.dumps(certified, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            shutil.copyfile(work / "certification-verdict.json", VERDICTS / f"{pid}.json")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
