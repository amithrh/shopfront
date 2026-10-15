"""`askonce-onboard`: website URL(s) -> profile.json + questions.json per business.

    uv run askonce-onboard --demo            # the 3 demo sites, from cached model replies ($0)
    uv run askonce-onboard --demo --live     # call Bedrock for any page not cached yet (spend-capped)
    uv run askonce-onboard https://example.com/ --live
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from askonce_onboarding.llm import CachedModel, ModelUnavailable
from askonce_onboarding.pipeline import onboard
from askonce_onboarding.site import DEMO_URLS, DirectoryFetcher, RoutingFetcher

DEFAULT_OUT = Path(__file__).resolve().parents[2] / "out"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="askonce-onboard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*", help="website start URL(s)")
    parser.add_argument("--demo", action="store_true", help="onboard the 3 fictional demo sites in sites/")
    parser.add_argument("--live", action="store_true", help="call Bedrock on cache misses (default: cached replies only)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output folder (default {DEFAULT_OUT})")
    parser.add_argument("--profile", default=None, help="AWS profile (default $AWS_PROFILE or shopfront)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    urls = list(args.urls) + (list(DEMO_URLS) if args.demo else [])
    if not urls:
        parser.error("give a URL or --demo")
    model = CachedModel("live" if args.live else "replay", profile=args.profile)
    fetcher = RoutingFetcher(DirectoryFetcher.demo())
    failures = 0
    for url in urls:
        print(f"== {url}", flush=True)
        try:
            outcome = onboard(url, model=model, fetcher=fetcher, out_root=args.out,
                              progress=(lambda m: None) if args.quiet else (lambda m: print(f"   {m}", flush=True)))
        except ModelUnavailable as exc:
            print(f"   STOPPED: {exc}", flush=True)
            failures += 1
            continue
        kinds = outcome.report.get("dropped_facts", [])
        print(f"   -> {outcome.out_dir}/profile.json ({'valid' if outcome.valid else 'INVALID'}), "
              f"questions.json ({len(outcome.questions)} questions), {len(kinds)} unquoted fact(s) dropped", flush=True)
        if not outcome.valid:
            failures += 1
            for v in outcome.report["violations"][:10]:
                print(f"      {v.get('invariant')} {v.get('where')}: {v.get('message')}", flush=True)
    u = model.usage
    print(f"model: {u.calls} live call(s), {u.cache_hits} cached, ${u.cost_usd:.4f} this run", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())


def certify_main(argv: list[str] | None = None) -> int:
    """`askonce-certify PROFILE.json...` (or --demo): writes certification-verdict.json next to each."""
    from askonce_onboarding.certify import certify

    parser = argparse.ArgumentParser(prog="askonce-certify", description="Deterministic certification of AskOnce profiles.")
    parser.add_argument("profiles", nargs="*", type=Path)
    parser.add_argument("--demo", action="store_true", help="certify the 3 onboarded demo profiles in onboarding/out/")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    paths = list(args.profiles) + (sorted(args.out.glob("*/profile.json")) if args.demo else [])
    if not paths:
        parser.error("give a profile.json or --demo")
    worst = 0
    for path in paths:
        v = certify(path)
        s = v.summary
        print(f"{v.profile_id}: {v.verdict}  ({s['passed']} passed, {s['failed']} failed, {s['skipped']} skipped; "
              f"{s['open_blocking_questions']} open owner question(s); p95 {v.latency['p95_ms']} ms)")
        for rule in v.rules:
            for f in rule["failures"][:5]:
                print(f"   FAIL {f['case']}: {f['detail']}")
        worst = max(worst, {"CERTIFIED": 0, "CONDITIONAL": 0, "NOT_CERTIFIED": 1}[v.verdict])
    return worst


def answer_main(argv: list[str] | None = None) -> int:
    """`askonce-answer PROFILE.json ANSWERS.json`: apply the owner's answers, then re-certify."""
    import datetime as dt
    import json

    from askonce_onboarding.answers import apply_answers
    from askonce_onboarding.certify import certify

    parser = argparse.ArgumentParser(prog="askonce-answer", description="Apply owner answers to a profile and re-certify it.")
    parser.add_argument("profile", type=Path)
    parser.add_argument("answers", type=Path)
    args = parser.parse_args(argv)
    doc = json.loads(args.profile.read_text(encoding="utf-8"))
    answers = {k: v for k, v in json.loads(args.answers.read_text(encoding="utf-8")).items() if not k.startswith("_")}
    now = dt.datetime.now(dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = apply_answers(doc, answers, answered_at=now)
    args.profile.write_text(json.dumps(updated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{updated['profile_id']}: applied {len(answers)} answer(s); revision {updated['revision']}")
    return certify_main([str(args.profile)])
