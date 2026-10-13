# Shopfront MCP server: architecture (v1)

The MCP server is the product: one multi-tenant endpoint at `/mcp`. Through it, any MCP host can find a
certified small business and then use it: browse, ask, book, order or request a quote. Answers are
grounded in the business's profile, and the owner stays in control of anything that needs approval.

This document is the contract for the five implementation work packages (section 19). The skeleton in
`server/src/shopfront_server/` and `packages/mcp-tasks/` matches it signature for signature.
`uv run pytest -q` passes on the skeleton: 11 architecture tests pass, and the acceptance tests skip
until their work packages land.

Inputs: `PLAN.md` (sections 0-4), `docs/design/profile-schema.md`, and `docs/research/*.md`. Every SDK
behaviour relied on below was verified there or in an architect probe (marked *probe*).

---

## 0. Principles

1. **Stateless protocol, durable state.** Protocol 2026-07-28 has no sessions and no server-to-client
   requests, so:
   - every confirmation is MRTR (`Resolve` + `Elicit`);
   - every long wait is a SEP-2663 Task;
   - every piece of state lives in a store behind a port (memory for tests, DynamoDB for AWS).

   Any replica can serve any request.
2. **The profile is the only tenant-specific thing.** Tools never branch on a business. A new vertical
   needs a template and certification cases, not code.
3. **Nothing is said that is not a statable profile fact.**
   - Speech is built from templates over facts, and every result lists the facts it used.
   - Unknown facts become a hand-off to the owner.
   - Disputed facts are withheld (L6).
4. **Nothing is committed without confirmation, and nothing is committed twice.**
   - Every commitment follows an accepted elicitation.
   - Each commitment is one atomic, conditional write keyed by a deterministic idempotency key.
5. **Small modules behind ports.** The dependency direction is fixed: `profile` ← `domain` ← `tools` ←
   `app`. `mcp_tasks` is independent and generic.

## 1. Package layout

```
packages/mcp-tasks/                      reusable, Apache-2.0, no Shopfront code      [tasks]
  src/mcp_tasks/models.py                TaskRecord, statuses, wire()
  src/mcp_tasks/store.py                 TaskStore protocol, InMemoryTaskStore, DuplicateTask
  src/mcp_tasks/manager.py               TaskManager state machine (CAS transitions)
  src/mcp_tasks/server.py                TasksExtension (interceptor + tasks/* methods), principal_of
  src/mcp_tasks/client.py                TasksClientExtension, get/update/cancel/list_tasks
  src/mcp_tasks/dynamodb.py              DynamoTaskStore
  tests/

server/src/shopfront_server/
  __init__.py  __main__.py  config.py  deps.py  observability.py               [app]
  clock.py  errors.py  ids.py                                                  [domain]
  canonical.py                                                                 [profile]
  profile/   model.py schema.py invariants.py loader.py snapshots.py
             factref.py statable.py directory.py store.py dynamodb.py           [profile]
  domain/    models.py planning.py ports.py intake.py pricing.py approval_rules.py
             availability.py idempotency.py
             services/{base,booking,custom_order,quote,owner}.py
             stores/{memory,dynamodb}.py                                        [domain]
  tools/     server.py context.py schemas.py grounding.py speech.py guard.py
             confirm.py search.py cards.py render.py
             discovery.py catalog.py knowledge.py booking.py orders.py quotes.py owner.py   [tools]
  app/       main.py wiring.py security.py links.py owner_api.py notifier.py
             background.py run.py                                               [app]
server/tests/
  conftest.py support.py test_architecture.py                                  [app]
  test_acceptance.py                                                           [tools]
  profile/  domain/  tools/  app/        one directory per work package
```

**Frozen contracts.** These are written in full now. A change to any of them must be agreed by every
work package that uses it.

| File | Contract |
|---|---|
| `domain/models.py` | Commitment record and its state machine |
| `domain/planning.py` | Plans and problem codes |
| `domain/ports.py` | Store, notifier and renderer protocols |
| `tools/schemas.py` | Every tool's output schema (the grounded-answer contract) |
| `deps.py`, `config.py` | Wiring and settings |
| `profile/model.py` | The method list |
| `mcp_tasks` | The public API |

## 2. Layering and dependencies

```
mcp_tasks  (mcp, mcp-types, anyio, pydantic)          generic, no Shopfront imports
profile    -> canonical, clock, errors                read-only views of profile documents
domain     -> profile, mcp_tasks, ids, clock, errors  rules, plans, commits, owner decisions
tools      -> domain, profile, mcp_tasks, mcp         MCP surface, speech, grounding, cards
app        -> tools, domain, profile, mcp_tasks       HTTP, wiring, owner API, run entrypoint
```

- The domain reaches the tools layer only through the `DecisionRenderer` port, and the app layer only
  through the `OwnerNotifier` port.
- Tool modules **do not** use `from __future__ import annotations`. The SDK evaluates tool and resolver
  annotations, and a factory-local resolver under that import fails registration.

**Dependencies.** All pinned, published before 2026-09-28, and already added:

| Group | Packages |
|---|---|
| Runtime | `mcp==2.2.0`, `pydantic==2.13.5`, `pydantic-settings==2.15.0`, `fastapi==0.141.1`, `uvicorn==0.54.0`, `httpx==0.28.1`, `jsonschema>=4.26`, `boto3>=1.43.111`, `opentelemetry-api==1.45.0`, `rfc8785==0.1.4`, `tzdata==2026.4` |
| Path dependency | `mcp-tasks` (editable, `../packages/mcp-tasks`) |
| Dev | `opentelemetry-sdk==1.45.0`, `pytest==9.1.1`, `pytest-asyncio==1.4.0` |
| Constraint | `python-dotenv==1.2.3`, to keep a transitive dependency under the date cutoff |

**No work package may run `uv add`.** If a package is genuinely missing, ask the architect.

## 3. Transport and request lifecycle

```
client ──HTTP POST /mcp──▶ CORSMiddleware ─▶ Route("/mcp", StreamableHTTPASGIApp(session_manager))
        (TransportSecuritySettings: Host allow-list 421, Origin allow-list 403)
        ├─ MCP-Protocol-Version: 2026-07-28 → modern per-request path (no session, MRTR, tasks)
        └─ handshake version / no header   → legacy stateful SSE path (sessions in-process)
   MCPServer("shopfront") middleware: SDK OpenTelemetry span → RequestStateBoundary
       → OutputGuardMiddleware (tools.guard) → extensions: TasksExtension interceptor → Apps
       → tool function (Resolve resolvers first, then the body)
```

**App assembly** (`app.main.create_app`, verified in `mcp-http-transport.md` §3.8):
- Call `server.streamable_http_app(json_response=False, stateless_http=False, transport_security=...)`
  **once**.
- Append `Route("/mcp", endpoint=StreamableHTTPASGIApp(server.session_manager))`. This serves exactly
  `/mcp` with no 307.
- In the FastAPI lifespan, `async with server.session_manager.run()`.
- `/health` and `/ping` give an unauthenticated 200.

**Settings that must hold:**
- `json_response=False`. It keeps progress events and the 15 s SSE pings that stop proxy idle
  timeouts.
- `stateless_http=False`, so legacy clients keep their sessions.
- An explicit `TransportSecuritySettings(allowed_hosts, allowed_origins)` from env. Never use the SDK's
  localhost-only default.

**Replicas.** Every replica shares two things:
- `RequestStateSecurity(keys=[...], ttl=SHOPFRONT_REQUEST_STATE_TTL_S)`, with keys of at least 32
  bytes, from env;
- the fixed server name `shopfront`, which is the request-state audience.

A confirmation round can therefore land on another replica.

**AgentCore Runtime:**
- It requires `python -m shopfront_server`, which serves `0.0.0.0:8000/mcp` on ARM64 (the image sets
  `SHOPFRONT_HOST=0.0.0.0`; outside the image, dev binds `127.0.0.1` because its keys are published).
- AgentCore pins a microVM by `Mcp-Session-Id`, but 2026-07-28 clients never send one. The design does
  not depend on affinity: all state is in DynamoDB.

**Protocol eras:**
- **2026-07-28** gets MRTR, tasks and Apps.
- **2025-11-25** clients:
  - `Resolve`/`Elicit` falls back to synchronous `elicitation/create` over stateful SSE;
  - tasks degrade to the synchronous fallback plus `get_task`.

## 4. Multi-tenancy and isolation

- **One endpoint.** `find_business(need, category?, locality?)` searches the directory of **live**
  profiles only. Every business-scoped tool takes `business_id`, and the first thing it does is
  `profiles.get_live(business_id)`.
- **Unknown and non-live tenants look the same.** Both get `UnknownBusiness`, surfaced as `ToolError`
  "Unknown business".
- **Tenant isolation:**
  - Commitments are keyed `BIZ#<business_id>`, and every read passes `business_id`.
  - `CommitmentStore.get(business_id, id)` never returns another tenant's record.
  - Owner tokens are bound to one `business_id`, and decisions re-check that the commitment belongs
    to it.
  - Task ids are 128-bit HMAC-derived capabilities.
  - Tasks are additionally bound to the principal (the authenticated identity, or `anonymous`).
- **Owner contact details stay out of the profile.** Contact handles and secrets for the owner channel
  live in settings and tenant config (the schema forbids them in the profile).

## 5. The grounded-answer contract

Every tool returns a model derived from `tools.schemas.Grounded`:

| Field | Rule |
|---|---|
| `speak` | ≤ 240 chars, no URLs, markup, `_` or newlines (validator `check_spoken`, the same rule as the schema's `spoken`). This is the only text a voice host reads aloud. It is built by `tools.speech` templates from fact values. |
| `facts[]` | `{fact_ref, value, evidence_url, owner_confirmed}` for every fact the speech or decision relied on. Prices are in cents, exactly as in the profile. |
| `card` | Display payload: title, lines, badges, **citations**, actions. Rendered by `ui://` cards or as text. |
| `handoff` | `{reason: unknown\|disputed\|not_offered\|needs_owner, approver_spoken_name, suggested_question, fact_ref, tool: "ask_owner"}`. |
| `business_id`, `profile_revision`, `trace_id` | Lets the certifier bind a claim to a profile revision and an OTel trace. |

**How a fact becomes speech.** The only path is `tools.grounding.state(profile, ref)`:
1. `profile.factref.resolve` resolves the ref, using L5 nearest provenance.
2. `profile.statable.statability` decides whether it may be said:
   - **absent:** `unknown_answer` gives "I don't know that. Want me to ask Rosa?" plus a hand-off;
   - **disputed** (an open `contradiction` or `confirm` question targets an overlapping ref):
     `withheld_answer` gives "Rosa is double-checking that…" and never mentions either candidate;
   - **safety facts** (allergens, dietary, `facility_may_contain`) need `owner_confirmed`. Every
     allergen answer also carries the catalog's `facility_may_contain`.

**Output guard** (`tools.guard.OutputGuardMiddleware`, server middleware applied to every `tools/call`):
- **L8 canaries.** If any injection-canary quote of the business appears in any string of the result,
  the result is replaced with a safe hand-off and traced. The hand-off names the approver only when
  the approver's own name carries no canary.
- **`speak` is sanitised** rather than crashing.
- **Unparseable fact refs are dropped.**
- **MRTR backstop.** A 2026-07-28 `input_required` round whose `inputRequests` (any string or property
  name) carry a canary of the called business becomes an error result. It never fires in practice:
  every elicitation is built by `confirm.guarded_elicit` (below), which already refuses canaries.

Task results built later by the renderer pass through the same checks. Profile and owner text also
reaches the customer outside tool results, and each of those paths is checked too, with the same
normalisation and always on decoded text (a `json.dumps` copy escapes curly quotes, accents and double
quotes):
- **Elicitations** (MRTR rounds and legacy `elicitation/create`, which the middleware never sees):
  `confirm.guarded_elicit` checks the message and the rendered form. `test_architecture.py` pins it as
  the only place an `Elicit` is built.
- **The owner's relayed question** (`request_info`): `OwnerService.decide` builds the input request
  (`owner_question_request`: "Rosa asks: ..." and the task's status message) and checks it before
  anything is stored.
- **Task objects** (the `tools/call` handle and `tasks/get`): `tools.guard.TaskViewGuard`, the tasks
  extension's `present` hook, re-checks every view against the business's current canaries. A canary
  added after a question was stored is replaced by a generic message; the form is kept, so the answer
  still validates. The stored record is unchanged.

**What the certifier can check deterministically:**
- every number or claim in `speak` equals a `facts[].value`, and every fact is statable in
  `profile_revision`;
- no canary appears anywhere;
- commits follow an accepted confirmation;
- retries replay the same reference.

## 6. Tool catalogue

All tools are registered by `tools.server.build_mcp_server`, in this order. Output schemas are in
`tools/schemas.py`. Every tool is Apps-bound (`_meta.ui.resourceUri`, visibility model+app). Hidden
parameters (`ctx`, `plan`, `confirmation`) are filled by resolvers and never appear in `inputSchema`
(verified by `test_architecture.py`).

**Schema portability.** Every subschema of every `inputSchema` and `outputSchema` declares what it
accepts (`type`, `enum`, `const`, `$ref` or `anyOf`). Free-form JSON (`Fact.value`,
`CardAction.arguments`, `TaskStatusResult.result`) is `schemas.JsonData`, an explicit recursive
`anyOf` of the seven JSON types in `$defs.JsonData`, never `Any` / `pydantic.JsonValue` (whose schema
is the untyped `{}` the MCP Inspector flags and some clients refuse). Its members are strict, so it
validates exactly like `JsonValue`. Tool inputs stay `$defs`-free: `details` is
`dict[str, str | int | float | bool | list[...] | None]` inline. `tests/tools/test_schema_portability.py`
walks every schema and fails on untyped or boolean `true` subschemas, type arrays, remote or dangling
`$ref`s, nested `$defs`, 2019-09+/conditional/vendor keywords, undeclared `required` names and
defaults that do not validate.

**Annotations:**
- **R** = read-only, idempotent, closed-world: reads the published profile and the server's own
  records, nothing leaves the server.
- **C** = writes, idempotent, not destructive, open-world: it puts a request (or a reply) in front of
  a real business owner. An identical retry replays the first request.
- **Q** = writes, **not** idempotent, not destructive, open-world (`ask_owner`): without auth every
  call mixes a fresh nonce into its key so strangers' identical questions never merge, so a retry asks
  the owner again.
- **D** = destructive, idempotent, open-world: withdraws a real appointment (or a request still waiting
  for the owner) at a real business.

`tests/tools/test_annotations.py` pins these on `tools/list`.

| Tool | Inputs (required in bold) | Output | Ann. | Card |
|---|---|---|---|---|
| `find_business` | **need**, category, locality, limit≤10 | `FindBusinessResult{businesses[BusinessMatch]}` | R | business |
| `get_business_info` | **business_id**, topics[summary,hours,location,contact], on_date | `BusinessInfoResult{name, open_now?, hours?, address, phone}` | R | business |
| `get_catalog` | **business_id**, category, query, limit≤25 | `CatalogResult{items[ItemSummary], total}` | R | items |
| `find_item` | **business_id**, **item**, aspects[price,options,allergens,dietary,duration,availability,description] | `ItemDetailResult{item, options, allergens?, may_contain?, dietary?, facility_may_contain?, offered_via, matched_options?}` | R | items |
| `ask_policy` | **business_id**, **question**, topic | `PolicyResult{entry_ref, topic}` (`answer_spoken` returned verbatim as `speak`) | R | business |
| `check_availability` | **business_id**, **item**, date_from, date_to, party_size, module_id, resource, limit | `AvailabilityResult{slots[SlotView]}`: times (booking) or days (custom order, quote) | R | commitment |
| `book` | **business_id**, **item**, **start**, party_size, details{}, module_id, resource | `CommitmentResult` | C | commitment |
| `cancel_booking` | **business_id**, **reference** (or the 8-digit booking code) | `CommitmentResult` | D | commitment |
| `place_custom_order` | **business_id**, **item**, choices{}, date, details{}, module_id | `CommitmentResult` (may be a Task) | C | commitment |
| `check_service_area` | **business_id**, postal_code, locality, module_id | `ServiceAreaResult{served: bool\|None}` | R | business |
| `request_quote` | **business_id**, **item**, date, visit_window, details{}, module_id | `CommitmentResult` (always a Task) | C | commitment |
| `ask_owner` | **business_id**, **question**, fact_ref | `CommitmentResult` (always a Task) | Q | task |
| `get_task` | **task_id** | `TaskStatusResult{task_status, status_message, commitment, result}` | R | task |
| `reply_to_owner` | **task_id**, **answer** (≤ 1000 chars) | `TaskStatusResult` | C | task |

**`CommitmentResult`** has `status`, `reference`, `task_id`, `commitment: CommitmentView`,
`alternatives[SlotView]`, `missing_fields` and `replayed`.
- It is one schema for every path: a synchronous answer, the pending fallback, and the final task
  result rendered after the owner decides.
- That matters because `Client.call_tool` re-validates a task's final result against the tool's
  `outputSchema`.
- `status` takes these values:

| Group | Statuses |
|---|---|
| Stored commitment states | `confirmed`, `pending_owner`, `approved`, `declined`, `counter_offer`, `suggested_time`, `quoted`, `info_requested`, `answered`, `cancelled`, `expired` |
| Nothing stored | `not_confirmed`, `needs_info`, `unavailable`, `not_offered` |

**Data flow per tool:**
- **find_business.**
  1. `profiles.list_live()` returns `DirectoryEntry[]`.
  2. `search.rank(need, category, locality)` scores category synonyms, item terms, dietary tags,
     keywords and locality. An item also matches when one item's words (its terms and its option
     choices', `DirectoryEntry.item_term_groups`) explain most of the need, its last word included
     ("someone to put together ikea furniture" -> furniture assembly).
  3. The result is the top N, with `matched_on`.
  4. Facts are `business#name` and `business#categories` for each match.
- **get_business_info.**
  1. `get_live`.
  2. `state(locations/<id>#hours)`: withheld if disputed.
  3. `open_now` is computed in the business timezone with `clock`.
  4. Address and contact come from `business#contact` and `locations/<id>#address`.
- **get_catalog / find_item.**
  1. `get_live`.
  2. The item is resolved by `tools.matching` (shared with check_availability, book,
     place_custom_order and request_quote): ref or id, exact name / spoken name / synonym, then a
     scored match over the customer's words. Dietary phrases ("gluten-free", "no nuts") and head counts
     ("for 30", "serves 25") are modifiers; filler ("a", "my", "fix", "please") never needs explaining;
     plurals are folded, and so are word forms of catalog words ("clogging" -> clog, "draining" ->
     drain, "replace" -> replacement, "12" -> twelve). A count ("two dressers", "3 chairs") needs no
     explaining, and a size ("75 inch", "2 by 2 foot") picks the option choice whose published range
     holds it ("up to 2 by 2 feet"). Each item is scored by how its terms, its option choices' terms
     ("10 inch", "carrot cake", "long hair"; inside an item its own words are implied, so the drain
     cleaning's "sink drain" is "sink") and its vocabulary explain the remaining words; words may be
     left over ("clogged kitchen sink") only when more are explained and the head of the phrase is
     ("pumpkin pie" never borrows the hand pie). Modifiers are checked against statable facts only: a
     dietary tag holds when the item's owner-confirmed tags carry it or one option choice carries it
     (that choice becomes the focus); a head count holds when the item or a choice serves that many.
     Ranking: unmet dietary tags, unmet head count, unexplained words, whether the head is explained
     ("fix a leaky faucet" is the faucet), match kind; the longest match breaks a tie only when nothing
     was left unexplained.
  3. Several equally good items give "Did you mean ...?", and so does an item found only by correcting
     a one-letter misspelling ("drywal patch"); an unknown one gives the `not_offered` hand-off, and so
     does a match whose leftover words the business's not-offered statement names (a knowledge entry
     with topic `not_offered`: "replace my sewer line" is not the drain cleaning); a dietary need no
     candidate is confirmed to meet gives "I don't know whether the custom cake comes nut-free. Want me
     to ask Rosa?" (also from check_availability and, as `not_offered`, from the commit tools: never an
     order for the plain item).
  4. find_item leads with the focus choices ("The custom cake has gluten-free cake flavors for $12 more
     per tier", "The smallest custom cake for that many is the ten inch round, which serves up to 36
     for $120", "For the custom cake, the cake flavor can be carrot with walnuts") and returns them as
     `matched_options` (price, price delta, owner-confirmed allergens and dietary tags, serves).
     get_catalog(query) lists every item that meets the modifiers.
  5. Price facts are `<item ref>#price` and choice prices / price deltas.
  6. Allergens use `state(<item>#allergens)` and `state(<catalog>#facility_may_contain)`: unknown gives
     a hand-off, never "no allergens". With a focus, the focus choices' allergens are spoken, plus what
     the other option groups (filling, frosting) can add.
- **ask_policy.**
  1. Match `question` and `topic` against the entries' `question_variants` and `topic`, using a token
     overlap score with a threshold (`tools.knowledge`). An entry whose variants and spoken answer
     together contain every word of a multi-word question also qualifies, and topics that leave fewer
     of the question's words unexplained rank first ("how fast will I get a quote" -> the turnaround
     entry, not "How do I get a quote?"). The business's multi-word place names are single words, so
     "parking" never matches the Central Park entry.
  2. `speak = answer_spoken` (verbatim). Facts are `<module>/<entry>#answer_spoken` and `#facts.*`.
  3. With no match above the threshold, the reply is the hand-off.
- **check_service_area.** `QuoteService.check_service_area` matches the module's service areas
  (postal codes; localities by case, punctuation, plural, a trailing state and short forms like "Cap
  Hill"). A location they don't list is a definite "no" only when a knowledge entry with
  `facts.service_zone: outside` lists its postal code or names the place, or when a postal code is in
  another region (no listed or referred-out code shares its first three characters). Anything else
  is `served=None`: an unlisted town asks for the ZIP code, an unlisted ZIP hands off to the owner,
  and request_quote sends the request (the owner decides).
- **check_availability.**
  1. The module type of the item's transactional module picks the method: `BookingService.open_slots`,
     or `CustomOrderService.open_days` / quote days.
  2. The result is `SlotView[]` with a spoken label. Facts are `<module>#availability.*`,
     `#slotting.*` and the hours ref.
- **book / place_custom_order / request_quote:** see §7 (MRTR) and §8 (commit).
- **cancel_booking.**
  1. `reference` is the booking's reference, or its spoken **booking code** (`booking.booking_id`,
     below).
  2. `plan_cancel` reads back the booking and the cancellation policy entry.
  3. Confirmation.
  4. `BookingService.cancel`: a CAS to `cancelled` that releases the holds atomically.
  5. Re-cancelling replays.

  **Booking code (VUX-11).** A voice host reads only `speak`, and the reference (`c_` + 26 base32
  characters) cannot be said back. So a confirmed or approved booking also says an 8-digit code ("Your
  booking code is 0 4 9 7, 3 0 7 3."), kept as the `tail` of the speech so the 240-character cap never
  drops it, shown on the card ("Booking code") and cited as a fact (`<module>/<id>#booking_code`,
  evidence `urn:shopfront:commitment:<id>`).
  - The code is the first 5 base32 characters of the derived commitment id (25 bits) as a
    zero-padded decimal (`ids.spoken_reference`; e.g. `c_exyqr…` -> `04973073`). Nothing new is stored,
    so existing bookings have codes too.
  - `ids.reference_prefix` turns a code back into the id prefix. Only 8 digits with optional
    separators count (`0497 3073`, `0497-3073`); a full reference is never read as a code.
  - `CommitmentStore.find_by_reference_prefix` looks the prefix up on the base table's own keys
    (`pk = BIZ#<business> AND begins_with(sk, C#<prefix>)`, strongly consistent): no index, no extra
    write, no infra change.
  - A code matches only the caller's bookings at that business, in `confirmed`/`approved` (the
    statuses that say it) or `cancelled` (so a retry replays); a live booking wins over a cancelled
    one. No match, or two matches, falls through to the exact lookup and its usual "I couldn't find a
    booking with that reference."
  - The code is a bearer capability like the reference, so the anonymous replay paths render
    `with_code=False` and `_redacted_replay` also drops the card line. Owner-approved results (the task
    result and `get_task`) keep it: only the task id holder sees those.
  - **Security note.** Without auth, a guessed code cancels a booking with probability
    (live bookings at that business) / 33,554,432 per call, against 2^-128 for the full reference
    (which still works). The cancel is still read back and confirmed first. Follow-up, not in this
    fix: ask for the booking's phone last-4 when it has one, or rate-limit `cancel_booking`.
- **ask_owner.**
  1. `OwnerService.plan_question`.
  2. `tasks.ensure_task(task_id=ids.task_id(key))`.
  3. `commit` (kind `owner_question`, status `pending_owner`).
  4. `tasks.adopt`.
  5. The sync fallback result carries `task_id`.
  6. No confirmation: the customer already accepted the hand-off offer.
- **get_task.**
  1. `task_manager.get(task_id, principal)`. Unknown, expired or foreign tasks all get `ToolError`
     "Unknown or expired task".
  2. `commitments.get_by_task(task_id)` adds the commitment view.
  3. `result = record.result.structuredContent` once completed.
  4. `speak` is the commitment's status line, or the task's `statusMessage`.
  5. Read-only: a customer reply that is on the task but not yet stored on the commitment is rendered
     as folded (`owner.with_customer_answers`, without a write), so get_task never repeats a question
     the customer already answered.
  6. While the owner's question is open (`input_required`) the card offers **Answer**: a
     `reply_to_owner` action whose `input` (`CardInput{key: "answer", label, max_length}`) tells the
     card to collect the text first and send it as `arguments.answer`.
- **reply_to_owner.** The sync-fallback twin of `tasks/update` for the owner's `request_info` question.
  1. Same task lookup as get_task; an empty answer, or a task with no open question, is a `ToolError`.
  2. `task_manager.submit_input` with the answer for every open input request (checked against each
     form's schema).
  3. `OwnerService.absorb_customer_answers` stores the reply on the commitment at once (see §9).
  4. The result is get_task's, with "Thanks, I've passed that on to Rosa."; if the owner decided in
     the meantime, that outcome is shown instead.

## 7. MRTR confirmation (every transactional tool)

**Verified pattern** (*probe*, mcp 2.2.0, both eras): each transactional tool declares a resolver
chain:

```python
async def x_plan(<tool args by name>, ctx) -> CommitPlan                     # pure plan from args
async def x_intake(plan=Resolve(x_plan), ctx) -> Elicit | AcceptedElicitation  # NO_INTAKE if nothing missing
async def x_final_plan(<tool args>, intake: Annotated[ElicitationResult[BaseModel], Resolve(x_intake)], ctx) -> CommitPlan
async def x_confirmation(plan=Resolve(x_final_plan), ctx) -> Elicit[ConfirmCommit] | DeclinedElicitation
tool(..., plan: Annotated[CommitPlan, Resolve(x_final_plan)],
          confirmation: Annotated[ElicitationResult[ConfirmCommit], Resolve(x_confirmation)])
```

**Rules:**
- **Use the union form `ElicitationResult[...]`.**
  - `ElicitationResult[X] | None` silently switches to the unwrapped form. The *probe* showed that a
    decline then aborts the tool with `is_error`.
  - To skip a question, return a plain `AcceptedElicitation` or `DeclinedElicitation` value (*probe*).
- **Rounds on 2026-07-28:**
  - when intake fields are missing: call → intake form → confirmation → body, which is 3 `tools/call`
    rounds, well under the client's cap of 10;
  - otherwise: call → confirmation → body, which is 2 rounds.
- **Legacy clients** get the same questions as synchronous `elicitation/create`.
- **Questions must be deterministic.**
  - The intake model is `create_model` over the missing `IntakeFieldSpec`s. It has a stable name and
    flat types: `str`, `int`, `float`, `bool`, `date`, `Literal` and `list[Literal]`.
  - The confirmation message is built only from the plan: no timestamps and no remaining capacity.
    Otherwise the SDK discards the answer and asks again.
- **Resolvers are side-effect free**, because they re-run every round. The only write is in the tool
  body, after `confirmed(confirmation)`.

**The confirmation message** (`tools.confirm.read_back_message`) is built from the module's
`confirmation`:
1. Each `read_back` key (intake key or built-in) is spoken from the plan, as a phrase ("zip code
   80211", "weekday morning", the customer's own description), never as a quoted form label.
2. When the owner must decide, the first sentence of the matched approval rule's `reason_spoken`.
3. The sentences of each `say_before_confirm` knowledge entry's `answer_spoken` that apply to this
   request (a sentence about the other side of an approval threshold, "Orders under 25 servings...",
   is skipped for a 30-serving order), verbatim, while the message stays within
   `READ_BACK_BUDGET` (360 characters; `ask_policy` reads any policy in full).
4. The question comes last:
   - "Shall I send it to Rosa?" when `plan.approval.required` (using the approver's spoken name);
   - otherwise "Shall I book it?" or "Shall I place the order?".
5. Profile text reaches the customer here outside any tool result, so the L8 canary check runs here
   (`guarded_elicit`) on the message and on the form exactly as sent: every string and property name
   of the rendered schema. On a hit a generic fallback is asked, and the fallback is checked the same
   way:
   - confirmation: "Shall I go ahead with this request? Shall I send it to Rosa?", then the bare
     "Shall I go ahead with this request?";
   - intake: a generic message, with the field keys as labels and the option values as option titles.
     If that still carries a canary (in a key or an option value), nothing is asked and the tool answers
     `needs_info`.

Example from the gold bakery profile: *"Custom cake, ten inch round, vanilla bean, lemon curd and
vanilla buttercream, for Sunday, October 18, 30 servings, $120. Orders of 25 servings or more need
Rosa's approval, so I'll send your request to her. For 25 servings or more, a 50 percent deposit is
due by card within 48 hours of Rosa's approval, and the rest at pickup. Shall I send it to Rosa?"*

**The tool body:**
1. If the plan has problems, return `unavailable` / `not_offered` / `needs_info` with up to 3
   `alternatives`.
2. If the customer did not confirm (decline, cancel or `confirm=false`), return `not_confirmed`.
   Nothing is written.
3. Otherwise commit (§8). A booking that needs no approval becomes `confirmed`; anything else becomes
   `pending_owner` and is turned into a Task (§9).

## 8. Idempotency, capacity and the atomic commit

**Idempotency key** (`domain.idempotency.idempotency_key`):

`"idem_" + sha256(JCS({business_id, module_id, operation, principal, payload}))`

- `payload` is the *normalised* request: item ref, choice ids, slot start in UTC, day, party size,
  visit window, normalised intake values, and question text.
- Two retries of one request therefore hash identically, on any replica.

**Derived ids** (`ids.IdFactory`, HMAC-SHA256 under `SHOPFRONT_ID_KEY`):
- `commitment_id = c_<HMAC("c"|key)>`
- `task_id = t_<HMAC("t"|key)>`

These ids are deterministic, which is what makes conditional puts deduplicate, and unguessable, since a
task id is a bearer capability.

**How long a request dedupes:**
- A *live* commitment (open, `confirmed` or `approved`) dedupes for its whole life. Its id is derived
  from the key, and both stores refuse a second commitment with that id (in DynamoDB, the commitment
  Put's `attribute_not_exists(pk)`). The `IDEM#<key>` marker's TTL (`IDEMPOTENCY_TTL_S`) only bounds the
  marker row. Nothing relies on it for semantics.
- A *settled* commitment is never replayed. `SETTLED_STATUSES` holds every status with no outgoing
  transition: `RELEASES_HOLDS` plus `quoted` and `answered`. An identical request is a new attempt
  under `<key>~<n>` (`generation_key`), with its own commitment and task ids; retries of that attempt
  dedupe again.
- After `MAX_GENERATIONS` (32) settled attempts, the request is refused (`too_many_attempts`, spoken as
  unavailable). It is never answered with an old outcome.

**Who counts as the same customer:** the principal keeps authenticated customers apart. Without auth
every caller is `anonymous`, so the payload itself must tell customers apart:
- Every transactional request carries a phone number or email address. `intake.missing_contact` asks
  for one even when each is optional on its own, as on Kettlewick, and loader invariant L11 makes prod
  refuse a booking, custom order or quote module with no such field.
- Owner questions mix a per-call nonce into the key. So does a module that a lenient (dev) load accepted
  without any contact field. A retry is then a new request, which is the price of never merging
  strangers.
- An anonymous replay is redacted anyway (see `Duplicate` below).

**One atomic unit** (`CommitmentStore.commit`; in DynamoDB, one `TransactWriteItems`):
1. Put `IDEM#<key>`, if absent.
2. Put the commitment, if absent.
3. For each capacity hold, `ADD used :units` under the condition `used + units ≤ limit`.

**Outcomes:**

| Outcome | Meaning |
|---|---|
| `Committed` | The new commitment was written. |
| `Duplicate(existing)` | A retry. The tool returns the stored commitment with `replayed=true`, and re-adopts its task for an authenticated caller. An anonymous duplicate (sequential, or concurrent with the first request) is neither adopted nor given the task id, reference or booking code (`_redacted_replay`); facts fixed in the commitment (an owner's note, a price no longer published) cite it by `observability.id_digest` instead of its id. |
| `CapacityExhausted(key)` | A concurrent customer took the last unit. The tool cancels the just-created task and returns `unavailable` with alternatives. |

**Capacity model** (`domain.availability`; holds are taken at commit time, including while
`pending_owner`):

| Mode | Key | Limit |
|---|---|---|
| Interval booking | one `CELL#<utc start>#<resource>` per covered step cell | 1 per resource, or `capacity_per_slot` |
| Fixed sessions | `SESSION#<id>#<date>`, with `units = party_size` | session capacity |
| Per day | `DAY#<date>` | `capacity_per_day` |

- A 90-minute colour on a 15-minute grid takes 6 cells atomically, so overlapping bookings are
  impossible, even across replicas.
- Holds are released in the same atomic update as the transition into `declined`, `cancelled`,
  `expired`, `counter_offer` or `suggested_time` (`RELEASES_HOLDS`). `holds_released` guards against
  releasing twice.

**Race analysis:**

| Race | Result |
|---|---|
| Same request twice, on two replicas at once | Exactly one transaction wins. The other sees `Duplicate`; without auth it gets the redacted replay (no task handle, task id or reference). |
| Two customers, last cake of the day | Exactly one passes the counter condition. |
| Crash after the commit, before the response | The retry replays. |
| Crash between `ensure_task` and the commit | The working task is orphaned until its TTL. It is harmless: there is no commitment, and the next retry reuses the same task id (its TTL refreshed, as below). |
| Commit loses a capacity race after `ensure_task` | Result `unavailable` with alternatives. The task is left `working` and unadopted (never cancelled: that would poison the retry). An identical retry reuses it, and `ensure_task(refresh_ttl=True)` extends its TTL so it again outlives the retry's own commitment by the slack (a retry long after the lost race otherwise inherited a task that died before the owner's deadline). A terminal derived task is replaced by a fresh random id. If the retry instead hits `Duplicate` (an identical request committed first), an anonymous caller gets the redacted replay and is never adopted. |

## 9. Owner approval (Tasks) and the owner API

**When a Task is created:**
- every `quote_request` and every `ask_owner`;
- a `custom_order` or `booking` whose `approval` block requires it. `mode: always`, or a matching
  rule, as evaluated by `domain.approval_rules`.

**Rule built-ins.** These are the union of the schema's two lists: `item`, `choices` (full choice refs
such as `menu/custom-cake/size/10in-round`, as used by the gold bakery profile), `when`, `price` /
`order_total_cents`, `party_size`, `visit_window` and `lead_minutes`.

**Sequence (tool body):**

```
record = await deps.tasks.ensure_task(task_id=deps.ids.task_id(plan.idempotency_key),
             status_message="Waiting for Rosa. She usually answers within 2 hours.",
             ttl_ms=owner_deadline - now + slack, labels={"business_id": ...},  # deadline: expiry, or the keep_waiting horizon
             refresh_ttl=True)   # a reused task lives ttl_ms from now, never less
outcome = await service.commit(profile, plan, task_id=record.task_id)   # notifies the owner if new
deps.tasks.adopt(record)        # True  → interceptor replaces the result with the task handle
return commitment_result(...)   # False → this sync result (status pending_owner, task_id) is sent
```

**`mcp_tasks.TasksExtension`** (ported from the verified prototype, with the verifier's `MCPError` fix
and three additive APIs):

| API | Behaviour |
|---|---|
| `ensure_task(task_id=...)` | Idempotent get-or-create with a caller-chosen id. Works for every caller, so the synchronous fallback can still hand out a pollable `task_id`. `refresh_ttl=True` extends a reused active task (in the same CAS) so it lives at least `ttl_ms` from now; TTLs are never shortened and terminal tasks are returned unchanged. |
| `adopt(record)` | Answers the current call with the handle, but only for 2026-07-28 clients that advertised `io.modelcontextprotocol/tasks`. Never for anyone else: legacy wires sieve unknown result types, and a client without the extension cannot parse a handle. |
| `current_call()` | Returns the call information. |

The original `defer()` is kept as `ensure_task` + `adopt`.

**Owner decision** (`OwnerService.decide`, called by the owner REST API):
1. Check tenant ownership.
2. Check the decision is allowed: it must be in `approval.decisions`, or be `answer` for questions.
3. Validate `response_fields` (money is converted to cents).
4. CAS-transition the commitment, releasing holds if needed.
5. Drive the task:
   - **Most decisions** (approve, decline, counter_offer, quote, suggest_time, answer):
     `TaskManager.complete(task_id, CommitmentRenderer.render(profile, commitment))`.
   - **`request_info`:** `TaskManager.require_input(task_id, {"owner_question_<n>": ElicitRequest(form)})`.
     - The task becomes `input_required`.
     - The client answers through `tasks/update` (and `TasksClientExtension` does this automatically),
       or, without the tasks extension, through the `reply_to_owner` tool.
     - Either way the reply is folded into the commitment at once (`OwnerService.absorb_customer_answers`:
       `reply_to_owner` calls it, and the tasks extension's input hook `on_task_input` does for
       `tasks/update`): `customer_input` gets the reply, the status goes back to `pending_owner` and
       `decision.answered_at` is set. The inbox, `decide` and expiry fold it again as a fallback;
       get_task renders it without writing.
     - Only reply keys not yet in `customer_input` count, so an earlier round's reply never answers
       a newer question.
     - Use a fresh key for each question: the client skips keys it has already answered.
6. A task that is already terminal (the customer cancelled it) gives `InvalidState` (HTTP 409).

**Customer cancel** (`tasks/cancel`, which `TasksClientExtension` also sends when its caller abandons a call).
- The tasks extension's cancel hook, `OwnerService.on_task_cancelled`, runs after the task is `cancelled`.
  It withdraws the commitment: the status becomes `cancelled`, the holds are released at once and the
  request leaves the owner inbox.
- The hook finds the commitment through the task's server-side labels (`business_id`, `commitment_id`,
  never sent to clients). Only tasks without those labels use the eventually consistent task index.
  Every write is a CAS against a primary-key (consistent) read, retried if another writer got in first.
- An `approved` commitment is withdrawn too. A cancel can land between step 4 and step 5 above; a
  cancelled task proves the approval was never delivered, because a delivered decision completes the
  task and `tasks/cancel` is then refused.
- If completing the task in step 5 fails because the customer cancelled it, `decide` withdraws the
  commitment itself (covering a hook that never ran) and returns `InvalidState` (HTTP 409).
- The inbox closes any commitment whose task was cancelled, as a last resort.

**Renderer** (`tools.render`). It builds a `CommitmentResult` serialised exactly like an SDK tool result,
so `Client.call_tool`'s re-validation against `outputSchema` passes. The owner's free text goes through
`guard.sanitize_speak` and is cited as `urn:shopfront:owner-answer:<commitment_id>`.

**Expiry.**
- `app.background.run_sweeper` calls `OwnerService.expire_due()` every `SHOPFRONT_SWEEP_INTERVAL_S`
  seconds.
- The owner_approval module's `on_expiry` decides what happens:
  - `notify_customer` completes the task as `expired`;
  - `auto_decline` declines it;
  - `keep_waiting` keeps the request open until its service time (a booking's slot start, the end of a
    custom order's day in the business timezone), or at most `KEEP_WAITING_MAX_DAYS` after it was made
    when that comes first or there is no service date (quote requests, owner questions). After that it
    behaves like `notify_customer`. The sweep first extends the task's TTL to cover that horizon plus
    the slack (never shortening it, always finite), and only then moves `expires_at` to the horizon. If
    extending the task fails, `expires_at` stays due and the next sweep retries. A request whose task
    the customer already cancelled is withdrawn instead (holds released).
- The task's TTL at creation already covers the keep_waiting horizon (`owner_deadline` plus the
  slack), so the task survives even if no sweep runs. A decided request's task keeps a finite TTL and is
  garbage-collected.
- Whatever the policy, a booking or custom order whose service time has passed cannot be approved,
  counter-offered or asked about (`InvalidState`, HTTP 409). It can still be declined or answered with
  another time, and the inbox leaves those decisions out.
- A customer reply nobody folded yet is folded first, so it is kept. An expired request says the
  question went unanswered only while `decision.answered_at` is unset; once the customer replied it
  says the owner has not answered yet.
- It is safe on several replicas, because every expiry is a CAS.

**Owner REST API** (`app.owner_api`, JSON, for the phone page):

| Route | Purpose |
|---|---|
| `GET /owner/{biz}/inbox` | Pending requests, with allowed decisions and response fields. |
| `GET /owner/{biz}/requests/{id}` | One request. |
| `POST /owner/{biz}/requests/{id}/decision` | Body `{decision, values, note, question}`. Returns 400, 401, 404 or 409 as appropriate. |
| `POST /owner/{biz}/links` | Mints an inbox link. Admin bearer token; disabled in prod without a real token. |

**Owner tokens** (`app.links.OwnerLinkSigner`):
- **Format:** `b64url(claims).b64url(HMAC-SHA256)`, with claims `{v, biz, scope: inbox|decide, cid, exp,
  kid}`.
- **Keys** come from `SHOPFRONT_OWNER_LINK_KEYS`: the first signs, all verify, so keys can rotate.
- **Checks:** the HMAC is compared with `hmac.compare_digest`, and the expiry is checked against the
  injected clock.
- **Scope:** an `inbox` token authorises its whole business; a `decide` token authorises exactly one
  commitment.
- **Errors** are generic 401s.

**Notifier** (`app.notifier.OwnerWebNotifier`, channel `owner_web`). There is no SMS. It records a signed
decision link in a bounded outbox (for the demo) and logs it without PII. The phone page polls the
inbox.

**Clients without the tasks extension** call `get_task(task_id)` until `task_status` is terminal. The
`TaskStatusResult.result` is the same `CommitmentResult` the Task would have delivered. When get_task
reports `input_required` (the owner asked a question), they pass the customer's answer to
`reply_to_owner(task_id, answer)`; the request then goes back to waiting for the owner. A tasks-capable
client that cannot show a free-text question can do the same: with
`TasksClientExtension(cancel_on_unanswerable=False)` it raises `TaskInputUnanswerable` (carrying
`task_id`) instead of cancelling the task.

## 10. `packages/mcp-tasks` (reusable)

- **Exports:** `TasksExtension`, `TaskManager`, `TaskRecord`, `TaskStore`, `InMemoryTaskStore`,
  `DuplicateTask`, `principal_of`, `client_opted_in`, `TASKS_EXTENSION_ID`, plus
  `mcp_tasks.client.TasksClientExtension`, `get_task`, `update_task`, `cancel_task`, `list_tasks`,
  `answer_input_requests`, `TaskInputUnanswerable` and `mcp_tasks.dynamodb.DynamoTaskStore`.
- **Hooks:** `TasksExtension(on_cancel=..., on_input=...)` run after an accepted `tasks/cancel` or
  `tasks/update` (shielded; a failure is logged and never undoes the cancel or the answers).
- **Client input rounds:** an input request the client cannot answer (no callback, or the callback
  replies with an error) raises `TaskInputUnanswerable(task_id, key, input_requests)`. By default the
  task is cancelled first; `cancel_on_unanswerable=False` leaves it `input_required` for the
  application to answer another way. A callback that raises still cancels the task.
- **Wire contract** (SEP-2663, as implemented by strands 1.57.1 and verified against its strict models):
  - **`tools/call` handle:** `{resultType:"task", taskId, status, statusMessage?, createdAt,
    lastUpdatedAt, ttlMs (required, nullable), pollIntervalMs?}`.
  - **`tasks/get`:** the same fields with `resultType:"complete"`, plus exactly one of `inputRequests`
    (input_required), `result` (completed) or `error` (failed).
  - **`tasks/update`, `tasks/cancel`:** empty acknowledgements.
  - **`tasks/list`:** a convenience, served only to authenticated principals
    (`TasksExtension(list_anonymous=False)` is the default; anonymous callers get `-32602`). Without
    auth every caller shares `anonymous`, so a listing would hand out every customer's task ids.
    Shopfront also wires `serve_list=False` (`app.wiring`) until MCP auth exists, so the method is not
    bound at all (`-32601`). With auth: `serve_list=True`, keeping `list_anonymous=False`.
  - **Errors:** unknown, expired or foreign tasks, and invalid transitions, all give `-32602`.
- **Changes from the prototype:**
  - It is split into modules.
  - `TaskManager.create(task_id=, labels=)` is idempotent.
  - `ensure_task`, `adopt` and `current_call` are added.
  - The interceptor now always records call information.
  - `TaskError` no longer imports the private `mcp_types._types.MCPModel`.
  - `DynamoTaskStore` raises `DuplicateTask` on conditional failures, and its key attribute and prefix
    are configurable.
- **Tests:** 7 in-process tests pass now (transparent resolve, input_required, list, cancel, both
  degradation paths, idempotent ids, sync fallback). The tasks work package adds the remaining coverage.
- **DynamoDB:** its own table, `shopfront-tasks` (hash key `taskId`, GSI `byPrincipal`, TTL
  `expiresAt`). It keeps the package independent of Shopfront's single-table design.

## 11. Availability engine (`domain.availability`)

**Inputs** (per module): `availability.weekly`, `closures`, `lead_time_minutes`,
`max_advance_minutes`, `capacity_per_slot`, `capacity_per_day`, `slotting` (interval `step_minutes` or
fixed `sessions`), and `services[].resource_ids`, `party_size` and `buffer_minutes`.

**Defaults:**
- **Hours:** with no `weekly`, the engine uses the hours of the fulfilment, session or primary
  storefront location.
- **Closures** are the module's plus the location's.

**Booking:**
- **Interval mode.**
  - Candidate starts fall every `step_minutes` from each opening time.
  - A booking occupies `[start, start + duration + buffer)` and must end by closing time.
  - Two lines on one day form a split shift.
- **Fixed mode.** Session days at the session start, with seat capacity.

**Day-granular modules** (custom_order, quote):
- A day is valid if it is open and not closed.
- Lead time is measured to the day's first opening time.
- `max_advance` is respected.

**Time:**
- All arithmetic is in `ZoneInfo(business.timezone)` (tzdata pinned).
- A start that does not exist (spring forward) is skipped.
- An ambiguous one (fall back) takes `fold=0`.
- Capacity keys use UTC starts.

**Resources:**
- Only the eligible resources can serve a service.
- An optional resource attribute `works_days` limits the days each one works. This is a convention the
  gold salon profile uses.
- Without a preference, the first free resource is assigned.

**API:** `booking_candidates` / `check_booking` / `open_booking_slots`, and `day_candidates` /
`check_day` / `open_days`.
- The `open_*` methods subtract usage with one `CapacityStore.usage` batch read. That read is
  advisory; the authoritative check is the conditional commit.
- Problems carry up to 3 alternatives.

## 12. Storage

| Port | Memory (tests, dev) | DynamoDB (AWS) |
|---|---|---|
| `ProfileStore` | `InMemoryProfileStore` (seeded by `load_profile_dir`), plus `CachingProfileStore` | `DynamoProfileStore`, wrapped in `CachingProfileStore(ttl 30 s)` |
| `TaskStore` | `InMemoryTaskStore` | `DynamoTaskStore` (table `shopfront-tasks`) |
| `CommitmentStore`, `CapacityStore`, `IdempotencyStore` | `MemoryState` (one lock, shared) | `Dynamo*Store` (single table `shopfront`) |

**Single table `shopfront`** (`pk`, `sk` strings; TTL attribute `expires_at`; on-demand; region
ap-southeast-2):

```
PROFILE#<biz>          CURRENT | REV#<n>          profile doc, revision (CAS), status, sha256
DIRECTORY              BUSINESS#<biz>             DirectoryEntry JSON (only while live)
BIZ#<biz>              C#<commitment_id>          commitment doc, status, version
                       begins_with(sk, C#<prefix>)                    spoken booking code (find_by_reference_prefix)
                       gsi1: BIZ#<biz>#S#<status> / <created_at>#<id>   owner inbox by status
                       gsi2: TASK#<task_id> / C                       get_by_task
                       gsi3: OPEN#<expiry date> / <expires_at>        expiry sweep (only while open)
IDEM#<key>             IDEM                       commitment_id, expires_at
CAP#<counter key>      CAP                        used, limit, expires_at (day after the slot)
```

**Rules for every DynamoDB store:**
- Every boto3 client is injected. Calls are synchronous and run in `anyio.to_thread`.
- Tests use botocore `Stubber`, or the in-memory fakes. **No test calls AWS.**
- Infra (CDK) creates both tables. That is outside this server's scope.

## 13. Profile loading

```
raw JSON ─▶ schema.validate_schema (Draft 2020-12 + formats)
         ─▶ invariants.run_all (L1..L11, severity policy)
         ─▶ Profile(raw, sha256=canonical.profile_sha256(raw))   ─▶ LoadedProfile(profile, report)
```

**Policy:**
- **strict** (prod, certifier): every violation is an error.
- **lenient** (dev, tests): L3, L7 and L11 are downgraded to warnings, so illustrative hashes and quotes
  still load (and a module without a contact field still loads; §8 covers how it is keyed).
- **L3 without a snapshot** is always a warning with `skipped=true`, never a silent pass.

**L3 is pluggable through `SnapshotProvider`:**
- `DirectorySnapshotProvider` reads `<sha>.txt|.html` and verifies the hash.
- `NullSnapshotProvider` is the fallback.
- The comparison is NFC plus whitespace-collapsed substring matching.

**L8** is checked twice:
- by the loader: no canary appears in any spoken field of the profile;
- at output time, on every path to the customer (§5): tool results (`tools.guard`), elicitations
  (`confirm.guarded_elicit`), the owner's relayed questions (`OwnerService.decide`) and task objects
  (`TaskViewGuard`).

**`ProfileStore.get_live` is the tenant gate.** `put` is a compare-and-swap on `revision` and keeps the
directory in sync.

**At startup, `load_profile_dir` loads `profiles/*.json`.** It falls back to
`schema/example-profile.json`. Invalid files are logged with their report and skipped.

**Fact refs** (`profile.factref`) are parsed with the schema's pattern and resolved by stable ids:
- module entity lists are searched in this order: items, entries, resources, `slotting.sessions`,
  visit_windows, categories;
- for an item, the next two segments are an option group id and a choice id;
- provenance follows L5.

`FactRef.overlaps` drives L6 withholding: a prefix field path, or a nested entity.

## 14. MCP Apps cards (`tools.cards`)

**Four static `ui://` resources:** `business-card`, `items-card`, `commitment-card` and `task-card`.
- They are self-contained HTML: no external fetches, so the default restrictive CSP works.
- Each renders `structuredContent.card` through `textContent` from the ext-apps
  `ui/notifications/tool-result` message.
- They are registered on `Apps` **before** `MCPServer(...)`.
- Tenant data arrives only through tool results; the HTML is shared by every tenant.
- Card actions send `tools/call` through the host. An action with an `input` (the task card's
  **Answer**) first shows a labelled textarea (`maxlength` from the action, send disabled while
  blank) and calls the tool with `{...arguments, [input.key]: text}`.

**Hosts without Apps** use `speak`, the card JSON, and the text content. Everything degrades to text.
`client_supports_apps(ctx)` may be used to drop the card on voice-only clients, but the default is to
always include it, because it is small.

## 15. Configuration (env, prefix `SHOPFRONT_`, `config.Settings`)

| Variable | Default (dev) | Notes |
|---|---|---|
| `ENV` | `dev` | `prod` requires real secrets, DynamoDB storage, strict invariants, a public `ALLOWED_HOSTS` entry clients on 443 match, a public `https://` `PUBLIC_BASE_URL`, `CORS_ORIGINS` admitted by `ALLOWED_ORIGINS` and an admin token of at least 32 bytes (or none). |
| `HOST`, `PORT` | `127.0.0.1` (dev/test), `0.0.0.0` (prod; the image sets it), `8000` | The MCP path is fixed at `/mcp`. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Used in owner links (bearer tokens). Prod: public `https://`. |
| `ALLOWED_HOSTS` | `localhost:*,127.0.0.1:*` | Transport Host allow-list. Exact names only: no scheme, no path, no wildcard names. List the bare name: clients on 443 send no port, and `name:*` needs an explicit one. |
| `ALLOWED_ORIGINS` | `http://localhost:*,http://127.0.0.1:*` | Transport Origin allow-list (`scheme://host[:port]`). |
| `CORS_ORIGINS` | `http://localhost:5173,…` | The reference host and the owner page. Must pass `ALLOWED_ORIGINS` (refused in prod, logged elsewhere). |
| `REQUEST_STATE_KEYS` | DEV-ONLY key | Each ≥ 32 bytes. Shared across replicas. |
| `REQUEST_STATE_TTL_S` | 900 | How long a customer may take to answer a confirmation. |
| `OWNER_LINK_KEYS` | DEV-ONLY key | HMAC keys for owner links; rotatable. |
| `ID_KEY` | DEV-ONLY key | Derives commitment and task ids. |
| `ADMIN_TOKEN` | DEV-ONLY | For the link-minting endpoint. The DEV token is off in prod and in anything that looks deployed; a real one there needs 32 bytes. |
| `STORAGE` | `memory` | `memory` or `dynamodb`. |
| `DYNAMODB_TABLE`, `DYNAMODB_TASKS_TABLE` | `shopfront`, `shopfront-tasks` | |
| `AWS_REGION`, `DYNAMODB_ENDPOINT_URL` | `ap-southeast-2`, unset | |
| `PROFILE_DIRS`, `FALLBACK_PROFILE` | `profiles/`, `schema/example-profile.json` | |
| `PROFILE_SCHEMA_PATH` | `schema/business-profile.schema.json` | Copied into the container image. |
| `PROFILE_SNAPSHOT_DIR` | unset | Enables L3 checking. |
| `STRICT_INVARIANTS` | unset | Unset: strict in prod or any deployment that looks real, lenient for local dev. Prod refuses `false`. |
| `TASK_POLL_INTERVAL_MS`, `TASK_DEFAULT_TTL_MS` | 3000, 48 h | |
| `KEEP_WAITING_MAX_DAYS` | 30 | The longest an `on_expiry: keep_waiting` request waits for the owner. |
| `IDEMPOTENCY_TTL_S`, `OWNER_LINK_TTL_S`, `SWEEP_INTERVAL_S` | 24 h, 7 d, 60 | |
| `OTEL_SERVICE_NAME`, `LOG_LEVEL` | `shopfront-mcp`, `INFO` | |

Secrets live in Secrets Manager and are injected as env at deploy time. None are ever in code or in a
profile.

The settings guard fails closed (`Settings.problems`). A deployment *looks real* when storage is DynamoDB
or any of `PUBLIC_BASE_URL`, `ALLOWED_HOSTS`, `ALLOWED_ORIGINS`, `CORS_ORIGINS` names a non-local host
(dotted, an IP address or `*`; loopback and single-label names such as `testserver` stay local). It then
refuses DEV-ONLY secrets and short admin tokens and checks invariants strictly, even if `ENV` was
forgotten. `ALLOWED_HOSTS` / `ALLOWED_ORIGINS` entries the transport can never match are refused in every
env. All problems are reported together; `python -m shopfront_server` prints one line per problem and
exits with code 2, and validation errors never echo the input (`hide_input_in_errors`).

## 16. Observability

**The SDK already emits a SERVER span for each MCP request**, named `tools/call <tool>` with `gen_ai.*`
attributes. Shopfront adds INTERNAL child spans named `shopfront.<area>.<op>` through
`observability.span`, `aspan` and `traced`.

**Span attributes:**
- `shopfront.business_id`, `module_id`, `tool`, `outcome` (ok, handoff, withheld, pending_owner,
  replayed…), `commitment_id`, `task_id`, `idempotent_replay` and `fact_count`.
- **Never PII.** Use `observability.redact` for logs.
- **Never a raw capability id.** Commitment and task ids are recorded as `observability.id_digest(id)`.
  `tools.context.tool_span` and `finish` both apply this, so the raw `task_id` argument of `get_task`
  and `reply_to_owner` never reaches a span.

**Trace ids reach the certifier.** `Grounded.trace_id` carries `current_trace_id()`, so a certification
failure links to its trace.

**There is no exporter config here.** ADOT/AgentCore Observability configures the provider at deploy
time. Tests install an `InMemorySpanExporter` from the `opentelemetry-sdk` dev dependency.

## 17. Errors

| Where | Raised | The caller sees |
|---|---|---|
| Tool, unknown or non-live business | `UnknownBusiness` → `ToolError("Unknown business …")` | `is_error=True` with a short message |
| Tool, a business problem (closed day, full, unknown item) | Not raised. Returned as a grounded `CommitmentResult` / hand-off | A normal result |
| Tool, an unexpected bug | Any exception | `is_error=True` "Error executing tool X"; the traceback is in the server log |
| Resolver, invalid business | `ToolError` | Aborts before any question is asked |
| Client without form elicitation, on a commit tool | SDK `-32021` | JSON-RPC error (documented) |
| `tasks/*`, unknown, expired or foreign task | `-32602` | JSON-RPC error |
| Owner API | `AuthError` 401, `NotFound` 404, `InvalidDecision` 400, `InvalidState`/`ConcurrencyConflict` 409 | JSON `{error, code}` |

## 18. Test strategy

- **Runner.** `cd server && uv run pytest -q`.
  - Tests run with anyio (`pytestmark = pytest.mark.anyio`) and `--import-mode=importlib`.
  - `tests/` is on `pythonpath`; shared helpers are in `tests/support.py`.
  - mcp-tasks: `cd packages/mcp-tasks && ../../server/.venv/bin/python -m pytest -q`.
- **Fixtures** (`tests/conftest.py`): `raw_profiles` → `live_raw_profiles` (`support.make_live` promotes
  the gold profiles to live, with a passed run bound to their sha) → `clock` (`FixedClock`, Wed
  2026-10-14 16:00Z) → `settings` → `profiles` → `deps`/`signer` → `mcp_server` → clients:
  - `client`: 2026-07-28, with the tasks and Apps extensions and an `ElicitationScript`;
  - `plain_client`: no tasks extension;
  - `legacy_client`;
  - `http_app`: TestClient;
  - `stubbed_dynamodb`.
- **Skeleton fixtures skip.** A fixture that hits a skeleton skips, naming the work package, so the
  suite always collects.
- **Per work package:** unit tests in `tests/<wp>/`.
  - pure functions (invariants, availability with DST cases, pricing, approval rules, speech,
    search);
  - stores (memory, plus DynamoDB with Stubber: transaction shapes and `CancellationReasons` mapping);
  - tools in-process (accept, decline and cancel elicitation; round counts; legacy client);
  - HTTP (421/403/CORS preflight, `/mcp` with no 307, owner API auth matrix).
- **Concurrency.** Fire N concurrent identical `book` calls and N different customers for the last
  slot with `anyio` task groups against the memory store. Assert exactly one commitment and that
  capacity is never exceeded.
- **Acceptance** (`tests/test_acceptance.py`): discovery, hand-off, decline-commits-nothing,
  big-cake-to-task-to-owner-approves, idempotent retry, and the `get_task` fallback.
- **Certifier compatibility.** Every tool result validates against its `outputSchema` (the client
  re-validates). `speak` passes `check_spoken`. Facts parse as fact refs.
- **No AWS, ever.** No network in tests.

## 19. Work packages

There are five packages, with disjoint file ownership (the full briefs are in the work-package
output).

| Package | Owns |
|---|---|
| **profile** | `profile/*`, `canonical.py`, `tests/profile/` |
| **tasks** | `packages/mcp-tasks/**` |
| **domain** | `domain/**`, `clock.py`, `errors.py`, `ids.py`, `tests/domain/` |
| **tools** | `tools/**`, `tests/tools/`, `tests/test_acceptance.py` |
| **app** | `app/**`, `config.py`, `deps.py`, `observability.py`, `__init__.py`, `__main__.py`, `tests/conftest.py`, `tests/support.py`, `tests/test_architecture.py`, `tests/app/` |

**Integration order:**
1. **profile** and **tasks** have no dependencies.
2. **domain** needs `Profile`, which it can fake in unit tests.
3. **tools** needs domain plans. It can stub services with `SimpleNamespace` until they land.
4. **app** wires everything, starting with links, security and the owner API.

## 20. Open decisions and risks

1. **Whether Alexa+ supports the tasks extension or MCP Apps is unknown.** The sync fallback
   (`pending_owner` + `get_task`) and text-first results are the primary contract.
2. **A client without form elicitation gets `-32021` on commit tools.** This is intended: "no commit
   without confirmation" is a certifier rule.
3. **Anonymous principal.** Without auth, the request details are all that tell customers apart (§8:
   every transactional request carries a phone or email, and owner questions a per-call nonce), and a
   task id is the only capability. Anonymous replays, including concurrent duplicates, are never
   adopted and are redacted, so repeating someone's request details never yields their task id.
   Enable OAuth (`authenticated_principal`) before real traffic.
4. **The SEP-2663 wire shapes come from strands, not from the spec text.** Interop has been verified,
   but the final SEP may change error codes and `tasks/list`.
5. **AgentCore header forwarding** (`Mcp-Method`, `Mcp-Name`) is unconfirmed. Fargate + ALB is the
   fallback (PLAN §0).
6. **The schema lists approval-rule built-ins in two different places.** The domain accepts their union
   (§9). Fix the schema text in the next profile revision.
