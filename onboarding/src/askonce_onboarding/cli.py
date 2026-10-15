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
