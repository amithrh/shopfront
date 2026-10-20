"""URL -> profile.json + questions.json: crawl, quarantine injections, extract per page, assemble, validate.

The profile is validated with the MCP server's own loader (JSON Schema + invariants L1-L11, strict), with
the page snapshots written next to it so L3 checks every evidence quote against the real page bytes.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from askonce_server import errors
from askonce_server.profile import DirectorySnapshotProvider, load_profile

from askonce_onboarding.assemble import Assembler
from askonce_onboarding.extract import PageFacts, extract_page, review_contradictions
from askonce_onboarding.injection import find_canaries, model_text
from askonce_onboarding.llm import Model, ModelUnavailable
from askonce_onboarding.site import Fetcher, crawl
from askonce_onboarding.variants import generate as question_variants

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schema" / "business-profile.schema.json"

Progress = Callable[[str], None]


@dataclass
class Outcome:
    profile_id: str
    out_dir: Path
    valid: bool
    profile: dict[str, Any]
    questions: list[dict[str, Any]]
    report: dict[str, Any] = field(default_factory=dict)


def _now() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def onboard(
    url: str,
    *,
    model: Model,
    fetcher: Fetcher,
    out_root: Path,
    clock: Callable[[], str] = _now,
    progress: Progress = lambda _msg: None,
    parallel: int = 3,
    max_pages: int | None = None,
    delay_s: float = 0.0,
) -> Outcome:
    pages = crawl(url, fetcher, clock=clock, delay_s=delay_s, **({"max_pages": max_pages} if max_pages else {}))
    progress(f"fetched {len(pages)} page(s): {', '.join(p.path_key for p in pages)}")
    canaries = {p.url: find_canaries(p.html) for p in pages}
    for page in pages:
        for c in canaries[page.url]:
            progress(f"quarantined suspected prompt injection on {page.path_key} ({c.where})")

    def one(page: Any) -> PageFacts:
        try:
            return extract_page(model, url=page.url, title=page.title, text=model_text(page.html, canaries[page.url]))
        except ModelUnavailable:
            raise
        except Exception as exc:  # a model/router failure on one page must not lose the rest
            return PageFacts(page_url=page.url, error=f"{type(exc).__name__}: {exc}")

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        page_facts = list(pool.map(one, pages))
    for pf in page_facts:
        progress(f"extracted {pf.page_url}" + (f" (error: {pf.error})" if pf.error else ""))

    assembler = Assembler(pages, page_facts, canaries, updated_at=clock())
    result = assembler.build(review=lambda facts: review_contradictions(model, facts),
                             variants=lambda business, entries: question_variants(model, business, entries))
    profile = result.profile
    profile_id = profile["profile_id"]
    out_dir = out_root / profile_id
    snap_dir = out_dir / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    for old in snap_dir.glob("*.html"):
        old.unlink()
    for page in pages:
        (snap_dir / f"{page.sha256}.html").write_bytes(page.raw)

    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    try:
        loaded = load_profile(profile, schema_path=SCHEMA_PATH, snapshots=DirectorySnapshotProvider(snap_dir), strict=True)
        warnings = [v.model_dump() if hasattr(v, "model_dump") else vars(v) for v in loaded.report.violations]
        valid = True
        unreachable = reachable_first(profile, loaded.profile)
        if unreachable:
            progress(f"{len(unreachable)} policy entr{'y' if len(unreachable) == 1 else 'ies'} no question reaches: {', '.join(unreachable[:5])}")
    except errors.ProfileInvalid as exc:
        valid = False
        violations = [v.model_dump() if hasattr(v, "model_dump") else vars(v) for v in exc.report.violations]
    progress(f"profile {'VALID' if valid else 'INVALID'}: {len(result.questions)} question(s) for the owner")

    injections = [
        {"page": page.url, "reason": c.reason, "where": c.where, "quote": c.quote}
        for page in pages
        for c in canaries[page.url]
    ]
    usage = getattr(model, "usage", None)
    report = {
        "url": url,
        "valid": valid,
        "violations": violations,
        "warnings": warnings,
        "pages": [{"url": p.url, "title": p.title, "sha256": p.sha256} for p in pages],
        "extraction_errors": [{"page": pf.page_url, "error": pf.error} for pf in page_facts if pf.error],
        "dropped_facts": result.report["dropped_facts"],
        "suspected_injections": injections,
        "model": vars(usage) if usage is not None else None,
    }
    questions_doc = {
        "profile_id": profile_id,
        "business": profile["business"]["name"],
        "summary": {
            "contradiction": sum(q["kind"] == "contradiction" for q in result.questions),
            "missing": sum(q["kind"] == "missing" for q in result.questions),
            "confirm": sum(q["kind"] == "confirm" for q in result.questions),
            "suspected_injections": len(injections),
        },
        "questions": result.questions,
        "suspected_injections": injections,
    }
    _write(out_dir / "profile.json", profile)
    _write(out_dir / "questions.json", questions_doc)
    _write(out_dir / "report.json", report)
    return Outcome(profile_id, out_dir, valid, profile, result.questions, report)


def reachable_first(profile: dict[str, Any], served: Any) -> list[str]:
    """Each policy entry leads with a question that the server's own matcher answers with that entry (the
    certifier and a host ask an entry's first question). Returns the entries no question of theirs reaches."""
    from askonce_server.tools.knowledge import best_entry

    unreachable = []
    for module_id, module in profile["modules"].items():
        if module.get("type") != "knowledge":
            continue
        for entry in module["entries"]:
            variants = entry["question_variants"]
            hit = next((v for v in variants if (m := best_entry(served, v, None)) and m[0] == module_id and m[1]["id"] == entry["id"]), None)
            if hit is None:
                unreachable.append(entry["id"])
            elif hit != variants[0]:
                entry["question_variants"] = [hit, *[v for v in variants if v != hit]]
    return unreachable


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
