# AskOnce: Alexa never guesses about your business

**It asks you, once, and won't go live until it passes certification.**

AskOnce is one multi-tenant MCP server that makes small businesses usable from Alexa+ (and any other MCP
host). An onboarding pipeline reads a business's website, asks the owner only what's missing or
contradictory, and a deterministic certifier decides in code whether the business may go live. When a
customer asks something the business never stated, the assistant says *"I don't know. Want me to ask
Rosa?"*, the owner answers once from their phone, and every later customer gets that answer instantly,
with its source.

> **Track:** Alexa+ (self-hosted MCP server, protocol 2026-07-28 over Streamable HTTP, with a 2025-11-25
> fallback). **Mini challenges:** AWS Builder; Open Source (`packages/mcp-tasks`).
>
> - Demo video: https://youtu.be/CmaW0W8vizY
> - Live endpoint: deployed to AWS (ECS Express Mode, ap-southeast-2) for the demo recording and shown in the video;
>   not kept running afterwards, to stay at $0. Deploy your own with one command: [`infra/`](infra/).

---

## Why

Big brands reach Alexa+ with a team of engineers. A corner bakery has a website and no time. Several
projects already turn a website into "an MCP server for a small business". The hard part isn't
generating the integration: it's whether a customer can **trust** what the assistant says about a
business it has never met. AskOnce is built around that:

| Problem | What AskOnce does |
|---|---|
| The assistant makes up a price, an allergen or a policy | Every spoken fact comes from a profile field with a source quote that is re-located on the page snapshot. No source, no statement. |
| The website contradicts itself ("closes at 6" vs "closes at 7") | Onboarding detects it and asks the owner. Until answered, the disputed fact is **withheld**, not guessed. |
| The website doesn't answer the question | The customer is offered a hand-off. The owner's answer is injection-checked, re-certified, and becomes a cited fact (`urn:...:owner-answer`). It is asked **once**. |
| A web page tries to instruct the AI ("ignore previous instructions…") | Text addressing AI agents is quarantined as an injection canary. The certifier fails the business if it ever reaches output. |
| Orders and bookings happen by accident or twice | Nothing is committed without an accepted confirmation (elicitation), and every commitment is one conditional write keyed by an idempotency key. |
| Custom work needs the owner ("cake for 30 on Saturday?") | It becomes a background **MCP Task**. The owner approves, declines or quotes from a phone page; the customer's device announces the result. |
| Amazon can't integrate 33M endpoints | **One** endpoint, `find_business`, then business-scoped tools. A new vertical is a profile template plus certification cases, not code. |

## How it works

```mermaid
flowchart LR
  subgraph Owner
    URL[Website URL]
    Phone[Owner phone page]
  end
  subgraph Onboarding["Onboarding (askonce-onboarding)"]
    Crawl[Crawl + snapshot pages<br/>quarantine injection text]
    Extract[Bedrock: quoted facts per page<br/>+ structure read in code]
    Assemble[Deterministic profile assembly<br/>contradictions → owner questions]
    Certify[Certifier: real MCP server,<br/>trace checked in code]
  end
  subgraph AWS["AWS (ap-southeast-2)"]
    MCP[AskOnce MCP server /mcp<br/>ECS Express Mode · Fargate arm64]
    DDB[(DynamoDB<br/>profiles · orders · tasks)]
    BR[Amazon Bedrock<br/>Nova + Claude-ready router]
  end
  Host[Alexa+ / voice host /<br/>MCP Inspector]

  URL --> Crawl --> Extract --> Assemble --> Certify
  Extract -.-> BR
  Assemble <-->|clarifying questions| Phone
  Certify -->|CERTIFIED → live| DDB
  Host <-->|Streamable HTTP| MCP
  MCP <--> DDB
  MCP -->|Task: needs the owner| Phone
  Phone -->|answer once| MCP
```

1. **Onboard.** `askonce-onboard <url>` crawls the site, snapshots every page by SHA-256, and quarantines
   text that addresses AI agents. One Bedrock call per page extracts quoted facts; price tables, option
   lists, allergen lines, forms and FAQs are read from the page structure in code, because the model can't
   be trusted to group variants or copy an allergen list. Every quote is re-located on its page or the
   fact is dropped. The profile is assembled deterministically and validated against
   [`schema/business-profile.schema.json`](schema/business-profile.schema.json) and invariants L1–L11.
2. **Ask.** Contradictions, missing facts and safety confirmations become owner questions
   (`questions.json`). A fresh profile is **CONDITIONAL**: disputed facts are withheld.
3. **Certify.** `askonce-certify` runs the profile through the real MCP server in process and checks the
   tool-call trace in code (no LLM judge). It writes `certification-verdict.json`: CERTIFIED,
   CONDITIONAL or NOT_CERTIFIED. Only CERTIFIED profiles are visible to customers.
4. **Serve.** Customers use the business through any MCP host. Unknown questions become owner tasks;
   the owner's answer is certified and served to everyone after.

### The certifier

Each demo business runs 62–73 cases (all pass). Pass/fail is decided in code on the trace:

| Rule | Checks |
|---|---|
| `schema_conformance` | JSON Schema + invariants L1–L11; every evidence quote is on its page snapshot |
| `grounded_fact` | every price, allergen, hour and policy in a reply matches a profile field |
| `disputed_fact_withheld` | a fact the owner hasn't settled is never stated |
| `unknown_handed_off` | an unanswerable question offers the owner hand-off instead of a guess |
| `confirm_before_commit` | no booking, order or quote without an accepted confirmation |
| `idempotent_retry` | a retried request never books or orders twice |
| `task_lifecycle` | owner tasks move through the Tasks states correctly |
| `injection_canary` | text planted on the website never appears in output |
| `latency` | p95 tool latency under 500 ms (voice budget), measured in process by the certifier |

Verdicts for the three demo businesses: [`profiles/certification/`](profiles/certification/).

### Real-site census

To check this isn't tuned to our own demo sites, onboarding ran read-only (robots.txt respected,
aggregates only, no names) on **23 real small-business websites**: 19 onboarded (bakeries, salons, home
services, other), 617 priced items, **100% of prices cited by name and amount**, 278 facts dropped because
their quote couldn't be re-located, and **every one** came out CONDITIONAL, with a median of 3 owner questions.
In other words: no real website was complete enough to go live without asking the owner. That is the
case for asking once. Summary: [`onboarding/census/census-summary.json`](onboarding/census/census-summary.json).

## MCP surface

One endpoint, `/mcp`. Protocol **2026-07-28** (stateless; confirmations via multi-round-trip requests,
long waits as SEP-2663 Tasks, MCP Apps `ui://` cards), plus a **2025-11-25** fallback (stateful sessions)
for older clients.

| Tool | Purpose |
|---|---|
| `find_business` | Find a certified business for a need ("gluten-free birthday cake for 30", "Portland") |
| `get_business_info` | Hours, location, contact and summary of one business |
| `get_catalog`, `find_item` | Items, prices, options, allergens, dietary tags, all cited |
| `ask_policy` | Policies and FAQs, matched to how customers actually phrase them |
| `ask_owner` | Hand an unanswered question to the owner (becomes a Task; the answer is reused for everyone) |
| `check_availability`, `book`, `cancel_booking` | Appointments, with read-back confirmation |
| `place_custom_order` | Orders that need owner approval (Task) |
| `check_service_area` | Whether a home-services business covers a ZIP code or town |
| `request_quote` | Home-services jobs: intake fields, owner quote (Task) |
| `get_task`, `reply_to_owner` | Status of an owner task; answer the owner's follow-up question |
| `check_request_status`, `cancel_request` | Plain-tool fallback for hosts without the Tasks extension (Alexa+'s add-on docs don't list it) |

16 tools in total.

Tool results carry spoken text (short, voice-first), the facts used with their sources, and an optional
`ui://` card for hosts with a screen.

## AWS integration (AWS Builder)

| Service | How AskOnce uses it |
|---|---|
| **Amazon Bedrock** (Converse) | Onboarding extraction, policy subjects, customer phrasings; the voice host's tool-calling model. A model router ([`packages/askonce-bedrock`](packages/askonce-bedrock)) spreads calls over Nova Pro, Nova 2 Lite and Claude inference profiles with token buckets, failover and a hard **$5 spend ledger**, because a new account is capped at 10–25 RPM per model. In practice every recorded call ran on **Amazon Nova**: Claude access on our new account was blocked pending Anthropic's use-case form, and the router failed over automatically. |
| **Amazon ECS Express Mode** (Fargate, arm64) | Hosts the MCP server behind the HTTPS load balancer Express Mode creates. |
| **Amazon DynamoDB** | Single table for profiles, orders, bookings, owner requests and idempotency keys; a second table for the Tasks store. On-demand, TTL for expiry. |
| **AWS Secrets Manager** | Request-state, owner-link, id and admin keys, generated by Secrets Manager and injected at deploy. The server refuses to start in prod with any dev key. |
| **Amazon ECR**, **CloudWatch Logs** | Immutable image tags with scan on push; 7-day logs. |
| **AWS CDK** (Python) | One stack, [`infra/`](infra/), pinned to the project Region. `infra/deploy.sh` is idempotent. |

Everything runs in a single Region (ap-southeast-2).

## What's real and what's simulated

| Real | Simulated / stand-in |
|---|---|
| The MCP server, all tools, Tasks, confirmations, cards, DynamoDB storage, the AWS deployment (CDK; deployed for the demo recording) | **Alexa+ itself.** Alexa+ add-ons aren't open for self-serve testing, so the demo uses `host/voice_host.py`: a smart-display-style web page (browser speech in and out) whose MCP client avoids the Tasks extension (Alexa+'s add-on docs don't list it), so it follows owner requests with the plain `check_request_status` tool. The server speaks both protocol eras (2026-07-28 and the 2025-11-25 `initialize` handshake). Any MCP host works, e.g. MCP Inspector. |
| Onboarding on real Bedrock, the certifier, the census of real websites | The three demo businesses are **fictional** sites in [`sites/`](sites/), each with a planted contradiction and a planted injection attempt. |
| The owner phone page and its signed, expiring links | Owner notifications: the owner page polls; no SMS or email is sent. |

## Run it locally ($0, no AWS)

Requirements: Python 3.12, [`uv`](https://docs.astral.sh/uv/), Node.js (for MCP Inspector).

```bash
git clone https://github.com/amithrh/shopfront.git askonce && cd askonce

# 1. Start the server with the three certified demo businesses (in-memory storage)
cd server
ASKONCE_PROFILE_DIRS=../profiles/live \
ASKONCE_CORS_ORIGINS=http://localhost:6274,http://localhost:5173 \
uv run python -m askonce_server            # MCP at http://localhost:8000/mcp
```

In a second terminal:

```bash
# 2a. Explore with MCP Inspector (use the "modern" protocol era)
npx -y @modelcontextprotocol/inspector@2.8.0 --web --transport http \
  --server-url http://localhost:8000/mcp --protocol-era modern
#    Tools tab → find_business  {"need": "gluten-free birthday cake for 30", "locality": "Portland"}

# 2b. Or play the full scripted conversation (bakery order + approval, salon booking, home-repair quote)
cd server && uv run python ../scripts/local_walkthrough.py
```

Act as the owner: mint an inbox link (dev only) and open the printed `inbox_url` in a browser.

```bash
curl -s -X POST -H "Authorization: Bearer DEV-ONLY-admin-token" \
  http://localhost:8000/owner/thimbleberry-bakehouse/links
```

**With Bedrock** (needs AWS credentials with Bedrock access; every call goes through the $5 spend cap):

```bash
AWS_PROFILE=<profile> server/.venv/bin/python scripts/chat.py         # terminal assistant
AWS_PROFILE=<profile> server/.venv/bin/python host/voice_host.py      # voice host: http://localhost:8100
```

**Onboarding and certification:**

```bash
cd onboarding
uv run askonce-onboard --demo            # the 3 demo sites, from recorded model replies ($0)
uv run askonce-certify --demo            # certification-verdict.json per profile (no model)
uv run askonce-answer out/thimbleberry/profile.json fixtures/owner-answers/thimbleberry.json  # owner answers → CERTIFIED
uv run askonce-onboard https://example.com/ --live    # any site, on Bedrock (spend-capped)
```

**As an Agent Skill:** [`skills/askonce-onboard-business/SKILL.md`](skills/askonce-onboard-business/SKILL.md) lets an agent run the whole owner flow: it reads the site, relays each question with its reason (never answering for the owner), re-certifies, and publishes only a CERTIFIED profile, and only after the owner says yes.

More detail: [`docs/local-testing.md`](docs/local-testing.md).

## Deploy to AWS

```bash
AWS_PROFILE=<profile> infra/deploy.sh    # core stack, arm64 image to ECR, ECS Express service, real host
AWS_PROFILE=<profile> uv run --project server --with "botocore[crt]==1.43.111" \
  python scripts/seed_dynamodb.py         # publish the certified demo profiles
```

Prerequisites, costs (≈ $1.50–1.80/day) and teardown: [`infra/README.md`](infra/README.md).

## Tests

```bash
cd server && uv run pytest -q
cd onboarding && uv run pytest -q
cd packages/mcp-tasks && ../../server/.venv/bin/python -m pytest -q
server/.venv/bin/python -m pytest -q packages/askonce-bedrock
cd infra && uv run pytest -q             # CDK synth-level tests
```

No test calls AWS or the network beyond `127.0.0.1` (DynamoDB via botocore `Stubber`, model replies
recorded in fixtures).

## Repository layout

| Path | What |
|---|---|
| [`server/`](server/) | The AskOnce MCP server (FastAPI + `mcp` 2.2.0), owner API and phone page. [README](server/README.md), [architecture](docs/design/server-architecture.md) |
| [`onboarding/`](onboarding/) | Website → profile + owner questions; the certifier; the census. [README](onboarding/README.md) |
| [`packages/mcp-tasks/`](packages/mcp-tasks/) | **Open Source entry:** the SEP-2663 Tasks extension for the MCP Python SDK 2.2, reusable, no AskOnce code |
| [`packages/askonce-bedrock/`](packages/askonce-bedrock/) | Bedrock model router and spend ledger |
| [`host/`](host/) | Reference voice host (Alexa+ stand-in) |
| [`skills/askonce-onboard-business/`](skills/askonce-onboard-business/SKILL.md) | **Agent Skill:** onboard → ask the owner → certify → go live, for any agent that runs skills |
| [`schema/`](schema/), [`profiles/`](profiles/), [`sites/`](sites/) | Business Profile schema, demo profiles and verdicts, fictional demo websites |
| [`infra/`](infra/) | AWS CDK stack and deploy script |
| [`docs/friction-log.md`](docs/friction-log.md) | Friction log kept throughout the build |

## License

[Apache-2.0](LICENSE). All businesses in the demo are fictional.
