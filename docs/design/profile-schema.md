# Business Profile schema (v1.0)

- **Schema:** `schema/business-profile.schema.json` (JSON Schema 2020-12, `$id` `https://askonce.dev/schema/business-profile.schema.json`, 585 lines)
- **Example:** `schema/example-profile.json` (Thimbleberry Bakehouse, every module type, validates)
- **Drafts judged:** `schema/drafts/profile-{generic,grounding,voice-tools}.schema.json`

A Business Profile is the only thing that differs between two tenants of the AskOnce MCP server. The onboarding agent writes it from a website plus the owner's answers. The certifier checks replies against it, and the MCP tools read from it. This document covers three things:
- how the three drafts were scored;
- what the final schema took from each;
- how every section is meant to be used.

---

## 1. Verdict

**Base: the generic draft.** It wins by one point over voice-tools. The tie-break is the product rule in PLAN.md: *adding a vertical = profile template + certification cases, not code*.

- **Voice-tools fails that rule structurally.** It has a closed `vertical` enum and a closed `Slot` enum that hard-codes `servings` and `job_description`. It has fixed top-level `catalog`/`booking` sections, and food fields on every item.
- **The generic draft's weaknesses are all additive.** Floats, optional quotes, no spoken names and positional question targets can each be fixed by grafting, without restructuring.

### Scoring (1-10)

| Criterion | Generic | Grounding | Voice-tools |
|---|:-:|:-:|:-:|
| Genericity across verticals | **9** | 6 | 3 |
| Grounding / provenance for deterministic certification | 6 | **10** | 5 |
| Ease of reliable LLM extraction | 6 | 3 | **8** |
| Fit for MCP tools and voice answers | 7 | 5 | **9** |
| Simplicity | 5 | 3 | **7** |
| **Total (/50)** | **33** | 27 | 32 |

**Why these numbers.**

- **Generic.**
  - **Genericity 9.** No top-level field belongs to a vertical. Modules are keyed by id with a `type`, so a fitness studio can have two booking modules. Vertical words live in `vocabulary` as data. Location kinds cover a food truck or a home tutor. The point lost: allergens are free vocabulary, so templates drift. Its own example uses `dairy`/`egg`/`soy`, not the FDA names.
  - **Grounding 6.** Every fact-bearing object has `provenance` with source ids and a status. But the quote is optional and prices are floats (`7.5`). Durations are ISO strings. Questions target positional pointers (`/business/locations/0/hours`), which break on re-extraction.
  - **Extraction 6.** `attributes` values are an `anyOf` union (scalar *or* `{value, provenance}`), and the indirection through vocabulary adds room for shape errors.
  - **MCP/voice 7.** Module id works as the tool handle, `intake_fields` map to elicitation, `before_commit` is a constant, and approvals have a decisions list. Items and choices have no synonyms, and spoken text isn't filtered.
  - **Simplicity 5.** 1124 lines and three layers of base-schema composition.
- **Grounding.**
  - **Grounding 10.** It has a typed fact ledger, compared byte for byte under canonical JSON. Evidence carries a verbatim quote, the snapshot sha256 and a locator. Each fact has `owner_confirmed`. Contradictions keep per-claim evidence and a `value_path`. It defines a "statable" rule, keeps injection canaries, and binds certification runs to a revision and hash.
  - **Genericity 6.** The ledger is vertical-neutral, but modules are a closed one-of-each set. `custom_order` hard-requires a daily-capacity fact, there's a single address, and the allergen enum is EU-style (celery, lupin, mustard).
  - **Extraction 3.** The LLM must emit nine required fields per fact, invent dotted ids, keep module references consistent with ledger keys, and fill hashes and run ids it cannot know. The bakery example alone is 1351 lines.
  - **MCP/voice 5.** Claims fit tool results well, but there are no spoken names, and every read dereferences the ledger.
  - **Simplicity 3.** 15 required top-level sections and two levels of indirection.
- **Voice-tools.**
  - **MCP/voice 9.**
    - It has `spoken_name` plus `synonyms` everywhere, `x-read-by` tool tags, and a Dialog (slots, read-back, say-before-confirm).
    - Approval rules come with a spoken reason, and there are pronunciation hints.
    - Its spoken-text pattern rejects URLs and markup.
  - **Extraction 8.** It is flat and concrete, uses closed enums and integer hours, and its description holds explicit extractor rules. It loses points for float prices and a `capabilities` array that must agree with which sections are present.
  - **Grounding 5.** `src` is required everywhere and an allergen `declared` flag exists. But quotes are stored once per source rather than per fact, there's no per-fact owner confirmation, prices are floats, and there's no snapshot hash.
  - **Genericity 3.** See above.
  - **Simplicity 7.** Simple concepts; the file is long only because of its formatting.

All three draft examples validate against their own schema (re-checked with jsonschema 4.26).

---

## 2. What came from where

### Kept from the generic draft (base)
- **Modules map.** A `modules` map keyed by module id, with `type` dispatch through `if`/`then`. Each module is optional and independent. A business may run several of one type, and `x-*` module types are extensions.
- **Strict core.** Unknown fields fail validation (`additionalProperties: false` / `unevaluatedProperties: false`). The only open doors are `x-*` keys and `x-*` module types.
- **`vocabulary.attributes`.**
  - Each entry has a `claim_class`.
  - Safety attributes are forced to `absent_means: unknown`.
- **Flat `intake_fields`.** Each one maps 1:1 to an MCP form-elicitation property and a tool argument.
- **Location kinds.** `storefront | mobile | service_area | virtual`, plus `area`.
- **Booking.** `slotting` is `interval` (appointments) or `fixed` (classes), with `resources`.
- **Shared owner-approval base.** `approval` is shared by booking, custom_order and quote_request, including `decisions` and the owner's `response_fields`.
- **Schema-level rules.**
  - **R1:** a module that routes to the owner requires an owner_approval module.
  - **R2:** certified or live means no open blocking question.

### Grafted from the grounding draft
- **Money.** Integer `amount_cents` plus `currency`.
- **Evidence.**
  - Each item is `{url, quote}`, where the quote is verbatim.
  - Owner answers get a uniform URL: `urn:askonce:owner-answer:<question id>`.
- **`owner_confirmed: true` requires owner evidence.** It must include an owner URN, so nobody can claim a confirmation that never happened.
- **Stable addressing instead of positional pointers.** A `fact_ref` such as `menu/celebration-cake#allergens` or `custom-cakes#availability.capacity_per_day` is built from ids, so it survives re-extraction. This gives the ledger's stability without a separate ledger.
- **Sources.** Each source has a `content_sha256` snapshot. The quote-in-snapshot check (L3) is what catches extractor hallucination deterministically.
- **`injection_canaries`** on sources (the grounding draft's `quarantined_spans`).
- **Certification runs.** Each run is bound to `profile_revision` and `profile_sha256`. `certified_run` names the run the status rests on. Failures carry `check`, `fact_ref` and an OTel `trace_id`.
- **The idempotency guard.** `confirmation.idempotency: "required"`.
- **The "statable" rule and disputed-fact withholding** (L6).
- **`x-invariants`.** A numbered list of rules the loader enforces because JSON Schema can't.
- **Timestamps.** UTC-only timestamp pattern.

### Grafted from the voice-tools draft
- **Speakable names.** `spoken_name` (≤40 characters, no symbols) and `synonyms` on the business, items, choices, categories, resources, visit windows and the approver.
- **URL-free spoken text.** The `spoken` text type (≤240 characters) has a pattern that rejects URLs and markup. It doubles as an injection filter, so `"see https://…"` cannot be stored as something the assistant says.
- **FDA "big 9" allergen enum.** `milk, eggs, fish, shellfish, tree_nuts, peanuts, wheat, soybeans, sesame`, plus a dietary-tag enum. Gluten is only ever the claim `gluten_free`, never inferred from wheat.
- **Absent allergens = unknown.** The assistant says "I don't know. Want me to ask Rosa?"
- **`facility_may_contain`.** A kitchen-wide cross-contact statement.
- **Flat availability rules.** Weekly lines, capacity per slot and per day, lead time, max advance and closures, all in minutes.
- **Dialog becomes `confirmation`.** It carries `read_back` and `say_before_confirm`.
- **Flat approval rules** `{when, op, value, reason_spoken}`, with `mode: when_any_rule_matches`.
- **`visit_windows`** for quote requests.
- **Spoken owner details.** The owner_approval module has the approver's spoken name, expected response time, expiry and `answers_unknown_questions`.
- **Knowledge entries.** `question_variants`, `answer_spoken` and `answer_card`.
- **`x-read-by`** annotations naming the tools that read each module. Phone numbers are E.164.

### New in the synthesis (in none of the drafts)
- **The catalog is the single home of item facts.** Name, price, duration, options and allergens live on the catalog item. booking, custom_order and quote_request only reference items (`menu/celebration-cake`), so a price can never be stated in two places that disagree.
- **Safety facts can't exist unconfirmed.** `allergens`, `dietary` and `facility_may_contain` require `owner_confirmed: true`. An allergen list the site states but the owner hasn't confirmed lives only as a candidate in a `confirm` question. Until then the assistant says it doesn't know.
- **R1 is keyed on "has an `approval`".** It is not keyed on specific types, so it also covers a booking module with owner review, and `x-*` modules.
- **Module-level fallback `provenance`.** Facts such as visit windows that have no closer provenance are still covered.
- **One lifecycle field.** `certification.status: draft | failed | certified | live`; `find_business` returns only `live` profiles.

### Deliberately left out
- **The grounding draft's separate `facts` ledger and `TypedValue`.** Too expensive for the extractor. `fact_ref` gives the same stable addressing over the nested profile.
- **Per-fact metadata and extractor details.** Per-fact `kind`, `visibility` and `revision`, extractor metadata and character locators belong in the snapshot store and traces, not the profile.
- **The voice-tools draft's closed lists.** Its `vertical` enum, `Slot` enum and the `capabilities` array (which duplicates module presence).
- **`voice.pronunciations`.** A good candidate for an `x-voice` extension later.
- **The generic draft's attribute `{value, provenance}` union, `confidence` and `options_from`.**
- **ISO 8601 durations.** All durations are integer minutes.

---

## 3. Sections

### Top level
| Field | Notes |
|---|---|
| `schema_version` | `"1.0"` (const). |
| `profile_id` | Tenant id; `find_business` returns it and every business-scoped tool takes it. |
| `revision` | Bumped on every edit; a certification run binds to one revision. |
| `template` | `{id, version}` of the vertical template that seeded it (vocabulary, default modules, intake fields, certification cases). |
| `business`, `vocabulary`, `modules`, `sources`, `open_questions`, `certification` | See below. |
| `x-*` | The only way to add a top-level key. |

### `business` and `locations`
- **Identity and discovery.**
  - `name` is for cards; `spoken_name` and `synonyms` are for speech.
  - `summary_spoken` is one spoken sentence.
  - `categories` are **open** tokens (`bakery`, `hair_salon`, `plumber`): a new vertical adds a token, not schema.
- **Settings.** `locale`, `timezone` (IANA) and `currency` (ISO 4217) apply to every time and price in the profile.
- **Locations.** Each has an `id` and a `kind`:
  - `storefront` and `mobile` require an `address` (a food truck has one mobile location per recurring stop);
  - `service_area` requires an `area` (postal codes, localities or a radius);
  - `virtual` covers online tutoring.
- **Hours.** `hours` has `weekly` lines (`days`, `open`, `close` in 24h `HH:MM`, `24:00` allowed), `closures` and its own `provenance`. A day with no line is closed.

### `vocabulary`
Vertical words as data, such as `serves`, `stylist_level`, `subject` or `deposit_percent`. Each attribute has:
- a `type` and, for enums, its values;
- a `claim_class` (`price | safety | availability | policy | spec | descriptive`), which tells the certifier how strictly to ground statements about it;
- `absent_means`, which is forced to `unknown` for safety attributes.

Items, choices, resources and knowledge `facts` carry `attributes` keyed by these. Allergens and dietary tags are **not** vocabulary: they are regulated, so they are fixed enums in the core.

### `modules`
The map key is the module id: the tool-facing handle and the scope of fact refs (`business` and `locations` are reserved). `type` picks the definition.

| type | Required | Read by (`x-read-by`) | Purpose |
|---|---|---|---|
| `catalog` | `items` | get_catalog, find_item | Single home of every item's name, price, duration, options, attributes and safety facts. Optional `categories` and `facility_may_contain`. |
| `knowledge` | `entries` | ask_policy | Policies/FAQ: `question_variants`, `answer_spoken` (returned verbatim), `answer_card`, structured `facts` (so the certifier can check the numbers inside a policy), `applies_to`. |
| `booking` | `services`, `slotting`, `availability`, `intake_fields`, `confirmation` | check_availability, book, cancel_booking | `services` reference catalog items; `resources` (stylists, tables, instructors); `slotting` is `interval` (`step_minutes`) or `fixed` (`sessions` with day, start time and capacity). Optional `approval` for owner review. |
| `custom_order` | `items`, `availability`, `approval`, `intake_fields`, `confirmation` | check_availability, place_custom_order, get_task | Made-to-order goods. The item's option groups become the `choices` slot. |
| `quote_request` | `items`, `approval` (mode `always`, decisions include `quote`), `intake_fields`, `confirmation` | check_service_area, request_quote, get_task | Job priced per request. The owner's reply comes back as an MCP Task. The assistant never estimates a price. Optional `visit_windows`. |
| `owner_approval` | `approver.spoken_name`, `channel`, `expected_response_minutes`, `expires_after_minutes`, `on_expiry`, `provenance` | ask_owner, get_task | Where approvals and "ask the owner" Tasks go. Contact handles live in tenant config, never in the profile. `on_expiry` applies once `expires_after_minutes` pass: `notify_customer` expires the request, `auto_decline` declines it, and `keep_waiting` keeps it open until its service time (a booking's start, the end of an order's day), or at most `ASKONCE_KEEP_WAITING_MAX_DAYS` (30) after it was made, then expires it like `notify_customer`. |
| `x-*` | `type` | (ignored) | Extension; facts inside are never statable. |

**Pieces shared by transactional modules:**
- **`intake_fields`.** A flat input spec; see the mapping table below.
  - **A contact is mandatory (L11).** Booking, custom_order and quote_request modules must include at least one `phone` or `email` field. Each may be optional on its own, like Kettlewick's `phone` and `email`, but every request must carry a value for one of them. The server asks for the field that matches a chosen channel (a choice answered `email` asks for the email field, anything else for the phone field). Without auth, the contact is what keeps two customers' otherwise identical requests apart in the idempotency key. It is also how the owner reaches the customer.
- **`confirmation`.** `before_commit: true` and `idempotency: "required"` are constants, so no tenant can disable them. `read_back` lists intake keys or built-ins (`item, choices, when, price, party_size, visit_window`). `say_before_confirm` lists up to two knowledge entries, such as the deposit policy.
- **`availability`.** Honest, rule-based slots with no calendar sync: `weekly`, `closures`, `capacity_per_slot`, `capacity_per_day`, `lead_time_minutes`, `max_advance_minutes`, `provenance`.
- **`fulfillment`.**
  - `modes`: one or more of `pickup | delivery | on_site | at_customer | virtual`.
  - `location_ids`: pickup locations, or a service area.
  - `fee`: a price.
  - `provenance`.
- **`approval`.** `module` (an owner_approval id), `mode` (`always` or `when_any_rule_matches`), `rules`, `decisions` and `response_fields`.
  - **Rules** are flat: `{when: "servings", op: ">", value: 24, reason_spoken: "Cakes for more than 24 people need Rosa's okay…"}`.

**`field.type` → MCP form elicitation** (checked against `mcp_types/_v2025_11_25` in the installed SDK):

| field.type | Elicitation property | Note |
|---|---|---|
| `text`, `long_text` | `string` + `maxLength` | |
| `integer`, `number` | `integer` / `number` + `minimum`/`maximum` | |
| `boolean` | `boolean` | |
| `choice` | titled single-select enum | `options[].label` → titles |
| `multi_choice` | titled multi-select enum | |
| `date` | `string`, `format: date` | |
| `email` | `string`, `format: email` | must be `pii: true` |
| `time` | `string` | Elicitation `StringSchema` has **no `pattern`**; validate `HH:MM` server-side |
| `phone` | `string` | Same; normalise to E.164 server-side; must be `pii: true` |
| `money` | `number` (currency units) | Mostly an owner `response_field`; the server converts to cents |

### Provenance, evidence and sources
- **Provenance.** `{owner_confirmed, evidence: [{url, quote}]}`.
  - **Nearest-provenance rule (L5).** A fact's provenance is the closest enclosing `provenance`. For example, an item's `price` is covered by the item's provenance, and its `allergens` by their own.
  - **Several quotes.** An object can cite several quotes (menu page plus owner answer).
- **Evidence URL** is a web page or `urn:askonce:owner-(answer|edit):<id>`. The quote is the page's verbatim words, or the owner's exact words.
- **`sources`** is written by **onboarding code, not the LLM**. There is one entry per fetched snapshot (`content_sha256`, `retrieved_at`) or owner answer/edit.
- **`injection_canaries`.** Planted instructions found on a page, kept only so the certifier can prove they never reach output (L8). They are never extracted into facts.

### Safety facts
- **Where they live.** `allergens {contains, may_contain, provenance}` and `dietary {tags, provenance}` sit on items and option choices, and `facility_may_contain` sits on the catalog. All three require `owner_confirmed: true`.
- **Absent = unknown.** "I don't know. Want me to ask Rosa?" starts an owner Task.
- **Explicit "none".** `contains: []`, owner-confirmed, means "none of the nine".
- **Choices replace item values.** Allergens on a choice describe the item *as configured with that choice*; they replace the item-level values.

### Units
- **Money.** `amount_cents` (integer) with `currency`; `max_cents` for ranges.
- **Price.** `kind` is one of `fixed | from | range | quote_only | free`. `quote_only` and `free` may not carry an amount.
- **Durations.** Integer minutes everywhere (`duration_minutes`, `lead_time_minutes`, `expected_response_minutes`). 48 hours = 2880.
- **Times and timestamps.** Times are local 24h `HH:MM` in `business.timezone`. Timestamps are UTC with a `Z` suffix.

### `open_questions`
- **Fields.**
  - `kind`: `missing | contradiction | confirm`.
  - `targets`: fact refs.
  - `prompt` and `why` for the owner.
  - `candidates`, each with its own evidence.
  - `answer_field` (the elicitation form), `blocking`, `status` and `answer`.
- **Schema-enforced rules.**
  - A contradiction lists ≥2 candidates.
  - A `confirm` question lists ≥1.
  - `answered` requires an answer; `open` forbids one.
- **What the owner's answer becomes.**
  - an `owner_answer` source;
  - the owner-URN evidence that the updated facts cite.
- **`confirm` is an addition** to the requested `missing|contradiction`: it is how the site's allergen statements reach the owner (one tap: "Is all of this correct?").

### `certification`
- **`status` lifecycle.** `draft → failed | certified → live`.
- **Certified or live** requires `certified_run`, and a run with `result: passed`.
- **Runs** are append-only. Each carries:
  - `suite {id, version}`, `profile_revision` and `profile_sha256`;
  - `cases_total` and `cases_passed`;
  - `failures[]` (`check`, `fact_ref`, `detail`, `trace_id`).
- **A passed run has no failures, and a failed run has at least one.**
- **Check ids are a closed list** because checks are code; cases are data in the template: `grounded_fact, unknown_handed_off, disputed_fact_withheld, confirm_before_commit, idempotent_retry, injection_canary, task_lifecycle, schema_conformance, quote_in_snapshot`.

---

## 4. Invariants: schema vs loader

**Enforced by the schema** (all covered by the stress test below):
- **Commit guards.** `confirmation.before_commit` is always `true` and `idempotency` is always `"required"`.
- **R1.** Any module with `approval` needs an `owner_approval` module. `quote_request` must use `mode: always` with a `quote` decision.
- **Certification gates.**
  - **R2:** certified or live means no open blocking question.
  - Certified or live requires `certified_run` and a passed run.
- **Safety facts are owner-confirmed.** Any `owner_confirmed: true` must cite an owner URN.
- **Units are typed.** Integer cents, a currency whenever there's an amount, integer minutes, `HH:MM`, an IANA-shaped timezone and UTC timestamps.
- **Enums.** FDA allergen and dietary enums are closed.
- **Spoken text** contains no URLs or markup.
- **Strict core.** Invented fields fail everywhere; only `x-*` keys and `x-*` module types are open.
- **Questions.** Contradictions have ≥2 candidates; open questions carry no answer.
- **Sources.** A web source needs a snapshot hash; an owner-answer source needs a `question_id` and a URN.

**Enforced by the loader** (`x-invariants` L1-L11; JSON Schema can't express them):
- **L1 referential integrity.** Refs, fact refs, location ids, `approval.module` and session items all resolve.
- **L2 id uniqueness.**
- **L3 evidence in snapshot.** Each evidence URL matches a source, and the quote appears verbatim in that snapshot.
- **L4 units.** Currency equals `business.currency`, and the timezone exists in tzdata.
- **L5 nearest provenance.**
- **L6 statable rule.**
- **L7 certification binding.**
  - `certified_run` is the *last* run;
  - its `cases_passed == cases_total`;
  - its sha matches the profile;
  - an edit resets to draft.
- **L8 canaries never in output.**
- **L9 vocabulary typing.** Attribute values match their declared type and enum, and `read_back` and rule keys resolve.
- **L10 contradiction candidates.** Pairwise different values, from different sources.
- **L11 contact field.** Every booking, custom_order and quote_request module has an intake field of type `phone` or `email`. This is an error in strict (prod) mode and a warning in lenient (dev) mode. A module loaded leniently without one keys each anonymous call apart with a per-call nonce, so a retry there counts as a new request.

---

## 5. Onboarding extraction guidance

- **Code writes `sources`; the LLM writes facts plus evidence.** It never writes hashes, timestamps or run ids.
- **Extract per section, with the matching `$defs` as the tool schema.** For example, `item` for each menu page and `knowledge_module.entries.items` for the policy pages. Then assemble the profile and validate the whole thing. Bedrock tool-use schemas may not support `if`/`then`, `unevaluatedProperties` or `$ref` siblings. Pass the sub-schemas as given and rely on a **validate-and-repair loop** with the full schema, rather than expecting single-shot conformance.
- **Each failed L3 quote check becomes a re-extraction**, not a silent pass.
- **The LLM never writes a safety fact.** It emits a `confirm` question whose candidates hold what the site says.
- **Contradictions become `contradiction` questions.** Every reading is a candidate with its own evidence. The disputed fact is withheld until the owner answers (L6).

## 6. Adding a vertical
Write a template, with no code changes:
- category tokens;
- `vocabulary.attributes`;
- which modules to create;
- default `intake_fields`, `read_back` and approval rules;
- the certification cases.

The stress test below shows salon, home services, fitness studio (two booking modules), food truck (mobile stops), tutor (virtual plus service area) and florist profiles all validating against the unchanged schema.

---

## 7. Validation

```
$ server/.venv/bin/python -c "import json; from jsonschema import Draft202012Validator as V; \
  s=json.load(open('schema/business-profile.schema.json')); V.check_schema(s); \
  e=list(V(s, format_checker=V.FORMAT_CHECKER).iter_errors(json.load(open('schema/example-profile.json')))); \
  print('valid' if not e else [x.message for x in e])"
valid
```

**Stress test** (jsonschema 4.26, format checking on):
- **44/44 broken copies of the example are rejected**, each by the intended rule. The cases include:
  - `before_commit: false` or a missing idempotency guard;
  - deleting the owner_approval module (R1);
  - live with an open blocking question (R2), live without a passed run or without `certified_run`;
  - invented fields at the root, on an item and on a module;
  - fractional cents, or an amount without currency;
  - unconfirmed allergens, `owner_confirmed` without owner evidence, `gluten` as an allergen, `keto` as a dietary tag;
  - a contradiction with one candidate;
  - a URL in spoken text, markup in a spoken name;
  - `6pm`, `CST`, `PT45M`;
  - quote_request with rule-based approval;
  - fixed slotting with `step_minutes`;
  - a non-`x-` unknown module type, or the reserved module id `business`;
  - a positional JSON Pointer as a target;
  - an email field not marked `pii`;
  - a safety vocabulary attribute with `absent_means: false`.
- **7/7 profiles for other shapes are accepted.** Salon, home services, fitness studio, food truck, tutor, florist, and the bakery with an `x-waitlist` module and `x-*` keys.

## 8. Known gaps and open decisions
1. **Quote-in-snapshot (L3) needs snapshots.** The loader (`server/src/askonce_server/profile/`) enforces L1-L11, but it can only check L3 when a snapshot provider is configured. Without one, L3 is reported as skipped, and lenient (dev) mode downgrades L3, L7 and L11 to warnings.
2. **Attribute values aren't typed by the schema** (L9). For example, a tutor profile with `grade_levels: [6, 7, 8]` validates even though the vocabulary enum says `g6…`.
3. **The example's quotes and hashes are illustrative.** `schema/example-profile.json` is a schema-only variant of the bakery (120 Elm Street, Springfield, IL): it shares the demo bakery's name and `profile_id` but not its address, its quotes don't come from the demo site, and its hashes are placeholders. The profiles that match the demo sites are `profiles/*.json`: their quotes pass L3 against the pages in `sites/<profile_id>/`, and their `content_sha256` values are the real page hashes. The planted canary and the 6 pm vs 7 pm contradiction are designed in.
4. **Custom-order approval mode.** PLAN.md says custom_order "needs owner approval". The schema requires an `approval` block but allows `when_any_rule_matches`. The example routes cakes for more than 24 people, or with any dietary need, to Rosa, so the cold-open "gluten-free cake for 30" still becomes a Task. Use `mode: always` if every custom order should wait for the owner.
5. **Prices on choices.** A choice's `price_delta` means the stated total is computed (base plus delta). The certifier must either compare computed totals or say the parts ("$48, plus $8 for gluten-free").
6. **Intake-field `min`/`max` are input validation, not statable facts.** Any limit the assistant says aloud must come from `availability`, an approval rule or a knowledge entry, which carry provenance.
