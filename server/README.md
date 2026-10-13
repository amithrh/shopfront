# AskOnce MCP server

One multi-tenant MCP endpoint (`/mcp`) that lets any MCP host find a certified small business and use
it: browse, ask, book, order or request a quote. Answers are grounded in the business's profile, every
commitment is confirmed first, and anything that needs the owner becomes a background task that the
owner settles from a phone page.

- Python 3.12, `mcp==2.2.0`, protocol **2026-07-28** (stateless, MRTR confirmations, the SEP-2663 tasks
  extension, MCP Apps cards), with a fallback for 2025-11-25 clients (stateful SSE sessions).
- FastAPI app: MCP at exactly `/mcp`, `/health` and `/ping`, and the owner REST API under `/owner`.
- Design: [`docs/design/server-architecture.md`](../docs/design/server-architecture.md).

## Run locally

```bash
cd server
uv run python -m askonce_server          # http://localhost:8000/mcp  (dev settings, in-memory, 127.0.0.1 only)
curl -s localhost:8000/health               # {"status":"ok","version":"0.1.0","storage":"memory"}
```

Profiles are loaded from `../profiles/*.json` (falling back to `../schema/example-profile.json` when no
file loads). Only profiles whose certification status is `live` are visible to customers, and the gold
profiles in `../profiles/` stay drafts until the certifier publishes them. To try the tools before
that, serve the live example profile:

```bash
ASKONCE_PROFILE_DIRS=/nonexistent uv run python -m askonce_server   # falls back to the live example
```

## Configuration

All settings are environment variables with the `ASKONCE_` prefix (`askonce_server.config.Settings`).
List values accept a JSON array or a comma-separated string. Every secret has a **DEV-ONLY** default so
`uv run` works out of the box (and dev binds `127.0.0.1` only, because those keys are published).

The settings guard fails closed and reports every problem at once (exit code 2, one line per problem on
stderr, never a secret value):

- **`ASKONCE_ENV=prod`** refuses DEV-ONLY secrets, in-memory storage, `STRICT_INVARIANTS=false`, an
  `ALLOWED_HOSTS` list that cannot match the public `Host` header, a `PUBLIC_BASE_URL` that is not a
  public `https://` URL, `CORS_ORIGINS` the transport's `ALLOWED_ORIGINS` would answer with 403, and an
  admin token shorter than 32 bytes.
- **A deployment that looks real**, whatever `ASKONCE_ENV` says, refuses DEV-ONLY secrets and short admin
  tokens, turns the DEV admin token off and checks profile invariants strictly. It looks real with
  DynamoDB storage, or with any non-local name (dotted, an IP address or `*`) in `PUBLIC_BASE_URL`,
  `ALLOWED_HOSTS`, `ALLOWED_ORIGINS` or `CORS_ORIGINS`. Loopback and single-label names (`testserver`,
  `askonce:8000`) stay local. Exposing a dev server through a tunnel therefore needs real keys.
- **Everywhere**, `ALLOWED_HOSTS` / `ALLOWED_ORIGINS` entries must be shapes the transport can match.

| Variable | Default (dev) | Notes |
|---|---|---|
| `ASKONCE_ENV` | `dev` | `dev`, `test` or `prod`. `prod` requires real secrets, `STORAGE=dynamodb`, strict invariants, a public `ALLOWED_HOSTS` entry, a public `https://` `PUBLIC_BASE_URL`, `CORS_ORIGINS` admitted by `ALLOWED_ORIGINS` and (if set) an admin token of at least 32 bytes. |
| `ASKONCE_HOST`, `ASKONCE_PORT` | `127.0.0.1` (dev/test), `0.0.0.0` (prod; the image sets it), `8000` | The MCP path is fixed at `/mcp`. |
| `ASKONCE_PUBLIC_BASE_URL` | `http://localhost:8000` | Base of the owner links (bearer tokens). Prod: the public `https://` URL. |
| `ASKONCE_ALLOWED_HOSTS` | `localhost:*,127.0.0.1:*` | MCP transport Host allow-list (else 421). Exact names only: no scheme, no path, no wildcard names. Clients on 443 send no port and `name:*` needs one, so list the bare `name` (prod refuses `name:443` or `name:*` without it). |
| `ASKONCE_ALLOWED_ORIGINS` | `http://localhost:*,http://127.0.0.1:*` | MCP transport Origin allow-list (else 403). `scheme://host[:port]`, no path or trailing `/`. |
| `ASKONCE_CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Browser origins for CORS (reference host, owner page). Must also pass `ALLOWED_ORIGINS` (prod refuses one that does not). |
| `ASKONCE_REQUEST_STATE_KEYS` | DEV-ONLY | AES-GCM keys sealing MRTR state, each at least 32 bytes. First seals, all open. **Shared by every replica.** |
| `ASKONCE_REQUEST_STATE_TTL_S` | `900` | How long a customer may take to answer a confirmation. |
| `ASKONCE_OWNER_LINK_KEYS` | DEV-ONLY | HMAC keys for owner links, each at least 32 bytes. First signs, all verify (rotation). |
| `ASKONCE_ID_KEY` | DEV-ONLY | Derives commitment and task ids (bearer capabilities). |
| `ASKONCE_ADMIN_TOKEN` | DEV-ONLY | Bearer token for `POST /owner/{biz}/links`. In prod, or any deployment that looks real, the route exists only with a real token of at least 32 bytes. |
| `ASKONCE_STORAGE` | `memory` | `memory` or `dynamodb`. |
| `ASKONCE_DYNAMODB_TABLE`, `ASKONCE_DYNAMODB_TASKS_TABLE` | `askonce`, `askonce-tasks` | Single table + the reusable tasks table. |
| `ASKONCE_AWS_REGION`, `ASKONCE_DYNAMODB_ENDPOINT_URL` | `ap-southeast-2`, unset | The boto3 client is created lazily on first use. |
| `ASKONCE_PROFILE_DIRS`, `ASKONCE_FALLBACK_PROFILE` | `profiles/`, `schema/example-profile.json` | Memory storage only. |
| `ASKONCE_PROFILE_SCHEMA_PATH` | `schema/business-profile.schema.json` | |
| `ASKONCE_PROFILE_SNAPSHOT_DIR` | unset | Page snapshots `<sha256>.txt`; enables invariant L3. |
| `ASKONCE_PROFILE_CACHE_TTL_S` | `30` | DynamoDB profile read cache. |
| `ASKONCE_STRICT_INVARIANTS` | unset | Unset: strict in prod or any deployment that looks real, lenient for local dev. `true`: every L1-L11 violation rejects a profile; `false` (refused in prod): L3/L7/L11 are warnings. |
| `ASKONCE_TASK_POLL_INTERVAL_MS`, `ASKONCE_TASK_DEFAULT_TTL_MS` | `3000`, 48 h | |
| `ASKONCE_IDEMPOTENCY_TTL_S`, `ASKONCE_OWNER_LINK_TTL_S` | 24 h, 7 d | |
| `ASKONCE_SWEEP_INTERVAL_S` | `60` | Approval-expiry sweep; `0` disables it on this replica. |
| `ASKONCE_OTEL_SERVICE_NAME`, `ASKONCE_LOG_LEVEL` | `askonce-mcp`, `INFO` | |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-*` headers uvicorn trusts (set to the LB's range). |

Secrets belong in Secrets Manager (injected as environment variables at deploy time), never in code or
in a profile. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.

## Try it with MCP Inspector (modern era)

```bash
npx -y @modelcontextprotocol/inspector@2.10.1
```

Open http://localhost:6274, choose **Streamable HTTP**, enter `http://localhost:8000/mcp`, then set
**Server Settings > Protocol Era = Modern** (the default, `legacy`, only exercises the 2025-11-25
path). Tools, MRTR confirmations (the pending-request modal), tasks and the `ui://` cards all work in
the web UI. If the browser reports a CORS error, add the Inspector's origin:
`ASKONCE_CORS_ORIGINS=http://localhost:5173,http://localhost:6274`.

CLI smoke test (the CLI cannot drive `tasks/*`):

```bash
npx -y @modelcontextprotocol/inspector@2.10.1 --cli http://localhost:8000/mcp --transport http \
  --protocol-era modern --method tools/list --strict --format json
npx -y @modelcontextprotocol/inspector@2.10.1 --cli http://localhost:8000/mcp --transport http \
  --protocol-era modern --method tools/call --tool-name find_business \
  --tool-args-json '{"need":"gluten-free birthday cake"}' --format json
```

## Owner API

The owner page has no login: a signed, expiring link is the credential. Tokens are
`base64url(claims).base64url(HMAC-SHA256)`; an **inbox** token covers one business, a **decide**
token (sent in notifications) covers exactly one request. Send the token as
`Authorization: Bearer <token>`, as `?token=`, or as a `token` field in the decision body.

| Route | Purpose |
|---|---|
| `GET /owner/{biz}/inbox?status=open\|all&cursor=&limit=` | Pending requests, with the allowed decisions and response fields. |
| `GET /owner/{biz}/requests/{id}` | One request. |
| `POST /owner/{biz}/requests/{id}/decision` | Body `{"decision", "values", "note", "question"}`. Settles the request and completes its task. |
| `POST /owner/{biz}/links?ttl_s=` | Mints an inbox link (admin bearer token). |

Errors are JSON `{"error", "code"}`: 401 (any token problem, deliberately generic), 404 (unknown
request or business, including another tenant's), 400 (decision not allowed, invalid fields or
request), 409 (no longer pending: already decided, cancelled or expired). Responses are `no-store`.

```bash
# Mint an inbox link (signed with ASKONCE_OWNER_LINK_KEYS, base ASKONCE_PUBLIC_BASE_URL)
uv run python -m askonce_server.app.links mint --business thimbleberry-bakehouse
# ...or through the running server (dev admin token)
curl -s -X POST localhost:8000/owner/thimbleberry-bakehouse/links -H 'Authorization: Bearer DEV-ONLY-admin-token'

TOKEN=...   # the token= part of the link
curl -s localhost:8000/owner/thimbleberry-bakehouse/inbox -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8000/owner/thimbleberry-bakehouse/requests/<id>/decision \
  -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"decision":"approve","note":"See you Sunday"}'
```

## Tests

```bash
cd server && uv run pytest -q
cd packages/mcp-tasks && ../../server/.venv/bin/python -m pytest -q
```

No test calls AWS or the network beyond `127.0.0.1`: DynamoDB code is tested with botocore `Stubber`,
and the HTTP tests run uvicorn on a free local port. Tests that need a work package that has not landed
yet are skipped with `pending work package: <name>`.

## Deploying

```bash
# from the repository root (/.dockerignore allow-lists the build context)
docker buildx build --platform linux/arm64 -f server/Dockerfile -t askonce-mcp --load .
docker run --rm -p 127.0.0.1:18000:8000 -e ASKONCE_ENV=dev -e ASKONCE_STORAGE=memory \
  -e ASKONCE_PUBLIC_BASE_URL=http://localhost:18000 \
  -v "$PWD/.local/profiles-live:/app/profiles-live:ro" askonce-mcp
server/.venv/bin/python scripts/local_walkthrough.py --url http://localhost:18000
```

AWS (ECS Express Mode, DynamoDB, Secrets Manager, ECR): see `infra/README.md` (`infra/deploy.sh`).

- **AgentCore Runtime:** the image serves `0.0.0.0:8000/mcp` on ARM64 (`python -m askonce_server`).
  Clients on 2026-07-28 send no `Mcp-Session-Id`, so nothing relies on microVM affinity: all state is in
  DynamoDB and every replica shares `ASKONCE_REQUEST_STATE_KEYS`.
- **ECS Fargate + ALB:** raise the ALB idle timeout above 60 s if tools can run long (the server sends SSE
  keep-alive pings every 15 s and keeps idle HTTP connections for 75 s), set `FORWARDED_ALLOW_IPS`, and
  point health checks at `/health`. Legacy (2025-11-25) sessions live in one process: run one worker per
  container (the default) and one task, or enable stickiness.
- Set `ASKONCE_ALLOWED_HOSTS` to the exact public `Host` values (for example `mcp.example.com`; clients
  on 443 send no port, so `mcp.example.com:443` or `mcp.example.com:*` alone would answer every MCP
  request with 421, and prod refuses them), and `ASKONCE_PUBLIC_BASE_URL` to the public `https://` URL
  owners open.
- Running the image locally: publish on loopback only (`docker run -p 127.0.0.1:8000:8000 ...`); the image
  binds `0.0.0.0` inside the container.
