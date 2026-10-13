"""Print the Bedrock spend ledger: total spent, remaining budget and a per-model breakdown.

    python -m askonce_bedrock.spend          # human-readable
    python -m askonce_bedrock.spend --json

Reads `ASKONCE_BEDROCK_LEDGER` (default `<repo root>/.local/bedrock-spend.json`) and
`ASKONCE_BEDROCK_BUDGET_USD` (default 5.00), like the router does.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any

from askonce_bedrock.ledger import BUDGET_ENV, LedgerError, SpendLedger


def format_snapshot(snap: Mapping[str, Any]) -> str:
    lines = [
        f"ledger:    {snap['ledger']}",
        f"budget:    ${snap['budget_usd']:.2f}   ({BUDGET_ENV})",
        f"spent:     ${snap['spent_usd']:.4f}   over {snap['calls']} call(s)",
    ]
    if snap["reserved_usd"]:
        lines.append(f"in flight: ${snap['reserved_usd']:.4f}")
    lines.append(f"remaining: ${snap['remaining_usd']:.4f}")
    models = snap.get("models") or {}
    if models:
        width = max(len(m) for m in models)
        lines.append("")
        lines.append(f"{'model':<{width}}  {'calls':>5}  {'in tok':>9}  {'out tok':>8}  {'cost':>9}")
        for model_id, row in sorted(models.items(), key=lambda kv: -float(kv[1].get("cost_usd") or 0)):
            lines.append(
                f"{model_id:<{width}}  {row['calls']:>5}  {row['input_tokens']:>9}  {row['output_tokens']:>8}"
                f"  ${float(row['cost_usd']):>8.4f}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m askonce_bedrock.spend", description="Show Bedrock spend and the remaining budget."
    )
    parser.add_argument("--json", action="store_true", help="print the snapshot as JSON")
    args = parser.parse_args(argv)
    try:
        snap = SpendLedger.from_env().snapshot()
    except LedgerError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(json.dumps(snap, indent=2) if args.json else format_snapshot(snap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
