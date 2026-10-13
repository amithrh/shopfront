"""Load a raw profile document: schema validation, then invariants L1-L11, then a `Profile` view.

    profile = load_profile(raw, schema_path=settings.profile_schema_path, snapshots=provider, strict=False)

Raises `errors.ProfileInvalid(report=LoadReport)` if the schema or any error-severity invariant fails.
Warnings (e.g. L3 skipped for lack of snapshots) are kept on `LoadedProfile.report`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shopfront_server import errors
from shopfront_server.canonical import profile_sha256
from shopfront_server.profile.invariants import InvariantContext, LoadReport, Violation, run_all
from shopfront_server.profile.model import Profile, thaw
from shopfront_server.profile.schema import validate_schema
from shopfront_server.profile.snapshots import SnapshotProvider


@dataclass(frozen=True)
class LoadedProfile:
    profile: Profile
    report: LoadReport


def _invalid(report: LoadReport) -> errors.ProfileInvalid:
    name = report.profile_id or "<unknown>"
    return errors.ProfileInvalid(
        f"Profile {name!r} is invalid ({len(report.errors)} error(s)): {report.summary()}", report=report
    )


def load_profile(
    raw: Mapping[str, Any],
    *,
    schema_path: Path,
    snapshots: SnapshotProvider | None = None,
    strict: bool = True,
) -> LoadedProfile:
    """Validate and wrap one profile. The input is deep-copied; the caller's dict is never retained."""
    if not isinstance(raw, Mapping):
        report = LoadReport(
            violations=[Violation(invariant="schema", severity="error", where="", message="not a JSON object")]
        )
        raise _invalid(report)
    doc: dict[str, Any] = thaw(raw)
    report = LoadReport(
        profile_id=doc["profile_id"] if isinstance(doc.get("profile_id"), str) else None,
        revision=doc["revision"] if isinstance(doc.get("revision"), int) else None,
    )
    report.violations = validate_schema(doc, schema_path=schema_path)
    if report.violations:
        raise _invalid(report)
    try:
        sha = profile_sha256(doc)
    except Exception as exc:  # rfc8785 rejects NaN, infinities and out-of-range integers
        report.violations = [
            Violation(invariant="schema", severity="error", where="", message=f"not canonicalisable: {exc}")
        ]
        raise _invalid(report) from exc
    report.sha256 = sha
    report.violations = run_all(InvariantContext(raw=doc, sha256=sha, snapshots=snapshots, strict=strict))
    if report.errors:
        raise _invalid(report)
    return LoadedProfile(profile=Profile(doc, sha256=sha), report=report)


def load_profile_file(
    path: Path, *, schema_path: Path, snapshots: SnapshotProvider | None = None, strict: bool = True
) -> LoadedProfile:
    """`load_profile` for a JSON file (UTF-8). Unparseable JSON raises `errors.ProfileInvalid` too."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        report = LoadReport(
            violations=[
                Violation(invariant="schema", severity="error", where="", message=f"{path.name}: not JSON ({exc})")
            ]
        )
        raise _invalid(report) from exc
    return load_profile(raw, schema_path=schema_path, snapshots=snapshots, strict=strict)
