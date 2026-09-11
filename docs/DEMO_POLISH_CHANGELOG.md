# Demo Polish Changelog

Branch: `demo-polish`. This log is updated as work lands, not written once at the end.

## Step 0 — Inventory & baseline (commit `28013da`)

- `docs/FEATURE_INVENTORY.md`, `docs/REGRESSION_WATCHLIST.md`, `eval/baseline_pre_polish.json` added.
- Headline finding: the causal-reasoning UI ("Explain why + recommend") had been removed the same day this effort started (`7d8088f`), leaving the backend's most impressive capability with no click-path.

## Restored: Explain-why + recommend button (commits `f440690`, `f50b52f`, `70bd490`)

- Verbatim reinstatement of the button, panel, and `runCausalQuery()` removed in `7d8088f`. The backend endpoint (`POST /context/query/causal`) was never touched and was confirmed working on live data before restoring the UI.
- Added friendly labels for the causal endpoint's own `retrieval_path` values (`causal_chain`, `causal_fallback_direct_facts`, `causal_path_between_entities`, `causal_chain_empty`), so the panel's observability line doesn't go blank the way it would have with only the plain-Ask labels.
- **Data/question gap found and addressed in part:** the three existing curated suggested questions for `solandra_supply_chain` are all single-entity lookups, so trying "Explain why" on any of them lands on the causal endpoint's fact-only fallback and just echoes the plain-Ask answer. Added one dedicated causal chip ("Why is SO-45821 at risk?"), confirmed live to walk a real multi-hop chain (QE-2091 → Ferrotek quarantine → Plant 2 throughput → SO-45821) and produce a real generated recommendation. Only one such question exists in the seeded data today — more causally-connected fixtures would be needed to show this on more than this one example. Not built; needs a decision on priority for next week.

## Ontology domain packs surfaced (commit `074e571`)

- New read-only route `GET /api/v1/ontology/packs` (new file `app/api/ontology_packs.py`) lists the core ontology plus all 9 domain packs with their own identity, separate from `GET /entities`' flattened view, which is untouched.
- New "Industry packs" panel in the UI. `legal`, `manufacturing`, `sales`, `supply_chain` show their real entity/relationship types; `finance`, `healthcare`, `pharma`, `retail`, `technology` are labeled "not filled in yet" rather than hidden — they are genuinely empty stub files on disk today.
- New test coverage (`tests/test_ontology_packs.py`) pins the populated/empty split to what's actually in `ontology/domains/*.yaml`.

## `as_user` role filter: honest empty state (commit `68cec73`)

- Previously hid the dropdown entirely on a knowledge base with no seeded `:User` nodes. Now shows a disabled, explained placeholder instead, so switching to an unseeded knowledge base mid-demo doesn't look like the feature disappeared.

## Fix-attribution copy tightened (commit `8abd3f1`)

- The existing "Fix attribution (re-sync for real)" button can only reach facts already tagged to a connector that hasn't re-synced; it structurally can't reach untagged/orphaned episodes from before connector-id tagging existed — exactly what's blocking the still-open Brightpeak Automation ownership answer (`CLAUDE.md`, commit `8c42148`). Added a plain-language caveat so a peer engineer who tries it on a case like that doesn't read an unchanged answer as the button being broken. Copy-only; the underlying orphaned-episode bug is untouched and needs new tooling that doesn't exist yet.

## Cold-start honesty (commit `3b0caa6`)

- The header health badge now says "waking up the service…" (with a tooltip explaining Azure Container Apps' scale-to-zero) if `/health` hasn't answered within ~2.5s, instead of sitting on a bare "checking…" that reads as a hung page during a cold start.

## Deliberately left alone

- `deploy_azure.sh`'s empty-secret bug (`task_78c64461`) — being fixed in a separate session, not duplicated here.
- The causal walker, entity resolver, and supersession handling — read, never modified, per the regression rules.
- `cost_usd`/latency visibility — a real open question, not a UI bug. Deliberately not decided unilaterally; see "Open decisions" below.
- The orphaned-episode/Brightpeak data bug — diagnosed and documented, not attempted; needs new purge tooling per `CLAUDE.md`.

## Open decisions, not yet answered

1. **`cost_usd`/latency for this specific peer audience.** Currently hidden by deliberate design (see `renderQueryStats`'s own comment on why). This peer team is other engineers building a similar system — they may specifically want to see the cost/latency story. Needs a call before building a UI toggle for it.
2. **How much more causally-connected demo data is worth adding before next week**, so "Explain why" has more than one good example question. This is real ingestion work through the connector interface (Step 4 of the original plan), not a quick UI fix.

## Not yet started

Step 1 (full visual design system), Step 2 (first-run explainer panel + inline glossary), the tenancy/"which tenant does my key resolve to" display, MCP tab polish, admin-surface polish, the snapshot switcher across the three sync dates, and a documented reset-to-clean-demo-state script.
