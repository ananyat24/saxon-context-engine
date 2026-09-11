# Feature Inventory — Saxon Context Engine

Captured 2026-09-11, as STEP 0 of demo-polish. Inventory only — no behavior
changed. "Reachable" means: a demo presenter clicking through `frontend/`
(the `/ui` chat app) would see or trigger it today, with no dev tools.

Legend: **Full** = visible and usable in the UI. **Partial** = present but
muted/easy to miss/requires a specific path. **Invisible** = only reachable
via raw API/MCP/curl, not the demo UI at all.

## Observability (`metadata` on `/context/query` and `/context/query/causal`)

| Field | Backend | UI reachability |
|---|---|---|
| `retrieval_path` | Always populated (`entity_resolution` / `semantic_search` / `causal_chain` / `causal_fallback_direct_facts` / `causal_chain_empty` / `none`) | **Partial** — shown as a quiet one-line label ("matched directly to a known entity" / "found via broader search") under the answer (`frontend/app.js` `renderQueryStats`). Easy to miss in a live demo unless pointed out; only 3 of ~6 possible path values have a friendly label (`RETRIEVAL_PATH_LABELS`) — an unmapped value renders nothing. |
| `cache_hit` | Always populated | **Partial** — same quiet line, only appended when true ("served from cache..."). |
| `cost_usd` | Always populated (estimate for Anthropic/Azure OpenAI, `null`/`0.0` for Gemini) | **Invisible in the demo UI by deliberate design** (`app.js` comment: "Deliberately does NOT include cost_usd... this operator's own internal cost, not something every tenant's end user... needs to see"). Only visible via raw API response or the admin-only `GET /api/v1/admin/spend` (needs `ADMIN_API_KEY`, no UI). Worth a deliberate decision before the demo: is this staying hidden, or does the peer-engineering audience want to see it as a selling point? |
| `latency` | **Not a field the backend returns at all.** Only inferable client-side from request/response timing; nothing in `ContextPacket.metadata` names it. | **Invisible** — doesn't exist as structured data anywhere. |
| `group_ids` | Populated | **Invisible** — present in metadata, not rendered anywhere in the UI. |
| `result_limit_hit` | Populated when the semantic-search cap is hit | **Partial** — drives a client-side "see more results" affordance per README, not independently surfaced as a number. |

## Temporal state / valid-from / superseded facts

- **Full in the UI for plain Ask.** Each fact renders with a current/superseded badge (`is_valid`), and the orchestrator's `_build_answer_lines` (fixed post-bug-3, see watchlist) explicitly surfaces an orphaned invalidated fact as "No longer current, but real: ..." rather than dropping it.
- **Full for transitions.** When both an old and new fact are in the same retrieval batch, they render as a single "changed from X to Y" line.
- Raw `valid_at`/`invalid_at` timestamps: present in every fact object, **not rendered** in the UI (badges are boolean current/superseded only, no dates shown to the viewer). Worth deciding whether a demo audience should see the actual dates.
- OData feed (`/api/v1/odata`) exposes `valid_at`/`invalid_at`/`is_valid` explicitly for Power BI — this is the one place raw temporal fields are consumer-facing, and it's **entirely outside the demo UI** (a separate BI integration, curl/Power BI Desktop only).

## Provenance / source attribution

- **Full.** Each fact in the evidence list carries `sources` (which document/connector it came from); `frontend/app.js` renders this. Once a query spans more than one connector, per-fact source tagging is visible.
- **Source authority tie-break** (`source_authority`, `is_authoritative`): **Partial** — a small "Authority N" badge renders on a connector's row only when `source_authority > 0` (`app.js` line ~474); the *effect* of authority (which fact won a disagreement) isn't independently called out anywhere in the answer itself, so a viewer has to infer it from which fact reads as current.

## Tenancy scoping

- **`group_id` / `knowledge_base`** — **Full.** Visible as the KB tab selector in the UI; every route validates it against the tenant's own list server-side.
- **`X-API-Key`** — **Full** (as the access-key modal gating the whole app) but the key itself, once entered, is never shown back to the user (masked), which is correct behavior, not a gap.
- **`document_set`** — **Full**, exposed as a scope option in the Ask dropdown alongside individual knowledge bases.
- **`as_user` (org-hierarchy-scoped visibility)** — **Partial, improved as of the latest commit.** A role/user dropdown exists in the UI (`getSelectedUser()`), and per commit 7d8088f `solandra_supply_chain` now has a seeded org chart too (previously only `contoso_dw` did — "the role dropdown was hidden in the UI because no :User nodes existed for this knowledge base"), so it should now be demoable on the Solandra dataset the baseline run above used. Still silently does nothing for any *other* KB without a seeded chart, with no UI messaging that a chart isn't seeded. Also **not combinable with `document_set`** (backend limitation, not surfaced as a disabled-state or tooltip in the UI).

## Document sets

- **Full.** `POST /api/v1/document-sets` and the UI's document-set management panel (create, name, group connectors, `is_public` flag) are reachable end-to-end.

## Ontology packs

- **Invisible from the chat UI entirely.** The 9 domain packs (healthcare, finance, manufacturing, retail, legal, pharma, sales, supply chain, technology) plus core and customer-extension are real, validated (`scripts/check_ontology.py`), and drive extraction — but nothing in `frontend/` lists, browses, or names them. The only UI-adjacent path is `GET /api/v1/entities` (raw JSON, `/docs` Swagger UI) — not a demo-friendly surface. **This is a real gap for a peer-engineering demo** if "the ontology adapts per industry" is part of the pitch — there's currently nothing to *show*, only to describe.

## Connector health / status

- **Full, but recently simplified.** Connector table shows status/health per connector and aggregate "N connectors need attention." As of the latest commit on `main` (7d8088f, "drop misleading connector staleness check"), the time-based "stale" amber badge was **deliberately removed** — it was a false positive under Azure Container Apps' scale-to-zero behavior (the in-process scheduler doesn't tick continuously, so every connector aged into "stale" within ~45 minutes regardless of real freshness). Connector health now reflects `error`/`never_synced`/`queued`/`ok` only, no time-based staleness. A "Real-time" badge for `outlook_mail`'s push subscription still renders.
- **Partial: "Fix attribution" recovery button.** Exists and does real work (purge + forced resync for connector-scoped orphaned-tag issues), but per CLAUDE.md's own newest finding, it does **not** fix the deeper "orphaned episodes with no connector_id at all" case (the still-open Brightpeak bug, reconfirmed live in this session's baseline — see question 3/4 below) — the button's own UI copy may currently overpromise what it fixes; worth checking the actual empty-state text against this known limitation before a demo where someone might click it expecting a full fix.

## Causal reasoning / recommendations (`/query/causal`)

- **Backend: Full and confirmed working live** (this session's baseline run, question 1: full causal chain Order → QualityEvent → Component → Supplier → alternate-supplier recommendation, produced in ~9s with a real `decision_id`-generating recommendation).
- **UI: REMOVED as of the latest commit (7d8088f, same-day, "Remove Explain-why button... wasn't adding value yet").** The "Explain why + recommend" button that used to expose this endpoint from the chat UI is gone from `frontend/app.js`; the HTTP/MCP endpoints themselves are untouched and fully functional. **This means the single most fleshed-out reasoning capability in the backend — multi-hop causal chains with a generated recommendation and an auditable Decision node — currently has no click-path in the demo UI at all.** This is the single highest-priority item for whoever plans Step 3: either the causal endpoint needs a UI entry point restored/redesigned before the demo, or the demo plan needs to explicitly route around the UI and hit `/query/causal` via curl/Postman/MCP instead, and the presenter should know this going in either way.
- `metadata.decision_id` (the auditable `:Decision`/`:SaxonRecommendation` node written per recommendation) — **Invisible** even when the endpoint is called directly; no "view the audit trail" affordance anywhere.

## MCP server (`/mcp`)

- **Invisible from the chat UI's main flow, but discoverable.** There's a "Connect an AI agent" section in the UI (per `app.js` grep) that presumably shows MCP connection instructions — worth confirming it actually renders the three real tool names and a working example, since a peer-engineering audience is exactly who'd want to see this.
- Confirmed tool names from `app/mcp/server.py` and README (do not deviate from these in demo talking points):
  - `query_context_graph`
  - `query_causal_chain`
  - `list_available_sources`
- Auth: same `X-API-Key` as the HTTP API, no separate MCP credential.

## BI access (OData / Power BI)

- **Entirely invisible from the demo UI.** `GET /api/v1/odata/*` (Entities + Facts feeds) is real, tenant-scoped, Power-BI-Desktop-compatible — has zero presence in `frontend/`. If this is meant to be part of the pitch, it currently requires a live Power BI Desktop demo outside this app, not a UI toggle.

## Admin plane

- **Entirely invisible from the demo UI**, by design (`ADMIN_API_KEY`-gated, curl/REST-client only): tenant creation/removal without redeploy, connector-tenant reassignment, spend totals. Correctly excluded from a client-facing surface — flagging only because Step 3 of the larger polish plan may want to decide whether any read-only admin view belongs in a future internal-only UI mode.

## Summary: what's likely worth Step-3 attention (UI-facing polish, not code logic)

1. `cost_usd`/latency have no visible home in the UI at all — a deliberate choice today, worth confirming still wanted for this specific demo audience.
2. Ontology domain packs have no visible surface — biggest "described but not shown" gap if the industry-pack story matters to the pitch.
3. `as_user` silently no-ops on any KB without a seeded org chart — needs either a UI guard or a demo script that only touches `as_user` on `solandra_supply_chain`/`contoso_dw`.
4. `retrieval_path` labels are incomplete (`causal_chain`, `causal_fallback_direct_facts`, `causal_chain_empty` aren't in `RETRIEVAL_PATH_LABELS`) — moot for the demo UI right now only because the causal UI entry point is gone (see #5), but relevant again the moment it's restored.
5. **The causal/"Explain why" UI entry point was removed same-day, before this task started** — the backend capability is real, live, and impressive (see the baseline run), but has zero click-path in the current demo UI. This is the single biggest decision point for Step 3: restore a UI entry point, or plan the demo to call the endpoint directly.
