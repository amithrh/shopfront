# askonce-onboarding

A small-business website URL becomes a draft AskOnce business profile (`profile.json`, valid against
`schema/business-profile.schema.json` and the server's invariants L1-L11) plus the questions the owner
must answer before certification (`questions.json`: contradictions, missing facts, safety confirmations,
and suspected prompt injections).

```bash
uv run askonce-onboard --demo            # the 3 demo sites, from recorded model replies ($0)
uv run askonce-onboard --demo --live     # call Bedrock for pages not recorded yet (spend-capped ledger)
uv run askonce-onboard https://example.com/ --live
uv run askonce-certify --demo            # certification-verdict.json per profile (no model, $0)
uv run askonce-answer out/thimbleberry/profile.json fixtures/owner-answers/thimbleberry.json   # owner answers, then re-certify
uv run askonce-compare                   # onboarded demo profiles vs the hand-built gold profiles (exit 1 on any mismatch)
uv run pytest
```

Certification runs each profile through the real MCP server in process and checks the trace in code.
The verdict is CERTIFIED, CONDITIONAL (owner questions still open; disputed facts withheld) or
NOT_CERTIFIED. A freshly onboarded demo profile is CONDITIONAL. After the scripted owner answers in
`fixtures/owner-answers/` are applied, it is CERTIFIED.

Outputs go to `onboarding/out/<profile_id>/` (`profile.json`, `questions.json`, `report.json`, `snapshots/`).

The pipeline: crawl the site and snapshot each page by sha256. Quarantine text that addresses AI agents
(it becomes an injection canary and is cut from what the model reads). Run one Bedrock call per page
(router role `onboard`: Haiku 4.5, then Nova Pro) into a small quoted-facts JSON. Read each page's own
structure in code (`structure.py`): price tables and menu cards become items, a table of sizes or job types
becomes one item with an option group, pick lists (flavors, fillings) and `+$` extras become option groups,
`Contains:` lines and allergen columns become allergens, a form becomes intake fields, `<dt>/<dd>` pairs,
FAQ headings, paragraphs and list items become policy entries, and a staff schedule gives each person's
working days. Structure is preferred over the model's reading of the same page (the model cannot be trusted
to group variants or copy an allergen list). Holiday closures are resolved in code (`holidays.py`): a printed
date as written, a named holiday as its next date in the business's country. Re-locate every quote on its
page or drop the fact. Assemble the profile deterministically: merge one product seen on several pages, drop
category teasers, derive approval rules (servings thresholds, items whose footnote mark needs a deposit),
read-outs before confirming and booking resources. Ask the model for a few customer phrasings per policy
answer (cached per entry, `variants.py`), and put first a question the server's own matcher answers with that
entry. Find contradictions per key, plus a model review pass held to invariant L10; an owner's answer to a
contradiction also corrects any policy entry that repeated the overruled value. Validate with the server's
strict loader against the snapshots. Model replies are cached in `fixtures/llm/`.

`askonce-compare` checks the result against `profiles/*.json` on what matters to a customer: business details,
hours and closures, service areas, every item with its price, duration, option groups and choices, allergens,
dietary tags, days and stated attributes, policy content (numbers, yes/no facts and wording, as spoken), each
order/booking/quote flow (items, approval rules, intake fields, availability, staff and their days, visit
windows, read-outs) and the owner. Matching is by content, never by id. It also asks the gold profile's own
customer phrasings through the server's policy matcher and lists the ones that reach no answer ("matcher
recall": reported, not counted as mismatches, since it measures the matcher's synonyms rather than what
onboarding extracted).
