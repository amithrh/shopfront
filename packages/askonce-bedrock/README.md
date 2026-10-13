# askonce-bedrock

A small Amazon Bedrock model router for the AskOnce components that call models directly (onboarding,
the certifier's simulated customers, the reference voice host). The MCP server itself never calls a model.

A new AWS account is capped at about 10 requests per minute per model. The router spreads Converse
calls for a **logical role** across an ordered list of model ids and inference profiles, so no single
model is pushed past its budget:

| Piece | Behaviour |
|---|---|
| Per-model token bucket | Default 9 RPM for Claude, 18 for Nova, burst 1 (any 60-second window holds at most RPM + 1 calls). Configurable per family and per model. |
| Routing | A call goes to the first model of the role that has a token now. If none has one, it waits for the earliest token, up to `max_wait_s` (default 60). |
| Throttling | `ThrottlingException` (and `ServiceQuotaExceededException`, `TooManyRequestsException`, `ModelNotReadyException`) cools that model down with exponential backoff and full jitter, then the call fails over to the next model at once. |
| Unusable models | `AccessDeniedException`, `ResourceNotFoundException`, or a `ValidationException` about the model id park the model for 10 minutes and fail over. |
| Transient errors | `ServiceUnavailableException`, `InternalServerException`, `ModelTimeoutException` and connection errors fail over. |
| Bad requests | Any other error (for example a malformed message list) raises at once: it would fail on every model. |
| Metrics | `router.metrics.snapshot()`: calls, successes, throttles, errors, unavailable, tokens and latency per model. |
| Spend cap | A persistent cost ledger with a hard budget (default $5.00); see below. |

Only `boto3` is required. License: Apache-2.0.

## Spend cap

Every call is priced from the Converse `usage` (input, output and prompt-cache tokens) with a deliberately
conservative price table (`PRICES_PER_MTOK` in `ledger.py`, sources cited there; unknown models are priced like
the most expensive Claude tier) and added to a persistent ledger:

| Piece | Behaviour |
|---|---|
| Ledger file | `<repo root>/.local/bedrock-spend.json` (git-ignored); `ASKONCE_BEDROCK_LEDGER` overrides it. Shared by every process on the machine: `flock` on a sidecar `.lock` file, atomic writes. A file that cannot be parsed raises `LedgerError`; it is never reset to $0. |
| Hard cap | `ASKONCE_BEDROCK_BUDGET_USD`, default **$5.00** in total. Before each attempt the router reserves the call's worst case (request characters / 3 input tokens, +600 with tools, plus `maxTokens` output, 8192 if unset) at that model's price. If spent + in-flight reservations + this estimate would pass the cap, `BudgetExceeded` is raised and nothing is sent. |
| Settling | A successful call replaces its reservation with the actual cost (`RouterResult.cost_usd`); a failed or interrupted one releases it. Reservations of dead processes are dropped. |
| Opt-out | The ledger is on by default (`BedrockRouter(...)` and `from_env`); pass `ledger=None` to turn it off. |

```bash
python -m askonce_bedrock.spend          # spent, remaining, per-model calls/tokens/cost
python -m askonce_bedrock.spend --json
```

## Roles

| Role | Models, in order |
|---|---|
| `extract` | `au.` Sonnet 4.6, `global.` Sonnet 4.6, `apac.` Nova Pro, `au.` Haiku 4.5, `global.` Haiku 4.5 |
| `host` | `au.` Sonnet 4.6, `global.` Sonnet 4.6, `au.` Haiku 4.5, `global.` Haiku 4.5, `apac.` Nova Pro |
| `simulate` | `au.` Haiku 4.5, `global.` Haiku 4.5, `apac.` Nova Lite, `global.` Nova 2 Lite, `apac.` Nova Pro |
| `cheap` | `apac.` Nova Lite, `global.` Nova 2 Lite, `au.` Haiku 4.5, `global.` Haiku 4.5 |

## Use

```python
from askonce_bedrock import BedrockRouter

router = BedrockRouter.from_env()  # AWS_PROFILE credentials; region AWS_REGION, else ap-southeast-2
reply = router.converse(
    "extract",
    messages=[{"role": "user", "content": [{"text": "List the opening hours on this page: ..."}]}],
    system="Answer in JSON.",
    inference_config={"maxTokens": 512, "temperature": 0},
)
print(reply.text, reply.model_id, router.metrics.snapshot())
```

`router.run(role, op)` routes any other runtime call: `op(client, model_id)` is called with the chosen
model, for example `lambda c, m: c.converse_stream(modelId=m, messages=...)`. The router is synchronous
and thread-safe; from asyncio, use `await asyncio.to_thread(router.converse, ...)`.

`from_env` builds the `bedrock-runtime` client with botocore retries off (`total_max_attempts=1`),
because the router does the retrying. Pass your own client to `BedrockRouter(client, config)` to
manage that yourself.

## Configuration (environment)

| Variable | Example | Meaning |
|---|---|---|
| `ASKONCE_BEDROCK_ROLE_<ROLE>` | `ASKONCE_BEDROCK_ROLE_HOST=au.anthropic.claude-haiku-4-5-20251001-v1:0,apac.amazon.nova-pro-v1:0` | Replace a role's model list, or add a role |
| `ASKONCE_BEDROCK_CLAUDE_RPM` / `ASKONCE_BEDROCK_NOVA_RPM` | `20` | Family defaults |
| `ASKONCE_BEDROCK_RPM` | `apac.amazon.nova-lite-v1:0=40,au.anthropic.claude-sonnet-4-6=5` | Per-model overrides |
| `ASKONCE_BEDROCK_BURST` | `2` | Bucket size |
| `ASKONCE_BEDROCK_MAX_WAIT_S` | `15` | Longest a call waits for capacity |
| `ASKONCE_BEDROCK_BUDGET_USD` | `5` | Total spend cap across all runs |
| `ASKONCE_BEDROCK_LEDGER` | `/tmp/spend.json` | Ledger file (default `<repo root>/.local/bedrock-spend.json`) |

## Tests

```bash
cd packages/askonce-bedrock
../../server/.venv/bin/python -m pytest -q     # botocore Stubber, no network
```

Live smoke test (one Converse call per model id, 7 calls at most):

```bash
AWS_PROFILE=shopfront uv run --no-project --python 3.12 \
  --with boto3==1.43.111 --with "botocore[crt]==1.43.111" python smoke.py
```

A profile created by `aws login` needs `botocore[crt]`; the project venvs do not ship it.
