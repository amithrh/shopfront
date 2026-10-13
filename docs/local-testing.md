# Local testing

Run Shopfront on your own machine and try it the way a judge or an MCP host would. Nothing touches AWS:
storage is in memory, so restarting the server clears every order, booking and task.

## 1. Seed the demo businesses as "live"

The server lists only certified (`live`) businesses. Until the certifier is built, this dev-only script writes
promoted copies of the gold profiles to `.local/profiles-live/` (git-ignored). Their certification run is
labelled `dev-seed`, which is not a real certification.

```bash
server/.venv/bin/python scripts/dev_seed_live.py
```

## 2. Start the server

```bash
cd server && SHOPFRONT_PROFILE_DIRS=../.local/profiles-live SHOPFRONT_CORS_ORIGINS=http://localhost:6274,http://localhost:5173 uv run python -m shopfront_server
```

MCP is served at `http://localhost:8000/mcp` (protocol 2026-07-28, with a legacy 2025-11-25 fallback). The owner REST
API is under `/owner/...`.

## 3a. Explore by hand with MCP Inspector

```bash
npx -y @modelcontextprotocol/inspector@2.8.0 --web --transport http --server-url http://localhost:8000/mcp --protocol-era modern
```

Open the printed URL, click the toggle on `localhost:8000` to connect, then use the **Tools** tab. Start with
`find_business` (need: "gluten-free birthday cake for 30", locality: "Portland").

## 3b. Play the full scripted conversation

```bash
cd server && uv run python ../scripts/local_walkthrough.py
```

The script plays a customer and the owners:

| Business | What happens |
|---|---|
| Bakery | 30-person cake, confirmation read-back, owner approval, confirmation spoken back |
| Salon | Slots in Austin time, booking with a spoken booking code |
| Home repair | Service-area check, quote request, the owner's $185 quote |

Every line the assistant would say is printed. Restart the server before re-running: identical requests are
deliberately deduplicated (idempotent), so a second run replays the first run's results.

## Acting as the owner by hand

In dev, the admin token is `DEV-ONLY-admin-token`; a prod server refuses to start with it. To mint an owner inbox link:

```bash
curl -s -X POST -H "Authorization: Bearer DEV-ONLY-admin-token" http://localhost:8000/owner/thimbleberry-bakehouse/links
```

Then `GET /owner/<business>/inbox?status=open` with the link's token, and `POST .../requests/<id>/decision`.

## Automated tests

```bash
cd server && uv run pytest -q
cd packages/mcp-tasks && ../../server/.venv/bin/python -m pytest -q
```
