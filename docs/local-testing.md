# Local testing

Run AskOnce on your own machine and try it the way a judge or an MCP host would. Nothing touches AWS:
storage is in memory, so restarting the server clears every order, booking and task.

## 1. The demo businesses (already certified)

The server lists only `live` businesses, and a business goes live only after the deterministic certifier
passes it. `profiles/live/` holds the three demo businesses as certified (verdicts in
`profiles/certification/`: every check decided in code on the real MCP server's trace, every evidence quote
matched against the page snapshot from `sites/`). To re-certify after editing a profile in `profiles/`:

```bash
cd onboarding && uv run python ../scripts/certify_demo.py
```

## 2. Start the server

```bash
cd server && ASKONCE_PROFILE_DIRS=../profiles/live ASKONCE_CORS_ORIGINS=http://localhost:6274,http://localhost:5173 uv run python -m askonce_server
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

## 3c. Chat with it the way Alexa+ would (Bedrock)

`scripts/chat.py` is a terminal voice-assistant stand-in: you type as the customer, a Bedrock model calls the AskOnce
tools and answers in short spoken-style sentences. Its MCP client behaves like Alexa+'s documented client (protocol
2026-07-28, no tasks extension), and when a tool needs the customer (a read-back to confirm, a name and phone) the
server's MRTR question is asked right in the terminal.

```bash
# server running as in step 2, then from the repo root:
AWS_PROFILE=shopfront server/.venv/bin/python scripts/chat.py            # --url http://localhost:8000/mcp
```

Try "Is there a bakery in Portland that does a gluten-free cake for 30 people?", then order one. Each tool call is
traced on a dim line (tool, key arguments, status, task id), and after every turn the running Bedrock spend is
printed. Commands: `/quit`, `/reset` (new conversation), `/spend` (per-model breakdown).

| Option | Default | Meaning |
|---|---|---|
| `--models` | Nova Pro, Nova 2 Lite, then Claude | Host-role model ids, in order (`ASKONCE_BEDROCK_ROLE_HOST` works too). Nova goes first while Claude is blocked on the account. |
| `--max-tokens` | 400 | Output cap per model call (also tightens the worst-case budget check) |
| `--protocol` | `2026-07-28` | `auto` or `legacy` to try the other eras |
| `--debug` | off | Print the model's raw content blocks |

**Spend cap.** Every model call goes through the `askonce-bedrock` spend ledger (`.local/bedrock-spend.json`,
git-ignored): a call whose worst-case cost would take the all-time total past **$5.00** is refused before it is sent.
Change the cap with `ASKONCE_BEDROCK_BUDGET_USD`, check it any time with
`PYTHONPATH=packages/askonce-bedrock/src server/.venv/bin/python -m askonce_bedrock.spend`. A turn costs about
$0.01 to $0.03 with Nova Pro (the tool list alone is ~5K input tokens per call; history grows within a conversation, so
`/reset` keeps it cheap).

The `shopfront` profile comes from `aws login`, which needs `botocore[crt]`; the server venv does not ship it, so the
chat falls back to `aws configure export-credentials` (AWS CLI v2) by itself.

To be the owner while chatting (approve the cake, answer a question), use the owner API below, then ask the assistant
"any news on my cake?".

## 3d. The voice host: a device with a screen (Bedrock)

`host/voice_host.py` is the same loop as the terminal chat, in a browser that looks and behaves like a
smart display: you talk (push the mic button or hold Space; Chrome's Web Speech API) or type, the reply is
spoken (browser text-to-speech), and the tools' cards show on the screen with their website citations.
When a tool needs the customer (a read-back to confirm, a name and phone), its question appears on the screen
and is spoken; answer with Yes/No, by voice, or in the fields.

```bash
# server running as in step 2, then from the repo root:
AWS_PROFILE=shopfront server/.venv/bin/python host/voice_host.py        # http://localhost:8100
```

Owner approvals come back as task ids. The host watches them with `check_request_status` (a plain MCP call,
no model and no Bedrock cost) and **announces the owner's reply the moment it lands**: a chime, a purple
light bar, "Good news: Rosa confirmed your custom cake...". Click **Open owner's phone** in the side panel to
open that business's owner page in a phone-sized window next to the device (the host mints the link with the
dev admin token).

The side panel ("Behind the scenes") shows each tool call with its arguments, status, task id and latency,
the model used, and the turn's and the total Bedrock spend (same $5 ledger as the terminal chat). Without
AWS credentials the host still starts and says so; typing `/tool <name> {json}` calls a tool directly with no
model, which is enough to rehearse confirmations, the owner page and announcements at $0.

## The owner's phone page

Every owner link opens a phone page when a browser follows it: the requests waiting for the owner, each with
its details and one-tap decisions (Approve, Decline, Send quote, Suggest another time, Ask the customer), and,
for a customer question the website couldn't answer, an **Answer** box. The page polls every 3 seconds, so a
new request shows up on its own. It uses the same signed link as the JSON API (a `fetch` with the token gets
JSON, a browser navigation gets the page), keeps the token out of the address bar, and inserts all text with
`textContent`.

## Acting as the owner by hand

In dev, the admin token is `DEV-ONLY-admin-token`; a prod server refuses to start with it. To mint an owner inbox link:

```bash
curl -s -X POST -H "Authorization: Bearer DEV-ONLY-admin-token" http://localhost:8000/owner/thimbleberry-bakehouse/links
```

Open the printed `inbox_url` in a browser for the phone page, or `GET /owner/<business>/inbox?status=open` with the link's token, and `POST .../requests/<id>/decision`.

## Automated tests

```bash
cd server && uv run pytest -q
cd packages/mcp-tasks && ../../server/.venv/bin/python -m pytest -q
server/.venv/bin/python -m pytest -q packages/askonce-bedrock    # router + spend ledger, botocore Stubber
```
