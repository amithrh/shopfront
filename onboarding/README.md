# askonce-onboarding

A small-business website URL becomes a draft AskOnce business profile (`profile.json`, valid against
`schema/business-profile.schema.json` and the server's invariants L1-L11) plus the questions the owner
must answer before certification (`questions.json`: contradictions, missing facts, safety confirmations,
and suspected prompt injections).

```bash
uv run askonce-onboard --demo            # the 3 demo sites, from recorded model replies ($0)
uv run askonce-onboard --demo --live     # call Bedrock for pages not recorded yet (spend-capped ledger)
uv run askonce-onboard https://example.com/ --live
uv run pytest
```

Outputs go to `onboarding/out/<profile_id>/` (`profile.json`, `questions.json`, `report.json`, `snapshots/`).

The pipeline: crawl the site and snapshot each page by sha256. Quarantine text that addresses AI agents
(it becomes an injection canary and is cut from what the model reads). Run one Bedrock call per page
(router role `onboard`: Haiku 4.5, then Nova Pro) into a small quoted-facts JSON. Re-locate every quote
on its page or drop the fact. Assemble the profile deterministically. Find contradictions per key, plus a
model review pass held to invariant L10. Validate with the server's strict loader against the snapshots.
Model replies are cached in `fixtures/llm/`.
