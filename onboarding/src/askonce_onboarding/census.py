"""Readiness census: onboard and certify real small-business websites, publish aggregate numbers only.

    uv run askonce-census SITES.txt --max-usd 3

Read-only and polite: robots.txt is honoured, at most 8 pages per site, one request per second per
site. Per-site profiles, snapshots and model replies stay under `.local/census/` (git-ignored): the
published result is `onboarding/census/census-summary.json`, with counts and rates across sites and no
business named. Every Bedrock call goes through the capped spend ledger, and the run stops before
`--max-usd` of new spend.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import traceback
from pathlib import Path
from typing import Any

from askonce_onboarding.assemble import _names_item
from askonce_onboarding.certify import Certifier
from askonce_onboarding.llm import CachedModel, ModelUnavailable
from askonce_onboarding.pipeline import onboard
import hashlib

from askonce_onboarding.site import Fetched, FetchError, PoliteFetcher

REPO = Path(__file__).resolve().parents[3]
WORK = REPO / ".local" / "census"
SUMMARY = REPO / "onboarding" / "census" / "census-summary.json"
MAX_PAGES = 8


class PageCache:
    """Fetch each URL once and keep the bytes, so re-running the census never re-hits a site and
    reads exactly the same pages (live pages change between requests; recorded replies then miss)."""

    def __init__(self, inner: object, directory: Path, offline: bool = False) -> None:
        self.inner, self.dir, self.offline = inner, directory, offline

    def fetch(self, url: str) -> Fetched:
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        body, meta = self.dir / f"{key}.bin", self.dir / f"{key}.json"
        if body.is_file() and meta.is_file():
            info = json.loads(meta.read_text())
            if info.get("error"):
                raise FetchError(info["error"])
            return Fetched(info["url"], info["content_type"], body.read_bytes())
        if self.offline:
            raise FetchError(f"{url}: not in the page cache")
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            fetched = self.inner.fetch(url)  # type: ignore[attr-defined]
        except FetchError as exc:
            meta.write_text(json.dumps({"error": str(exc)}))
            body.write_bytes(b"")
            raise
        body.write_bytes(fetched.body)
        meta.write_text(json.dumps({"url": fetched.url, "content_type": fetched.content_type}))
        return fetched


def read_sites(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line if line.startswith("http") else f"https://{line}")
    return out


def site_metrics(outcome: Any, verdict: Any | None) -> dict[str, Any]:
    questions = outcome.questions
    report = outcome.report
    modules = outcome.profile.get("modules", {})
    items = sum(len(m.get("items", [])) for m in modules.values() if m.get("type") == "catalog")
    entries = sum(len(m.get("entries", [])) for m in modules.values() if m.get("type") == "knowledge")
    priced = [i for m in modules.values() if m.get("type") == "catalog" for i in m.get("items", [])
              if (i.get("price") or {}).get("amount_cents")]
    named = [i for i in priced if _names_item(i["provenance"]["evidence"][0]["quote"], i["name"])]
    return {
        "valid": outcome.valid,
        "priced_items": len(priced),
        "priced_items_cited_by_name": len(named),
        "template": outcome.profile.get("template", {}).get("id"),
        "pages": len(report["pages"]),
        "items": items,
        "policies": entries,
        "has_hours": any(loc.get("hours") for loc in outcome.profile["business"]["locations"]),
        "contradictions": sum(q["kind"] == "contradiction" for q in questions),
        "missing": sum(q["kind"] == "missing" for q in questions),
        "confirm": sum(q["kind"] == "confirm" for q in questions),
        "injections": len(report["suspected_injections"]),
        "dropped_facts": len(report["dropped_facts"]),
        "extraction_errors": len(report["extraction_errors"]),
        "verdict": verdict.verdict if verdict else None,
        "cases_failed": verdict.summary["failed"] if verdict else None,
        "failed_checks": sorted({c["check"] for c in verdict.cases if c["status"] == "failed"}) if verdict else [],
    }


def aggregate(rows: list[dict[str, Any]], skipped: list[dict[str, str]]) -> dict[str, Any]:
    ok = [r for r in rows if r.get("valid") is not None]
    n = len(ok)

    def share(pred: Any) -> float:
        return round(sum(1 for r in ok if pred(r)) / n, 3) if n else 0.0

    def med(key: str) -> float:
        return statistics.median([r[key] for r in ok]) if n else 0

    verdicts: dict[str, int] = {}
    for r in ok:
        verdicts[str(r["verdict"])] = verdicts.get(str(r["verdict"]), 0) + 1
    failed_checks: dict[str, int] = {}
    for r in ok:
        for check in r["failed_checks"]:
            failed_checks[check] = failed_checks.get(check, 0) + 1
    templates: dict[str, int] = {}
    for r in ok:
        templates[str(r["template"])] = templates.get(str(r["template"]), 0) + 1
    return {
        "sites_attempted": n + len(skipped),
        "sites_onboarded": n,
        "sites_skipped": {reason: sum(1 for s in skipped if s["reason"] == reason) for reason in sorted({s["reason"] for s in skipped})},
        "verticals": templates,
        "profiles_valid": share(lambda r: r["valid"]),
        "verdicts": verdicts,
        "share_with_contradiction": share(lambda r: r["contradictions"] > 0),
        "share_with_injection_text": share(lambda r: r["injections"] > 0),
        "share_missing_hours": share(lambda r: not r["has_hours"]),
        "share_needing_owner_answers": share(lambda r: r["contradictions"] + r["missing"] + r["confirm"] > 0),
        "median_pages": med("pages"),
        "median_items": med("items"),
        "median_policies": med("policies"),
        "median_owner_questions": statistics.median([r["contradictions"] + r["missing"] + r["confirm"] for r in ok]) if n else 0,
        "median_unquoted_facts_dropped": med("dropped_facts"),
        "unquoted_facts_dropped_total": sum(r["dropped_facts"] for r in ok),
        "priced_items_total": sum(r["priced_items"] for r in ok),
        "share_prices_cited_by_name_and_amount": round(sum(r["priced_items_cited_by_name"] for r in ok) / max(1, sum(r["priced_items"] for r in ok)), 3),
        "certifier_failed_checks": failed_checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="askonce-census", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sites", type=Path, help="text file, one site URL per line (# comments)")
    parser.add_argument("--max-usd", type=float, default=3.0, help="stop before this much new Bedrock spend")
    parser.add_argument("--offline", action="store_true", help="cached replies only (re-aggregate without spending)")
    args = parser.parse_args(argv)

    model = CachedModel("replay" if args.offline else "live", cache_dir=WORK / "llm")
    fetcher = PageCache(PoliteFetcher(), WORK / "pages", offline=args.offline)
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    per_site_path = WORK / "per-site.json"  # local only: names businesses
    for i, url in enumerate(read_sites(args.sites), 1):
        if model.usage.cost_usd >= args.max_usd:
            print(f"budget reached (${model.usage.cost_usd:.2f}); stopping", flush=True)
            break
        print(f"[{i}] {url}", flush=True)
        try:
            outcome = onboard(url, model=model, fetcher=fetcher, out_root=WORK / "out", max_pages=MAX_PAGES, delay_s=1.0, parallel=2)
        except ModelUnavailable as exc:
            print(f"    model unavailable: {exc}", flush=True)
            skipped.append({"url": url, "reason": "model_unavailable"})
            if args.offline:
                continue
            break
        except FetchError as exc:
            print(f"    fetch: {exc}", flush=True)
            skipped.append({"url": url, "reason": "robots_or_fetch"})
            continue
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the census
            print(f"    error: {type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc(limit=3)
            skipped.append({"url": url, "reason": type(exc).__name__})
            continue
        verdict = None
        if outcome.valid:
            try:
                snaps = outcome.out_dir / "snapshots"
                verdict, _ = Certifier(outcome.profile, snapshots=snaps).run()
            except Exception as exc:  # noqa: BLE001
                print(f"    certifier error: {type(exc).__name__}: {exc}", flush=True)
        row = {"url": url, **site_metrics(outcome, verdict)}
        rows.append(row)
        print(f"    {row['pages']} pages, {row['items']} items, {row['policies']} policies, valid={row['valid']}, "
              f"verdict={row['verdict']}, questions c/m/s={row['contradictions']}/{row['missing']}/{row['confirm']}, "
              f"injections={row['injections']}  (spend ${model.usage.cost_usd:.3f})", flush=True)
        WORK.mkdir(parents=True, exist_ok=True)
        per_site_path.write_text(json.dumps({"rows": rows, "skipped": skipped}, indent=1), encoding="utf-8")
    summary = aggregate(rows, skipped)
    summary["model_spend_usd_this_run"] = round(model.usage.cost_usd, 4)
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
