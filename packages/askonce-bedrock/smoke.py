"""Live smoke test: ONE Converse call per model id (7 calls at most), through the router.

    AWS_PROFILE=shopfront python packages/askonce-bedrock/smoke.py

Each model gets its own single-model role, so a failure is reported for that model instead of failing
over. Region: AWS_REGION, else ap-southeast-2. Prints one line per model and the router metrics.
Exit code 0 when every model answered, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from askonce_bedrock import ALL_MODELS, BedrockRouter, RouterConfig  # noqa: E402
from askonce_bedrock.router import error_code  # noqa: E402

MESSAGES = [{"role": "user", "content": [{"text": "Reply with exactly the word: ready"}]}]


def main() -> int:
    config = RouterConfig(roles={model_id: (model_id,) for model_id in ALL_MODELS}, max_wait_s=0)
    router = BedrockRouter.from_env(config=config)
    print(f"region: {router.client.meta.region_name}")
    failures = 0
    for model_id in ALL_MODELS:
        try:
            result = router.converse(model_id, MESSAGES, inference_config={"maxTokens": 10, "temperature": 0})
            usage = result.response.get("usage") or {}
            latency = (result.response.get("metrics") or {}).get("latencyMs")
            tokens = f"{usage.get('inputTokens')}+{usage.get('outputTokens')} tokens"
            print(f"OK    {model_id}: {result.text.strip()!r} ({tokens}, {latency} ms)")
        except Exception as exc:  # noqa: BLE001 - report every model
            failures += 1
            attempts = getattr(exc, "attempts", None) or ()
            detail = (
                "; ".join(a.detail or a.outcome for a in attempts) or f"{error_code(exc) or type(exc).__name__}: {exc}"
            )
            print(f"FAIL  {model_id}: {detail}")
    print(json.dumps(router.metrics.snapshot(), indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
