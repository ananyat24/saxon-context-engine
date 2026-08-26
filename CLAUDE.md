# Saxon Context Engine — Product & Technical Specification

**From: single-tenant graph prototype → live, multi-source enterprise context platform**

Grounded in the current [`saxon-context-engine`](https://github.com/ananyat24/saxon-context-engine) repository (Python/FastAPI + Neo4j + Graphiti). This spec is written to build *on top of* what's already there rather than replace it — the existing ontology, temporal graph, and tenant-isolation work is real infrastructure, not a throwaway prototype, and the roadmap below treats it that way.

---

## Part 1 — Business-Facing Overview

### The problem

A company's knowledge about its own business lives in a dozen disconnected places: a CRM, an ERP, spreadsheets, email threads, support tickets, Slack messages, maintenance logs. Every one of those systems has its own vocabulary, its own notion of what "the customer" or "the account" means, and its own idea of what's current. When an AI assistant is connected to a business today, it's usually connected to one of these systems at a time, or handed a raw export and left to guess how the pieces fit together. Ask it a question that spans two systems — "who manages the Contoso account, and has anything about their contract changed recently?" — and it either can't answer, or answers confidently from a stale or incomplete slice of the truth.

The failure mode is not "the AI is bad at reasoning." It's that nobody handed it organized, current, sourced facts to reason over. That's a data engineering problem wearing an AI costume, and most companies trying to ship an AI assistant end up solving it badly and expensively, once, per client, from scratch.

### What Saxon does

Saxon sits between a company's scattered systems and whatever is asking questions of them — a chat interface, an agent, another application — and does the consolidation work in between, continuously, so the answer is always built on the current state of the business rather than a one-time export.

Picture a corkboard with pins and string: each pin is a real thing (a person, a company, a machine, an order), each string is a relationship between two things (this person manages this account, this machine sits in this factory). That corkboard is the graph at the center of Saxon. Every pin and string carries a timestamp for when it became true and whether it's still true today, so the graph never just says "Sarah manages the account" — it can say "Sarah managed the account until March, when it moved to Marcus," without ever deleting the older fact. And every pin and string points back to the document, email, or record it came from, so nothing in the graph is an unsourced claim.

Today, getting facts onto that corkboard means manually feeding Saxon a file or a web page. The product this spec describes is the version where Saxon plugs directly into a client's actual systems — their CRM, their database, their email, their document store — pulls from all of them continuously, keeps the graph current as those systems change, and reconciles the same real-world entity when it shows up differently across sources (a "Contoso Ltd" in the CRM and a "contoso.com" domain in email traffic are the same customer).

### The product experience

From the user's side, this stays simple on purpose: ask a question in plain language, get one clear answer. What makes it trustworthy is what sits underneath that answer, one click away — an expandable panel showing exactly which facts the answer drew on, which system each fact came from, when it became true, and whether it has since changed. The answer is the headline; the evidence trail is always there for anyone who wants to check it, without it needing to be read every time. That's the same shape as "the answer plus a citations panel," but built on facts assembled from a graph that already reconciled a dozen systems, not on a pile of retrieved text chunks that may or may not agree with each other.

### Why this is hard to copy quickly

Three things compound here, and they're each real engineering, not marketing claims:

*Temporal correctness.* Most retrieval-augmented systems treat "the account is managed by Marcus" and "the account is managed by Sarah" as two chunks of text that both look relevant and let the model sort it out. Saxon's graph knows one superseded the other and when, so the model is handed the current fact (and, when asked, the history) rather than being asked to referee a contradiction at answer time.

*Vocabulary that adapts without a rewrite.* A hospital, a factory, and a law firm describe similar concepts — an "asset," a "case," a "batch" — completely differently. Saxon's ontology is layered (an enterprise-neutral core, industry packs for healthcare/finance/manufacturing/retail/legal/pharma/sales/supply chain/technology already exist, a per-client extension layer on top), so onboarding a new client's vocabulary is a configuration change, not new application code.

*Sourced, reconciled facts instead of a text dump.* Because ingestion constrains extraction to the ontology's actual entity and relationship types, the graph doesn't accumulate the usual pile of near-duplicate, inconsistently-named nodes that make most knowledge graphs unusable at scale. Every fact keeps a link back to its source, which is what makes the "click to see where this came from" experience possible at all rather than being retrofitted after the fact.

### Who this is for

The ontology domain packs already built (healthcare, finance, manufacturing, retail, legal, pharma, sales, supply chain, technology) point at the intended shape of client: mid-market and enterprise organizations whose knowledge is real but fragmented across systems that don't talk to each other, and who need an AI layer that can answer questions spanning those systems without hallucinating across the gaps. A sales org asking "who owns this account and what's changed" and a manufacturing plant asking "what's this machine's maintenance history and who's responsible for it" are the same underlying product.

### Where things stand today, in plain terms

What exists now proves the hard part of the idea works: text goes in, gets turned into structured, timestamped, sourced facts, and comes back out through a question-answering API with per-client data isolation. What's missing is breadth and automation — today, getting data in means manually pointing Saxon at a file or a single web page; nothing yet reaches into a live CRM, database, or inbox and keeps itself current on its own. Everything in Part 3 is about closing exactly that gap, in order, without re-doing the parts that already work.

---

## Part 2 — Technical Architecture & Stack

### Layered architecture

The system is organized in the same shape as the reference architecture this spec was asked to align with (source layers → orchestration → context graph → context API → consumers), mapped onto Saxon's own terms:

```
 STRUCTURED IQ        DOCUMENT IQ           LIVE IQ              WEB IQ
 (CRM/ERP/DBs via     (email, PDFs,        (CDC + webhooks,     (already built:
  connectors)          tickets, Slack)      push notifications)  app/ingestion/web_source.py)
        \                   |                    |                    /
         \__________________|____________________|___________________/
                                       |
                         INGESTION & EXTRACTION LAYER
              (per-source connector -> normalize -> prose ->
               ontology-constrained LLM extraction -> dedup/content-hash)
                                       |
                          CONTEXT ORCHESTRATION LAYER
                (app/context/: Connect · Resolve · Reconcile ·
                       Plan · Rank · Assemble · Deliver)
                                       |
                              CONTEXT GRAPH (Neo4j)
              Entities · Relationships · Temporal validity (Graphiti) ·
              Evidence/provenance · Source authority · Ontology types ·
                    Org/authorization subgraph (:User, REPORTS_TO)
                                       |
                          CONTEXT API  +  MCP SERVER
                 (FastAPI /api/v1/context/query, tenant-scoped)
                                       |
                 Saxon chat UI  ·  Claude/Copilot/agents via MCP  ·
                         client apps via API key
```

Everything below the "Ingestion & Extraction" line already exists in the repo in working or partially-working form. Everything above it (the four source-type lanes) is the main build-out this spec describes.

### Stack by layer

| Layer | Today (in repo) | Recommended v-next | Why |
|---|---|---|---|
| Runtime / API | Python 3.11+, FastAPI, uvicorn, pydantic v2 | Keep. Add background job runner (APScheduler → Celery/RQ as volume grows) for scheduled/continuous sync | No reason to change a working, well-tested foundation; scheduling is additive |
| Graph database | Neo4j (Community, single DB, `group_id`-scoped multi-tenancy) | Keep Neo4j. Move specific clients to Aura Professional/Enterprise (per-tenant database) only when their compliance needs it | Already the repo's own stated plan — see README's "worth revisiting if a client's compliance requirements call for it" |
| Temporal knowledge graph engine | Graphiti (`graphiti-core[anthropic,google-genai]>=0.29.3`) | Keep and track upstream closely — the 0.29.x line already ships hybrid search (semantic + BM25 + graph traversal), cross-encoder reranking, and a reference MCP server, which map directly onto this spec's token-efficiency and MCP goals. Confirm exact latest patch at implementation time (`pip index versions graphiti-core`) | This is the single most important existing dependency; most of "make retrieval good and cheap" is enabling features Graphiti already has rather than building new ones |
| LLM providers (extraction/synthesis) | Gemini (default, per-tenant key, free-tier friendly), Azure OpenAI (shared, alternative), Anthropic/Claude Haiku 4.5 (shared, alternative — pinned to `anthropic<1.0.0` due to a `graphiti-core` bundled-client incompatibility) | Keep multi-provider design. For production tenants past the demo/free-tier stage, default to Claude (Haiku for extraction/synthesis, Sonnet for harder queries) or a shared Azure OpenAI resource, not per-tenant Gemini free-tier keys, which rate-limit hard under continuous ingestion | Per-tenant Gemini free keys are fine for a pilot; they become the bottleneck the moment ingestion is continuous rather than a manual script run — the repo's own `.env.example` says as much |
| Embeddings/reranking | Gemini embeddings (`gemini-embedding-001`), used even in `anthropic` mode since Claude has no embeddings API | Keep as-is; consider Voyage embeddings (natively supported by Graphiti, tuned for retrieval quality) if quality becomes the bottleneck | No urgent reason to change; flagged here since it's a one-line config change in `graphiti_adapter.py` if needed later |
| Ontology | Hand-authored YAML, core + domain packs + customer extension, validated by `app/ontology/validator.py`/`registry.py` | Keep the YAML model as the source of truth. Add a thin authoring UI on top (already on the repo's own roadmap) that reads/writes the same YAML rather than a parallel config store | Already well-designed (additive layering, `extends:` chains); the gap is authoring ergonomics, not the model itself |
| Ingestion — files/web | `app/ingestion/file_source.py`, `web_source.py`, `structured.py` (CSV→prose), `pipeline.py` (Graphiti episode wrapper) | Keep. Generalize `structured.py`'s "turn a row into prose" pattern into the shared interface every new connector type uses | This prose-conversion step is a genuinely good design choice already in place — extraction quality depends on it |
| Ingestion — structured DB / SaaS connectors | Not built (`app/graph/connectors.py` + `app/api/connectors.py` exist but only support `type="web"`) | Generalize the existing `Connector` model to more `type`s; for "any and all" source coverage without hand-writing 50 integrations, put a **unified connector platform** (Nango, open-source, self-hostable, usage-based pricing, ~900 prebuilt API connectors incl. CRM/ERP/HRIS/accounting/documents/email) *underneath* Saxon's own ingestion pipeline. Nango handles OAuth, polling/webhooks, and normalization; Saxon's `IngestionPipeline` still does the ontology-constrained extraction step | Hand-rolling connectors for "any and all" client systems is the single most expensive way to build this. A unified connector layer turns "write a CRM connector" into "map a normalized Nango record to prose," reusing infrastructure the repo already has (`structured.py`'s row→prose pattern) |
| Ingestion — structured DB, direct/live | Not built | For clients with direct database access requiring sub-minute freshness (rather than API-based SaaS polling), add CDC via Debezium (Postgres/MySQL logical replication → change events) feeding the same ingestion queue | CDC is the standard, mature answer for "keep a graph current with a live database" and is a separate concern from SaaS/API connectors (which Nango covers) |
| Ingestion — unstructured documents/email | `app/ingestion/unstructured.py` exists (file structure only, logic stubbed per README status table) | Use Gmail/Microsoft Graph APIs (via Nango's unified email connectors, or direct if a client requires it) for message retrieval; use **Docling** (actively maintained, strong table/layout fidelity) for parsing PDFs/DOCX/attachments into clean text before it reaches the extraction step | Docling has become the stronger default over Unstructured.io for document-structure fidelity as of 2026; either is a drop-in before Graphiti's episode ingestion, so this is a low-risk, swappable choice |
| Change queue (decouples capture from extraction) | Not built (today: capture and extraction happen in the same request) | Add a lightweight queue (Redis Streams, or SQS if already on AWS) between "a connector/CDC stream saw new data" and "run ontology-constrained extraction on it" | Decoupling lets ingestion volume spike (a big CRM sync) without spiking concurrent expensive LLM calls; also gives a natural place to batch and rate-limit extraction for cost |
| Entity resolution / reconciliation | Relies entirely on Graphiti's built-in semantic entity dedup during extraction; the repo's own roadmap notes an earlier deterministic-matching stub was removed as premature | Add deterministic cross-source identity matching only where it's genuinely needed (e.g., matching a CRM contact's email address to an email-connector's sender, or an ERP customer ID to a CRM account ID) as an explicit "Reconcile" step feeding Graphiti, rather than expecting semantic dedup alone to bridge structurally different ID systems | This is exactly the gap the repo's own README flags: "worth adding back only if a concrete gap shows up, such as a cross-system ID match Graphiti's matching misses" — multi-source ingestion is precisely when that gap shows up |
| Retrieval | `GraphRetriever` implemented and working; `SemanticRetriever`, `LiveDataRetriever` are stubs; shared `TextRetriever` interface already supports appending retrievers without restructuring | Implement `SemanticRetriever` on top of Graphiti's hybrid search (semantic + BM25) rather than a separate vector store; add a cheap query planner (`app/context/planner.py`, currently a stub) that decides *which* retrievers a given query actually needs, instead of always running all of them | Every retriever that runs is tokens and latency spent; a planner that skips semantic search for a query the graph retriever already answered confidently is the single biggest lever on both cost and speed |
| Context assembly / orchestration | `ContextOrchestrator` (working): pools retriever results, dedupes, detects fact transitions, synthesizes a one-line answer via a small capped-token LLM call only when >1 fact is involved | Keep this design — it's already token-conscious (skips synthesis entirely for a single-fact answer, caps synthesis at 80 tokens). Extend `ContextPacket` so the API response carries structured, source-attributed evidence (not just a flattened summary string) for the "expand to see sources" UI | This is genuinely good existing design; the gap is in the response shape for the UI, not the logic |
| Context API | FastAPI `/api/v1/context/query`, `/api/v1/graph/*`, `/api/v1/entities`, API-key tenant auth, `as_user` org-scoped visibility | Keep. Add an **MCP server** exposing the same query capability as a tool, so Claude Desktop/Code, Copilot, or any MCP-capable agent can query a client's consolidated graph directly — this is the literal "Context API/MCP" layer of the reference architecture, and Graphiti already ships a reference MCP server to build from | This turns Saxon from "an API you build a chatbot on top of" into "a context source any agent can plug into," which is the direction the whole industry (including Claude's own MCP support) is moving |
| Frontend | Static HTML/CSS/JS chat-style UI (~1,700 lines total) | Extend to render the answer with an expandable "sources" section per the product experience in Part 1, backed by the richer `ContextPacket` shape above | Directly implements the UX described in Part 1 |
| Multi-tenancy & security | API-key → `TenantConfig` lookup, `group_id` data partitioning, per-tenant Graphiti client pooling, org-hierarchy-scoped visibility (`as_user`), local spend caps | Keep for early/mid-size clients. Move to per-tenant Neo4j databases (Aura Enterprise) for clients whose compliance needs stronger isolation than shared-database + `group_id` | Already exactly the repo's own stated tradeoff and plan |
| Cost control | `spend_limiter.py`: local $ budgets for ingestion vs. query buckets, provider-agnostic | Keep. Add Anthropic prompt caching (see below) and semantic response caching as additional levers, tracked through the same spend accounting | Builds on existing infrastructure rather than replacing it |
| Deployment | `Dockerfile` + `scripts/deploy_azure.sh` for Azure Container Apps, deployed to `saxon-context-engine.kindsea-5648017b.southindia.azurecontainerapps.io` | Pair with Neo4j Aura (managed, network-reachable by default) — already in place | N/A — already live |

### Token & cost efficiency, concretely

Each of these is either already partly built or a natural extension of something that is:

1. **Ontology-constrained extraction** (already built). Passing the ontology's actual entity/relationship types into extraction means the model isn't burning tokens (or accuracy) inventing types — the repo's own README documents this concretely: unconstrained extraction on the same data invented six of eight relationship types.
2. **Content-hash dedup before extraction** (already built for the web connector, in `app/api/connectors.py`). A sync that fetches identical content skips extraction entirely rather than paying for a near-duplicate episode. Generalize this to every connector type, not just web.
3. **Small-model, capped-token synthesis** (already built, `_SYNTHESIS_MAX_TOKENS = 80`). A single-fact answer skips the synthesis LLM call entirely; multi-fact answers get a tightly bounded call. Extend the same discipline to any new synthesis paths.
4. **Query planning to avoid running every retriever on every query** (planner exists as a stub, not yet implemented). The graph retriever alone answers most "who/what/when" questions; semantic search and live-data lookups should only fire when the planner judges the graph result insufficient, not unconditionally.
5. **Prompt caching for static, repeated context.** The ontology schema passed into every extraction call, and any per-tenant system prompt, is large and identical across many calls. Anthropic's prompt caching (as of 2026: cache writes at roughly 1.25x base input cost, cache reads at roughly 10% of base input cost) turns a repeated ontology schema from full-price-every-time into a one-time cost amortized across a session or an ingestion batch. This is a direct, mechanical win with no quality tradeoff — worth wiring in as soon as extraction moves to Claude for a given tenant.
6. **Batch processing for non-time-critical ingestion.** Historical backfills and large one-time imports don't need synchronous, real-time extraction — Anthropic's Batch API (roughly half the per-token cost) is a straightforward fit for "ingest this client's last two years of CRM history" style jobs, as opposed to live incremental syncs.
7. **Semantic response caching.** Two different users (or the same user twice) asking near-identical questions shouldn't re-run retrieval and synthesis from scratch. A cache keyed on a normalized/embedded form of the query, scoped per-tenant and invalidated when relevant facts change, avoids repeat spend on repeat questions.
8. **Hybrid search over a separate vector database.** Graphiti's own hybrid search (semantic + BM25 + graph traversal) removes the need to stand up and pay for a separate vector store for semantic retrieval — one less system, one less place tokens/cost leak.

### Compatibility notes and explicit v0-overwrite flags

Where full compatibility with the current stack would cost quality or features, this is called out as a deliberate later-version break rather than quietly working around it forever.

- **`anthropic<1.0.0` pin.** `graphiti-core`'s bundled `AnthropicClient` calls `messages.create(..., temperature=...)` in a shape that breaks under the Anthropic Python SDK's 1.x changes. This is fine to leave as-is while Gemini remains the default provider. **Overwrite note:** the moment Claude becomes the default extraction/synthesis provider (recommended above, once ingestion goes continuous), this pin needs a real fix — either wait for an upstream `graphiti-core` fix, or write a thin custom `LLMClient` for Graphiti that calls a current-version Anthropic SDK directly, bypassing the bundled client. Don't build long-term architecture around staying below `anthropic 1.0`.
- **Neo4j Community + `group_id` tenancy.** Correct, working, and honestly documented in the repo as "a reasonable baseline, not the strongest possible guarantee." **Overwrite note:** for any client with real compliance requirements (healthcare/pharma clients in particular, given the existing ontology packs), plan a per-tenant Neo4j database (Aura Enterprise or self-hosted Enterprise) migration rather than trying to harden the shared-database model further — the repo's own README already reaches this conclusion.
- **Synchronous, click-to-sync connectors.** Deliberately simple for the current single-connector-type MVP (documented in `app/api/connectors.py`'s own comments as "not a background job or a schedule... real future scope, not this"). **Overwrite note:** this model is fully replaced, not extended, once live/continuous ingestion (v2 below) lands — it becomes a scheduler + queue, and the "sync now" button becomes an optional manual trigger on top of that, not the primary mechanism.
- **Per-tenant Gemini free-tier keys as the default provider.** Great for demos and pilots; the repo's own comments already flag its rate limits as the reason `azure_openai`/`anthropic` alternate paths exist. **Overwrite note:** treat Gemini-per-tenant as the "trial tier" default, not the production default, once a client's ingestion volume is real and continuous.

None of these require touching working code today — they're flagged so the roadmap in Part 3 doesn't accidentally build v2/v3 features on top of an assumption (single connector type, sub-1.0 Anthropic SDK, free-tier rate limits) that's already known to need replacing.

---

## Part 3 — Versioned Implementation Roadmap

Each version lists its goal, concrete deliverables, and exit criteria (how to know it's actually done, not just started). Versions build strictly on the previous one; nothing here proposes re-doing work that already exists and works.

### v0 — Current state (baseline, already built)

**Goal:** none — this is what exists today and what everything else builds on.

Ontology (core + 9 domain packs + customer extension) validated and tested. Neo4j persistence and Graphiti temporal-fact tracking implemented and tested. File- and single-web-page ingestion working, ontology-constrained. Graph-based retrieval working; semantic and live-data retrieval stubbed. Context assembly produces a plain-text summary with source facts; structured `ContextPacket` fields mostly unpopulated. Multi-tenant API-key auth, `group_id` isolation, org-hierarchy-scoped visibility, and local LLM spend caps all implemented. Document Sets and web-page Connectors (single type) implemented. Basic chat frontend, deployed to Azure Container Apps against Neo4j Aura.

### v0.5 — Hardening & connector SDK groundwork

**Goal:** make the current single connector type (`web`) into a real, pluggable pattern before adding more types to it, so v1 adds connectors instead of also redesigning the abstraction.

- Generalize `app/graph/connectors.py`'s `Connector` model and `app/api/connectors.py`'s dispatch (`_SUPPORTED_TYPES`) into a small connector interface: `fetch() -> list[NormalizedRecord]`, `content_hash()`, `source_description()` — same shape `web` already has, made explicit and reusable.
- Generalize the content-hash dedup-before-extraction pattern (currently web-connector-only) so every future connector type gets it for free.
- Resolve or work around the `anthropic<1.0.0` pin if Claude is going to be used anywhere in the ingestion path for testing this milestone (see compatibility notes above) — don't defer this past the point where it blocks real work.
- Add integration tests for the connector interface itself (not just the web connector), so new connector types have a contract to test against.

**Exit criteria:** a new connector type can be added by implementing the interface and registering it in one dispatch table, with no changes to `IngestionPipeline`, ontology handling, or the API route.

### v1 — First live connectors, scheduled sync

**Goal:** prove ingestion from a real structured source and a real unstructured source, on a schedule, not just on manual click.

- Add one structured connector (a client's Postgres/MySQL database, or a CRM's REST API) using the v0.5 interface, converting rows to prose via the existing `structured.py` pattern.
- Add one unstructured connector (Gmail or Microsoft Graph for email) with basic attachment/body text extraction.
- Replace "sync now" as the *only* mechanism with a scheduler (APScheduler is enough at this stage — no need for Celery/RQ yet) running incremental syncs on a per-connector interval, tracking a cursor (e.g., `updated_at` watermark or Gmail history ID) so each run only pulls what's new.
- Extend `record_sync_result`/connector status tracking to surface last-sync timestamps and errors in the (still basic) frontend, so a client can see their connectors are alive.

**Exit criteria:** two live source types stay current in the graph without a human clicking "sync," each only re-processing genuinely new/changed data, within the existing per-tenant spend budgets.

**Status:** `database`, `documents`, and `email` connector types exist and work end to end (`app/ingestion/database_source.py`, `document_source.py`, `email_source.py`), each reading a small bundled demo dataset rather than a live source, since real per-client CRM/inbox credentials weren't available yet — see `data/samples/mock_*`. Scheduling is now built: `app/graph/connector_scheduler.py` runs every tenant's connectors on an interval (`CONNECTOR_SYNC_INTERVAL_MINUTES`, default 15) via APScheduler's `AsyncIOScheduler`, started/stopped from `app/main.py`'s lifespan — the manual "Sync now" route and the scheduler both call the same extracted `run_connector_sync()` (`app/ingestion/connector_sync.py`), so they can't drift apart. Runs in-process per container instance; a real multi-instance deployment should move this to a dedicated worker (see v2's "worker pool" step) rather than every instance scheduling independently — noted in that module's own docstring.

### v1.5 — Unified connector platform for broad source coverage

**Goal:** stop hand-writing a connector per client system — this is the step that makes "any and all data sources" actually tractable and cost-effective rather than an ever-growing bespoke-integration backlog.

- Stand up Nango (self-hosted, open-source) as the source-of-record for OAuth, polling/webhook scheduling, and per-source normalization across its ~900 prebuilt API connectors (CRM, ERP, HRIS, accounting, documents, email, and more).
- Write one adapter layer that takes a Nango-normalized record and hands it to the same `structured.py`/prose-conversion + `IngestionPipeline` path every other source already uses — Nango replaces "who do I poll and how do I auth," not "how does this become a graph fact."
- Keep the v1 hand-written connectors as-is for any source Nango doesn't cover, or where a client requires direct, non-Nango access for compliance reasons — this is additive, not a rip-and-replace.
- Re-run the v0.5 connector-interface tests against a Nango-backed connector to confirm the abstraction held.

**Exit criteria:** onboarding a new client system that already has a Nango connector is a configuration/mapping change, not new integration code.

**Status — deliberate substitution:** standing up Nango itself (a separate hosted service, plus per-client OAuth app registrations) was judged too much new infrastructure to take on before a single real live connector had been proven end to end. Instead, `google_drive` (`app/ingestion/google_drive_source.py`) was built as one real, direct connector — no mock data, a genuine live external source — authenticating as a Google Cloud **service account** (`GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`, see `.env.example`) rather than per-user OAuth, since a service account needs no interactive consent flow and is what a server-side "Sync now"/scheduled sync actually needs. It only ever sees folders explicitly shared with its own email address. Reads plain text, Markdown, CSV, PDF, Word (`.docx`), and Google Docs/Sheets/Slides (via `pypdf`/`python-docx` for the binary formats — a scanned/image-only PDF has no text layer to extract and is silently skipped, not OCR'd). This satisfies v1.5's underlying goal ("prove a real live source works, cheaply") without its literal Nango deliverable. **Overwrite note:** Nango (or an equivalent unified platform) is still the right answer once *breadth* of sources matters more than proving the pattern once — don't read this substitution as "Nango isn't needed," just "it wasn't the next-cheapest proof." `google_drive`'s own interface (`SourceConnector`) is exactly what a future Nango adapter would also implement, per v0.5's exit criteria, so nothing here needs to be redone to add Nango later.

### v2 — Continuous, low-latency ingestion

**Goal:** move from "polled on a schedule" to "reflects change in near real time" for sources that need it, and decouple capture from extraction so ingestion spikes don't spike LLM spend.

- Add a change queue (Redis Streams, or SQS if the deployment target is AWS) between "something changed" and "run extraction on it" — every connector (Nango webhook, CDC event, scheduled poll) writes to the queue rather than calling extraction inline.
- Add Debezium-based CDC for structured databases where a client needs sub-minute freshness and has granted direct database (logical replication) access — distinct from the Nango/API-polling path, for the subset of sources where that level of freshness matters.
- Add webhook-based push where the source supports it (Nango's own webhook relay, Gmail/Graph push notifications) rather than polling everything.
- Add a worker pool consuming the queue with rate limiting and batching, tuned against the existing per-tenant/per-bucket spend caps.

**Exit criteria:** a change in a connected live source appears in the graph within the target latency for that source type (seconds for webhook-driven sources, minutes for CDC/polled sources), and ingestion volume spikes don't produce a corresponding spike in concurrent LLM calls.

### v2.5 — Cross-source entity resolution & reconciliation

**Goal:** close the specific gap the repo's own roadmap already identifies — Graphiti's semantic dedup is good within a source's own vocabulary, but doesn't reliably know a CRM's `contact_id: 4471` and an email connector's `sarah@contoso.com` are the same person unless something tells it so.

- Add an explicit "Reconcile" step in the ingestion path (matching the reference architecture's own naming) that runs deterministic identity matching where a real key exists (email address, external system ID, domain) before an episode reaches Graphiti.
- Only reach for probabilistic matching (e.g., a library like Splink) where no deterministic key exists — most cross-CRM/ERP/email matching in practice does have one.
- Store the resolved cross-source identity as part of the entity's evidence/provenance, so "this is the same Sarah Chen across three systems" is itself a sourced, inspectable fact, not a silent merge.

**Exit criteria:** a person or organization that appears across two or more connected sources resolves to one graph entity with multiple sourced identifiers, verifiable by inspecting that entity's evidence.

**Status — MVP-scoped reconciliation shipped, full version deferred:** `GraphRepository._match_entities_by_name`/`_resolve_named_entities`/`search_graphiti_facts` now reconcile an *exact* (case-insensitive) name match across every connector in a query's scope — e.g. "Fenwick & Cole Legal" mentioned in both a CRM record and a separate email now pools facts from both, rather than only whichever node happened to be picked first. This is deterministic, real reconciliation, but on a weaker signal than the spec's own plan (normalized name equality, not a shared key like an email address or external system ID — no such shared key exists in the current mock/live data yet). The known tradeoff: two genuinely different entities sharing an exact name would incorrectly merge. Loose (CONTAINS) matches are deliberately NOT reconciled this way, to avoid re-introducing the padded-results problem an earlier fix removed. Revisit with a stronger signal (e.g. a real shared identifier field) once one exists in the data, per this version's original plan.

### v3 — Token-efficient query layer & the answer/evidence UI

**Goal:** this is where the product experience described in Part 1 (one answer, expandable full sourcing) actually ships, and where per-query cost gets actively minimized rather than just not-yet-a-problem.

- Implement `app/context/planner.py` (currently a stub) as a cheap up-front classifier deciding which retrievers a given query actually needs, so semantic/live-data retrieval only runs when the graph retriever's result is insufficient.
- Implement `SemanticRetriever` on Graphiti's built-in hybrid search (semantic + BM25 + graph traversal) rather than standing up a separate vector database.
- Extend `ContextPacket` so the API returns structured, per-fact source attribution (which connector/document/message, what timestamp, current vs. superseded) rather than only a flattened text summary — this is the actual data the frontend's "expand for sources" panel needs.
- Wire in Anthropic prompt caching for the ontology schema and any per-tenant system prompt used in extraction/synthesis, for tenants on the Claude provider path.
- Add a semantic response cache (query → recent answer, scoped per tenant, invalidated on relevant graph changes) to avoid repeat retrieval+synthesis spend on repeat questions.
- Update the frontend to render the answer with a click-to-expand sources section, driven by the extended `ContextPacket`.

**Exit criteria:** a query's cost is visibly lower for a question the graph retriever alone can answer, and every answer in the UI can be expanded to show the exact facts, sources, and timestamps behind it.

**Status:**
- The planner + semantic-search bullets turned out to already be substantially met by the existing design, just organized differently than this plan assumed: `GraphRepository.search_graphiti_facts()` already tries named-entity resolution first and only falls back to Graphiti's built-in hybrid search (semantic + BM25 + graph traversal) when that doesn't resolve — which *is* the "only run semantic search when the graph result is insufficient" decision a separate `ContextPlanner` would have existed to make, and *is* "semantic search on Graphiti's hybrid search, not a separate vector database." The early-scaffolded `app/context/planner.py`, `app/context/ranker.py`, `app/context/composer.py`, `app/retrieval/semantic_retriever.py`, and `app/retrieval/live_data_retriever.py` (never wired into the real pipeline, verified via a repo-wide import search) were removed rather than kept as misleading placeholders — see `app/context/orchestrator.py`'s module docstring for the real architecture.
- Per-fact source attribution is done: every fact returned by `search_graphiti_facts` now carries `group_id` (which connector/knowledge base it actually came from), threaded straight through to the API response and shown in the frontend as a small "from X" tag next to each fact — only when a Document Set (multiple possible sources) is the active scope, since it's redundant noise on a single-connector query.
- Anthropic prompt caching is done: `app/graph/caching_anthropic_client.py`'s `CachingAnthropicClient` subclasses graphiti_core's `AnthropicClient` to send the system prompt as a `cache_control`-marked block (graphiti_core sends it as a plain string, which can never be cached) and is now what `llm_provider="anthropic"` actually builds. The local spend limiter (`app/graph/spend_limiter.py` via `graphiti_adapter._apply_spend_limit_anthropic`) was updated in the same change to count `cache_creation_input_tokens`/`cache_read_input_tokens` toward estimated spend — those aren't part of the plain `input_tokens` figure, so leaving them out would have made the spend estimate silently wrong the moment caching turned on.
- A semantic response cache (query → recent answer) is NOT built — genuinely separate, lower-priority scope from the above.
- The click-to-expand frontend sources section is DONE in spirit (the bulleted fact list with current/superseded badges and now per-fact source tags), not built as a separate collapsible "expand for sources" affordance layered on top of the answer.

### v3.5 — Expose the context graph as an MCP server

**Goal:** this is the literal "Context API/MCP" layer from the reference architecture — make a client's consolidated graph queryable by any MCP-capable agent (Claude Desktop, Claude Code, Copilot, a custom agent), not only through Saxon's own chat UI.

- Stand up an MCP server (Graphiti already ships a reference implementation to build from) wrapping the same tenant-scoped, authorization-aware query path `/api/v1/context/query` already uses — same API key and `as_user` scoping, no separate auth model.
- Expose it as a small number of well-described tools (e.g., "query this client's context graph," "look up an entity's current facts and history") rather than raw Cypher access, keeping the ontology-and-authorization guarantees intact.
- Document the MCP connection for a client the same way `X-API-Key` is documented today.

**Exit criteria:** Claude (or another MCP client) can be pointed at a tenant's Saxon instance and answer questions grounded in that tenant's graph, with the same tenant isolation and role-based visibility the HTTP API already enforces.

### v4 — Enterprise hardening & scale

**Goal:** the items already explicitly flagged as "not needed yet, but known to be needed eventually" throughout this spec and the repo's own README.

- Per-tenant Neo4j databases (Aura Enterprise) for clients whose compliance requirements outgrow shared-database + `group_id` isolation.
- Replace `config/tenants.json`-file tenant administration with a real admin plane (backed by a small Postgres instance, most likely), including the ontology-authoring UI already on the repo's own roadmap.
- Observability: structured tracing of which retrievers ran and why (from the v3 planner), per-query cost, and per-connector sync health, surfaced to both the operator and the client.
- Formalize per-tenant usage-based billing on top of the existing `spend_limiter.py` accounting.
- Deploy for real: pair the existing Azure Container Apps script with Neo4j Aura (network-reachable by default) — already in place.

**Exit criteria:** a new enterprise client can be onboarded, isolated, billed, and monitored without operator intervention beyond initial setup.

---

### A note on sequencing

The order above is deliberate: v1/v1.5 (breadth of sources) come before v2 (real-time freshness) because a live connector to one source is more valuable to prove the model than sub-second latency on that same one source. v2.5 (entity resolution) comes after v2 (continuous ingestion) rather than before, because reconciliation only becomes a visible problem once multiple live sources are actually flowing into the same graph. v3 (token efficiency + UI) comes after ingestion breadth is real, because optimizing query cost against a graph that only has one manually-ingested file in it doesn't tell you anything about production cost. This ordering can compress — v1 and v1.5 in particular could run together if a first client's needs point straight at Nango-covered systems — but skipping straight to v3/v4 features on top of v0's manual-ingestion-only graph would mean building the token-efficiency and MCP layers against a shape of data the real product will never actually have.
