"""Publish a CERTIFIED AskOnce profile: `go_live.py out/<id> --to profiles/live`.

Refuses unless the folder's certification-verdict.json says CERTIFIED for this exact profile revision.
Writes `<to>/<profile id>.json` with `certification.status` set to `live` (the owner's "Go live").
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="go_live", description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="onboarding output folder, e.g. out/thimbleberry")
    parser.add_argument("--to", type=Path, required=True, help="folder of live profiles the server loads")
    args = parser.parse_args(argv)

    profile = json.loads((args.folder / "profile.json").read_text(encoding="utf-8"))
    verdict = json.loads((args.folder / "certification-verdict.json").read_text(encoding="utf-8"))
    pid = profile["profile_id"]

    problems = []
    if verdict.get("verdict") != "CERTIFIED":
        problems.append(f"verdict is {verdict.get('verdict')}, not CERTIFIED")
    if verdict.get("profile_id") != pid:
        problems.append(f"verdict is for {verdict.get('profile_id')}, not {pid}")
    if verdict.get("profile_revision") != profile.get("revision"):
        problems.append(f"verdict is for revision {verdict.get('profile_revision')}, profile is {profile.get('revision')}: re-certify")
    if profile.get("certification", {}).get("status") != "certified":
        problems.append(f"profile status is {profile.get('certification', {}).get('status')}, not certified")
    if problems:
        print(f"{pid}: not published: " + "; ".join(problems), file=sys.stderr)
        return 1

    profile["certification"]["status"] = "live"
    args.to.mkdir(parents=True, exist_ok=True)
    target = args.to / f"{pid}.json"
    target.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    summary = verdict.get("summary", {})
    print(f"{pid}: live -> {target} ({summary.get('passed')}/{summary.get('cases_total')} checks passed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
