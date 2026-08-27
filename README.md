# Saxon AI Context Engine

## Overview

Companies have knowledge scattered across a CRM, an ERP, spreadsheets, emails,
support tickets, and maintenance logs. When an AI assistant is connected to a
business, its answers are only as good as the information it's given. Without
context it guesses. Handed a raw dump of every system's exports, it gets
confused or gives a confident but wrong answer.

The Context Engine sits between a company's scattered systems and an AI
assistant, and does the organizing work in between. It:

- Structures facts consistently instead of leaving them as a pile of raw text.
- Keeps history, not just current state, so it knows an account used to be
  managed by one person and is now managed by someone else, rather than only
  knowing today's answer with no memory of the change.
- Attaches sources to every fact, so an answer can be traced back to the
  document, record, or message it came from -- and, once a client is
  connected to more than one source, which one.
- Adapts to each client's vocabulary through configuration. A hospital, a
  factory, and a law firm each use different terms for similar concepts. The
  underlying structure stays the same across clients; the vocabulary is
  swapped in as config rather than rewritten as code.
- Pulls from a client's own live systems continuously (a CRM, a Google Drive
  folder, a SharePoint site, email) rather than only ever being fed a
  one-time file export, and stays current on its own without anyone manually
  re-triggering it.
- Is queryable by any MCP-capable AI agent (Claude Desktop, Claude Code,
  Copilot, a custom agent), not only through this project's own chat UI.

The goal is for this layer to be built once and reused across client
engagements, instead of every project starting from scratch on getting data
into a shape an AI can use.

**Live demo:** https://saxon-context-engine.kindsea-5648017b.southindia.azurecontainerapps.io/ui
(needs an access key -- see [Adding a client's API key](#adding-a-clients-api-key)).

## How it works

Think of a corkboard with pins and string. Each pin is a thing: a person, a
company, a machine, an order. Each string connecting two pins is a
relationship, for example this person manages this account, or this machine
is located at this factory. That corkboard is the graph.

Now give every pin and string a sticky note that records when it became true,
and whether it's still true today. That's what makes this a "temporal" graph.
Nothing gets erased when it changes; it gets marked out of date, and the new
fact is added alongside it. Ask "who manages this account" and the system can
give the current answer and the fact that it used to be someone else.

The flow, start to finish:

1. Something happens in the real world and gets written down somewhere: a CRM
   note, an email, a maintenance log entry, a file in a Drive folder or
   SharePoint site. A source connector picks it up, on its own schedule --
   nobody has to remember to feed the system a fresh export.
2. Ingestion takes that text and hands it to an LLM, which reads it and pulls
   out entities and facts. "Contoso Ltd is a customer." "Sarah Chen manages
   the Contoso account."
3. Before anything is stored, it's checked against the ontology, the rulebook
   that says what kinds of things ("Organization", "Person", "Event") and
   what kinds of relationships ("MANAGES", "OWNS", "LOCATED_AT") are allowed
   to exist. This keeps the graph consistent instead of accumulating one-off,
   inconsistent labels.
4. The extracted facts are written into Neo4j, a database built for storing
   this kind of pins-and-string data (a "graph database"), with each fact
   tagged with when it became true and which connector it came from.
5. Later, someone or something asks a question -- through this project's own
   API/UI, or through an MCP-capable AI agent connected directly. Retrieval
   first checks whether the question names a specific, known entity (skipping
   straight to that entity's own facts, at no extra cost); only if that
   doesn't resolve does it fall back to a broader search across the graph,
   matching by meaning rather than exact keywords, and respecting time so
   only currently true facts are returned unless history is specifically
   requested.
6. The relevant facts are assembled into a context packet: a structured
   bundle of exactly the relevant information, with sources attached, which
   is what actually gets handed to the AI assistant answering the question.
   A short synthesized answer is generated on top of it when more than one
   fact is involved; a repeat question against an unchanged graph is served
   straight from a short-lived cache instead of paying for retrieval and
   synthesis again.

## Current status

This is a working, deployed, multi-tenant system, not a prototype. Live
connectors pull from a client's own CRM/database, documents, email, Google
Drive, SharePoint, and arbitrary web pages, on an automatic schedule as well
as on demand; cross-source entity resolution pools facts about the same
real-world thing when it appears under the same name in more than one
source; and the whole thing is queryable both through this project's own API
(and demo chat UI) and through any MCP-capable AI agent. See
[Status](#status) below for a layer-by-layer breakdown, and
[`CLAUDE.md`](CLAUDE.md) (the internal product/technical spec this build
follows, continuously updated with a status note per section) for the full
version-by-version history of what was built and what was deliberately
substituted along the way.

## Setup

### Prerequisites

- Python 3.11+
- [Neo4j](https://neo4j.com/). The easiest way to run one locally is
  [Neo4j Desktop](https://neo4j.com/download/): install it, create a local
  database, and start it. [Neo4j AuraDB Free](https://neo4j.com/cloud/aura-free/)
  works too, and is what a real deployment needs (Neo4j Desktop isn't
  reachable from a hosted deployment).
- A [Google Gemini API key](https://aistudio.google.com/), used for
  embeddings/reranking regardless of which `LLM_PROVIDER` you choose, and for
  extraction/chat too if you stay on the default `gemini` provider. Gemini
  has a usable free tier.

### Install and configure

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with your Neo4j connection details and Gemini API key
```

The minimum `.env` to get running locally:

```
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<your Neo4j password>
GOOGLE_API_KEY=<your Gemini API key>
```

`.env.example` has everything else, documented inline: switching
`LLM_PROVIDER` to `azure_openai` or `anthropic`, local spend caps for either,
credentials for the live Google Drive/SharePoint connectors, background
connector-sync tuning, and the MCP server's allow-listed hostnames. None of
it is required to run the app locally with the default `gemini` provider and
only the built-in demo/file-based connectors.

## What you can do today

This walks through what actually works right now, in order, with what each
step's output means. Run them in this order the first time; each one builds
on the last. Each ingestion/search step calls the Gemini API a couple of
times, so this whole sequence stays well within the free tier's rate limit.

### 1. Confirm the database connection

```bash
python scripts/test_neo4j.py
```

Expected output: `Neo4j connection successful`. This only checks that the
app can reach Neo4j with the credentials in `.env`. It doesn't touch
Graphiti, the LLM, or the ontology at all, so it's the first thing to run
when something else fails; if this fails too, the problem is Neo4j or `.env`,
not the rest of the app.

### 2. Validate the ontology

```bash
python scripts/check_ontology.py
```

Expected output:

```
OK: ontology\core.yaml
OK: ontology\domains\finance.yaml
...
Entity types: 41
Relationship types: 60
```

This loads and merges every ontology YAML file (core plus all nine domain
packs), the same way the app does at startup. It doesn't touch Neo4j. If
you've edited or added a domain file, this is how to check it's valid before
running anything else.

### 3. Set up Neo4j's indices

```bash
python scripts/init_neo4j.py
```

One-time setup so Neo4j can look things up quickly. Safe to run again later;
it just re-applies the same schema. (The API itself also does this on every
startup for the indexes newer features depend on -- role-based visibility,
document sets, connectors -- so this step matters most for a completely
fresh database before you've ever run the app.)

### 4. Ingest one sentence and ask about it

```bash
python scripts/seed_core_graph.py
```

This is the smallest real end-to-end example: it sends one sentence to
Graphiti, waits for the LLM to extract entities and facts from it, and then
asks a question about what it just stored. A run against a database that
already has some data in it looks like:

```
--- Seeding quickstart core episode ---
Fact: Ananya set up the Saxon AI Context Engine with Graphiti and Gemini.
Fact: Sarah Chen manages the enterprise customer account for Contoso Ltd.
Fact: Contoso Ltd's account is managed by Marcus Lee
```

What this shows: the sentence you fed in came back out as a fact (proving
ingestion and extraction work), and older facts already in the database
also came back if they were relevant to the question asked (proving search
works, not just storage).

### 5. Watch a fact get superseded, not overwritten

```bash
python scripts/test_graph.py
```

This is the core feature demo. It ingests that a CRM account is managed by
Sarah Chen, ingests an unrelated order from an ERP system, then later
ingests that the account is now managed by Marcus Lee instead. Querying
"who manages this account" afterward returns both facts, one marked valid
and one marked invalidated:

```
[VALID] Contoso Ltd's account is now managed by Marcus Lee, not Sarah Chen.
[INVALIDATED] The account is managed by Sarah Chen.
```

Neither fact was deleted. The old one is still in the graph, just marked as
no longer current. This is what makes the system useful for questions like
"who used to manage this account" as well as "who manages it now."

### 6. Query it over the API

```bash
uvicorn app.main:app --reload
```

Then, in another terminal:

```bash
curl -X POST http://localhost:8000/api/v1/context/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: local-dev-key" \
  -d '{"query": "who manages the Contoso account"}'
```

Expected output, a `ContextPacket` whose `metadata` carries the synthesized
answer plus the individual sourced facts behind it:

```json
{
  "query": "who manages the Contoso account",
  "entities": [], "relationships": [], "facts": [], "events": [], "evidence": [], "timeline": [],
  "confidence": null,
  "metadata": {
    "group_ids": ["acme_demo"],
    "summary": "Contoso Ltd's account is managed by Marcus Lee.",
    "facts": [
      {"fact": "Contoso Ltd's account is now managed by Marcus Lee.", "is_valid": true, "group_id": "acme_demo", "valid_at": "...", "invalid_at": null},
      {"fact": "The account is managed by Sarah Chen.", "is_valid": false, "group_id": "acme_demo", "valid_at": "...", "invalid_at": "..."}
    ],
    "retrieval_path": "entity_resolution",
    "cache_hit": false,
    "cost_usd": 0.0
  }
}
```

`entities`/`relationships`/`facts`/`events` at the top level are still
unpopulated (see [Status](#status)) -- everything an API caller actually
uses today lives in `metadata`: the synthesized `summary`, the individual
sourced `facts` (each tagged current/superseded and, once a query spans more
than one connector, which one it came from), and a small observability
block (`retrieval_path`, `cache_hit`, `cost_usd`) describing how the answer
was produced. Or open `http://localhost:8000/ui` for the same thing as a
demo chat interface instead of raw JSON.

Run the test suite at any point (the ontology and model tests don't need
Neo4j running; most of the rest do):

```bash
pytest
```

## Ingesting data

The steps above ingest one hand-written sentence at a time, and
[Source connectors](#source-connectors) below is how a real client's data
gets in continuously. For loading one of this repo's own bundled sample
datasets in bulk (useful for a demo, or for trying the ontology against a
larger dataset), `scripts/ingest_samples.py` reads the files in
`data/samples/`, turns each row or document into a sentence, and extracts
entities and facts from it against the ontology.

```bash
# See what would be ingested, without calling the LLM or writing anything
python scripts/ingest_samples.py northwind --dry-run

# Ingest for real (northwind | manufacturing | legal)
python scripts/ingest_samples.py northwind --limit 10
```

Each record costs several LLM calls (extraction, embedding, deduplication),
not one, and Gemini's free tier caps requests per minute, so the script
defaults to 20 records per run with a 15-second gap (`--limit`, `--delay`).
Expect to hit the limit anyway on the free tier: a run at a 4-second delay
failed 6 of 10 records in testing. That's recoverable rather than
destructive. Successfully ingested records are tracked in
`data/processed/ingest_log.json`; failures are not marked, so re-running the
same command retries exactly what failed and skips what already succeeded.
Use `--group-id` to keep a run's data in its own bucket.

Extraction is constrained to the ontology: the entity and relationship types
from `ontology/core.yaml` plus the dataset's domain pack are passed into the
extraction prompt, so the model picks from types the ontology defines rather
than inventing its own. This matters more than it sounds. Ingesting the same
Northwind customers without it produced `HAS_COMPANY_NAME`,
`LOCATED_IN_CITY`, and `LOCATED_IN_COUNTRY` alongside the ontology's own
`LOCATED_AT` and `OWNS` -- six of eight relationship types were invented, and
a graph like that can't be queried consistently. With the ontology passed in,
all of them came from the ontology.

To add a dataset of your own, describe its files with a `FileSourceSpec` (see
the `NORTHWIND_SPECS` list in the script) and add an entry to
`DATASET_DOMAINS` naming which ontology domain pack it extracts against.

### Experimenting

- Open Neo4j Desktop's Neo4j Browser against your running database and run
  `MATCH (n) RETURN n LIMIT 50` to see the graph after running the scripts
  above.
- Edit `scripts/seed_core_graph.py` or `scripts/test_graph.py` with your own
  sentences to see what gets extracted from different kinds of text.
- Add a new entity type to `ontology/domains/manufacturing.yaml` (or any
  other domain file) following the pattern in
  [`ontology/README.md`](ontology/README.md), then rerun
  `python scripts/check_ontology.py` to confirm it's valid.
- Start the API and try the demo UI in a browser:

  ```bash
  uvicorn app.main:app --reload
  ```

  Visit `http://localhost:8000/ui` for the demo chat interface, or
  `http://localhost:8000/docs` for interactive API docs, or
  `http://localhost:8000/api/v1/entities` for the full list of entity and
  relationship types the ontology currently defines. Every `/api/v1/*` route
  requires an `X-API-Key` header; see [API access](#api-access) below.

## Source connectors

`POST /api/v1/connectors` configures a link to an external source and ties
it to one of a tenant's knowledge bases; `POST /api/v1/connectors/{id}/sync`
(or the demo UI's "Sync now" button) pulls it in on demand, and every
connector also re-syncs on its own automatically (`CONNECTOR_SYNC_INTERVAL_MINUTES`,
default 15 -- see `app/graph/connector_scheduler.py`), so a client doesn't
have to remember to keep re-triggering it. A sync only re-runs (paid)
extraction when the source's content has actually changed since last time
(a content-hash check), and the manual "Sync now" trigger itself returns
immediately (`202 {"queued": true}`) rather than blocking on a large
folder's worth of extraction calls -- it's handed to a small in-process
worker queue (`app/graph/ingestion_queue.py`), and the connector's own
`status`/`health` in `GET /api/v1/connectors` is where the real outcome
shows up a moment later.

| Type | What it reads | Auth |
|---|---|---|
| `web` | A single web page | none -- just fetches the URL (SSRF-guarded: rejects loopback/private/link-local/reserved/multicast targets and redirects, so a connector can't be pointed at internal infrastructure) |
| `google_drive` | Plain text, Markdown, CSV, PDF, Word (`.docx`), Google Docs/Sheets/Slides in a shared Drive folder | a Google Cloud service account (`GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`); the folder must be explicitly shared with that account's email first |
| `sharepoint` | Plain text/Markdown/CSV/PDF/`.docx` files in a site's default document library | an Azure AD app registration via OAuth2 client credentials (`SHAREPOINT_TENANT_ID`/`CLIENT_ID`/`CLIENT_SECRET`) -- note this grant is org-wide (`Sites.Read.All`) once admin-consented, not per-site like Drive's sharing model |
| `gmail` | Recent messages in one Gmail inbox | the same Drive service account, but a mailbox can't be per-folder-shared -- a Workspace admin must grant it domain-wide delegation for `gmail.readonly`; doesn't work against a personal (non-Workspace) Gmail account |
| `outlook_mail` | Recent messages in one Microsoft 365 mailbox's inbox | the same SharePoint app registration, plus the `Mail.Read` Graph application permission, admin-consented, alongside `Sites.Read.All` |
| `database` | Every CSV bundled under `data/samples/mock_crm/` | none -- demo data, not a live source; drop in your own CSV(s) and it infers id/name columns for anything that isn't the original `accounts.csv` |
| `documents` | Every `.txt`/`.pdf`/`.docx` bundled under `data/samples/mock_docs/` | none -- demo data, not a live source |
| `email` | A small bundled demo inbox | none -- demo data, not a live source |

A scanned/image-only PDF has no extractable text layer and is silently
skipped, not OCR'd. Legacy binary `.doc` (not `.docx`) isn't supported.

Facts from different connectors about the same real-world thing (a CRM
record and a document both mentioning "Fenwick & Cole Legal", say) are
pooled together when the name matches exactly -- see
`GraphRepository._match_entities_by_name` -- so a question spanning sources
gets one combined answer instead of whichever source happened to be checked
first. This is deliberately name-based, not a stronger shared-key match
(an email address, an external system ID); two different entities that
happen to share an exact name would incorrectly merge under this today.

**Document sets** (`POST /api/v1/document-sets`) group several connectors
under one name, so a question can be scoped across all of them at once
(`document_set` in a query) instead of picking exactly one connector every
time.

## MCP server

Any [MCP](https://modelcontextprotocol.io)-capable agent -- Claude Desktop,
Claude Code, GitHub Copilot, a custom agent -- can query a tenant's
consolidated graph directly, with the same access this project's own API
has, not a separate integration. Point an MCP client (streamable HTTP
transport) at:

```
POST https://<this deployment>/mcp
X-API-Key: <the tenant's own access key>
```

Two tools are exposed:

- **`query_context_graph`** -- ask a question in plain language, get back a
  synthesized answer plus the exact sourced facts it's built from. Takes the
  same `knowledge_base`/`document_set`/`as_user` scoping options as
  `POST /api/v1/context/query`, because it's the same underlying
  implementation (`app/context/query_service.py`) -- an MCP query and an API
  query against the same scope can never drift apart.
- **`list_available_sources`** -- lists the tenant's own knowledge bases and
  document sets, so an agent can discover what it's allowed to query instead
  of guessing an id.

Authentication is the calling agent's own `X-API-Key` header, checked
against the exact same tenant lookup the HTTP API uses (`app/security.py`) --
there's no separate MCP auth model, and a missing/invalid key is rejected
before any tenant data is touched. Locally, run against `http://localhost:8000/mcp`;
see `MCP_ALLOWED_HOSTS` in `.env.example` if you're pointing an MCP client at
a deployment with a different hostname than the default (`localhost:8000`) --
the MCP SDK's own DNS-rebinding protection rejects any other Host header.

## API access

Neo4j's free (Community) edition doesn't support separate databases per
client the way its paid edition does, so one client's data is kept separate
from another's within a single database using Graphiti's `group_id`. On its
own that separation is only as strong as whatever calls the API: if a caller
could put any `group_id` it wanted directly into a request, the separation
would be advisory rather than enforced.

To close that gap, `POST /api/v1/context/query` (and the `GET /api/v1/graph/*`,
`/api/v1/connectors`, and `/api/v1/document-sets` routes, and the MCP server
above) require an `X-API-Key` header. `app/security.py` looks the key up in
`config/tenants.json` and returns that tenant's config, including the list of
knowledge bases (`group_id`s) it's allowed to query. An invalid or missing key
is rejected before any Neo4j or LLM call is made.

A tenant can have more than one knowledge base -- e.g. one API key that can
switch between "Contoso" and "Northwind" datasets -- so requests may include
an optional `knowledge_base` (defaulting to the tenant's first one if
omitted), or `document_set` to search across several knowledge bases at
once under one name (see [Source connectors](#source-connectors)). Either
value is still validated against *that tenant's own* list before it's used:
a caller can pick among its own datasets, but can never name a `group_id`
outside its own list.

```bash
# List the knowledge bases this key can query
curl http://localhost:8000/api/v1/graph/knowledge-bases -H "X-API-Key: local-dev-key"

curl -X POST http://localhost:8000/api/v1/context/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: local-dev-key" \
  -d '{"query": "who manages the Contoso account", "knowledge_base": "acme_demo"}'
```

A repeat (or near-repeat, whitespace/case-normalized) question against the
same scope within a short window (`RESPONSE_CACHE_TTL_SECONDS`, default 5
minutes) is served from an in-process cache instead of re-running retrieval
and synthesis -- see `app/context/response_cache.py`. It's invalidated the
moment a connector sync actually changes data for a group, so a fresh sync
doesn't leave a stale cached answer sitting around for the rest of the TTL.

Each tenant also brings their own Gemini API key rather than sharing one
operator-owned key (unless `LLM_PROVIDER` is switched to `azure_openai` or
`anthropic`, which use one shared operator-owned credential instead --
see `.env.example`). Building a Graphiti client (LLM + embedder + reranker
setup) is real overhead, so this isn't done fresh on every request: each
tenant gets one client (shared across all of that tenant's knowledge bases,
since `group_id` is just a data partition, not a separate connection), built
on their first request and cached after that -- see
`app/graph/tenant_graphiti_pool.py`.

This is a reasonable baseline for one shared database serving multiple
clients, not the strongest possible guarantee. Full separation would mean a
dedicated database or deployment per client, which costs more in
infrastructure but removes any risk of an application bug leaking one
client's data into another's. Worth revisiting if a client's compliance
requirements call for it.

### Role-based visibility

`knowledge_base` controls *which dataset* a request sees; `as_user` controls
*how much of it*. A given knowledge base can have an org hierarchy seeded
into it, and a request scoped to a specific person only sees what's assigned
to them plus what's assigned to everyone who reports to them -- a rep sees
their own accounts, their manager sees the rep's accounts too, and so on up
the chain. Omitting `as_user` (the default) returns the whole knowledge base,
unfiltered. `as_user` isn't supported together with `document_set` yet --
role-based visibility is scoped to one knowledge base's org chart at a time.

This is deliberately a separate layer from Graphiti's own fact graph, not
another ontology domain. Who-reports-to-whom and who-owns-what is exact
organizational data -- the kind a real deployment gets from an HR/CRM sync or
an admin action -- not something to have an LLM infer from text. So instead
of extracted entities, it's `:User` nodes and `:REPORTS_TO`/`:ASSIGNED_TO`
edges written directly via Cypher (see `scripts/seed_roles.py`), scoped to a
knowledge base's `group_id` like everything else. `app/graph/authorization.py`
enforces it at query time, and validates a request's `as_user` against that
knowledge base's own org chart the same way `resolve_knowledge_base` validates
`knowledge_base` -- a caller can pick anyone in the chart it can already see,
never an id from a knowledge base it can't.

This is built to stay cheap regardless of how large the knowledge base gets:
resolving "who does this person outrank" only ever traverses the `:User`
subgraph (sized to the org -- hundreds or thousands of people), never the
business-entity graph itself, so the expensive dimension (how many customers,
orders, contracts exist) never enters into that part of the query.

```bash
# Everyone in contoso_dw's seeded org chart, with manager_id so a client can
# render it as a hierarchy
curl "http://localhost:8000/api/v1/graph/users?knowledge_base=contoso_dw" \
  -H "X-API-Key: local-dev-key"

# The CRO sees the whole knowledge base; a rep sees only their own accounts
curl "http://localhost:8000/api/v1/graph/summary?knowledge_base=contoso_dw&as_user=jordan_blake" \
  -H "X-API-Key: local-dev-key"
curl "http://localhost:8000/api/v1/graph/summary?knowledge_base=contoso_dw&as_user=diego_ramirez" \
  -H "X-API-Key: local-dev-key"
```

Seed an org chart for a knowledge base with `python scripts/seed_roles.py`
(currently hardcoded to `contoso_dw`'s own already-ingested customers --
adapt the `USERS`/`ASSIGNMENTS` lists at the top of that script for another
knowledge base or a real org chart).

### Adding a client's API key

No code editing or hand-written JSON required -- use `scripts/manage_tenants.py`,
which reads and writes `config/tenants.json` for you:

```bash
# Add a new client, generating a random API key for them. This also creates
# their first knowledge base, using the same id as the tenant.
python scripts/manage_tenants.py add --name "Acme Corp" --gemini-key <their Gemini API key>

# Give an existing client another dataset to switch between
python scripts/manage_tenants.py add-knowledge-base acme_corp --id northwind --label "Northwind"

# See who's configured (keys shown masked, not in full)
python scripts/manage_tenants.py list

# Remove a client
python scripts/manage_tenants.py remove acme_corp
```

`add` prints the generated API key once -- that's what you give the client to
put in their `X-API-Key` header. It isn't shown again by `list`, so save it
somewhere before closing the terminal. The API needs a restart to pick up any
change, since configuration is only read once at startup.

`config/tenants.json` is gitignored (it holds real API keys); see
`config/tenants.example.json` for the shape it expects if you'd rather edit
it directly. Platforms that prefer environment-variable configuration over a
file (e.g. Azure Container Apps) can set the equivalent `TENANT_API_KEYS`
environment variable instead -- see `.env.example`. The file takes priority
if both are present.

## Deployment

`Dockerfile` plus `scripts/deploy_azure.sh` deploy this to Azure Container
Apps -- the script is fully parameterized (subscription, resource group,
Neo4j connection, tenant keys, LLM provider, every optional connector
credential, all read from environment variables) with nothing
deployment-specific hardcoded, so it's reusable for a different Azure
subscription/environment, not just this project's own. It builds the image
in Azure (`az acr build`, no local Docker install needed), creates the
Container Apps environment and registry if they don't already exist, and
updates them in place (with a forced new revision, so a rotated secret's
*value* actually takes effect) if they do.

```bash
SUBSCRIPTION="your subscription name" \
RESOURCE_GROUP="your resource group" \
LOCATION="your Azure region" \
NEO4J_URI="neo4j+s://xxxx.databases.neo4j.io" \
NEO4J_PASSWORD="..." \
TENANT_API_KEYS="$(cat config/tenants.json)" \
./scripts/deploy_azure.sh
```

This project's own instance is live at the URL under
[Overview](#overview) above, backed by Neo4j AuraDB. Scale-to-zero is on
(`--min-replicas 0`), so the first request after a period of no traffic pays
a cold-start delay (roughly 30-45s) while a new instance spins up.

---

## Technical documentation

### Folder structure

```text
saxon-context-engine/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── Dockerfile
├── app/
│   ├── main.py                  # FastAPI app entry point; mounts the HTTP API and the MCP server
│   ├── config.py                # Settings, loaded from .env
│   ├── security.py              # API key -> tenant + knowledge base lookup
│   ├── models/                  # Entity, Relationship, Fact, Event, Evidence, ContextPacket
│   ├── ontology/                # Loads, validates, and merges ontology YAML files
│   │   ├── loader.py
│   │   ├── validator.py
│   │   ├── registry.py
│   │   └── bootstrap.py
│   ├── graph/                   # Neo4j connection, Graphiti integration, and app-owned graph data
│   │   ├── neo4j_client.py
│   │   ├── graph_repository.py         # Cypher + Graphiti search, including cross-source entity resolution
│   │   ├── graphiti_adapter.py         # Builds a per-provider Graphiti client (Gemini/Azure OpenAI/Anthropic)
│   │   ├── caching_anthropic_client.py # Anthropic prompt caching for the ontology schema/system prompt
│   │   ├── tenant_graphiti_pool.py     # One cached Graphiti client per tenant
│   │   ├── spend_limiter.py            # Local $ budgets on paid-provider LLM usage
│   │   ├── authorization.py            # Role-based (org-hierarchy-scoped) query visibility
│   │   ├── connectors.py               # :Connector storage (create/list/sync-result/health)
│   │   ├── connector_scheduler.py      # Background interval sync for every tenant's connectors
│   │   ├── ingestion_queue.py          # In-process queue decoupling a sync trigger from extraction
│   │   └── document_sets.py            # :DocumentSet storage (named groups of connectors)
│   ├── ingestion/                # Turning raw text/records/live sources into graph writes
│   │   ├── connector_base.py           # SourceConnector interface every connector type implements
│   │   ├── connector_sync.py           # fetch -> dedup-check -> ingest, shared by manual and scheduled sync
│   │   ├── web_source.py, google_drive_source.py, sharepoint_source.py  # real live connectors
│   │   ├── gmail_source.py, outlook_mail_source.py                     # real live mailbox connectors
│   │   ├── html_text.py                # shared HTML-to-text stripping (web pages, mail bodies)
│   │   ├── database_source.py, document_source.py, email_source.py      # demo/mock-data connectors
│   │   ├── document_text_extraction.py # shared PDF/DOCX text extraction (Drive + SharePoint)
│   │   ├── structured.py               # row -> prose conversion for structured sources
│   │   └── pipeline.py                 # Wraps a record as a Graphiti episode and ingests it
│   ├── retrieval/
│   │   ├── base.py               # Shared interface for query-based retrievers
│   │   └── graph_retriever.py    # The one retriever wired in -- wraps GraphRepository's search
│   ├── context/
│   │   ├── orchestrator.py       # Pools retriever results, synthesizes an answer, classifies retrieval_path
│   │   ├── query_service.py      # Scope resolution + cache + orchestrator, shared by the HTTP route and MCP
│   │   └── response_cache.py     # Short-TTL cache for repeat/near-repeat questions
│   ├── mcp/
│   │   └── server.py             # MCP tools (query_context_graph, list_available_sources) over the same query path
│   └── api/                     # FastAPI routes: /health, /entities, /context, /graph, /document-sets, /connectors
├── ontology/
│   ├── README.md                # Ontology design principles and layering
│   ├── core.yaml                # Enterprise-wide entity/relationship definitions
│   ├── customer-extension-template.yaml
│   └── domains/                 # Industry-specific extensions (9 packs)
├── data/                         # raw/processed/sample data
├── scripts/                      # Standalone runnable scripts, see Setup above; deploy_azure.sh for hosting
├── frontend/                     # The demo chat UI served at /ui (static HTML/CSS/JS, no build step)
├── tests/                        # pytest test suite
└── eval/                         # question/expected-answer pairs for eval work
```

### Ontology layer

`ontology/core.yaml` defines domain-neutral concepts (Entity, Organization,
Person, Event, Document, Metric...) and relationships (OWNS, MANAGES,
PART_OF...) that apply to any business. Industry-specific packs under
`ontology/domains/*.yaml` (healthcare, finance, manufacturing, retail, legal,
pharma, sales, supply chain, technology) extend that core additively: a
domain pack can specialize an existing type but can't invent an unrelated
one. A client deployment can extend further via
`ontology/customer-extension-template.yaml`. Full design in
[`ontology/README.md`](ontology/README.md).

- `app/ontology/loader.py` reads a single YAML file into a Python dict.
- `app/ontology/validator.py` checks a loaded ontology has the required
  structure.
- `app/ontology/registry.py` merges any number of validated ontology files
  (core, then domain packs, then client extensions) into one queryable
  registry.
- `app/ontology/bootstrap.py` builds the app's single registry at startup
  from `ontology/core.yaml` plus everything under `ontology/domains/`.

Validate all ontology files at once with `python scripts/check_ontology.py`.

### Graph persistence layer

- `app/graph/neo4j_client.py` wraps the official Neo4j Python driver.
  `check_neo4j_connection()` is a non-raising health check used by the
  `/health` endpoint.
- `app/graph/graphiti_adapter.py`'s `build_graphiti()` constructs a
  [Graphiti](https://github.com/getzep/graphiti) client, the library that
  turns plain-text episodes into extracted entities/facts (via an LLM) and
  stores them in Neo4j with time tracking. This is what implements the
  "facts get superseded, not overwritten" behavior described above.
- `app/graph/graph_repository.py`'s `GraphRepository` runs Cypher directly
  and wraps Graphiti's own search for time-aware fact lookups, including
  resolving a query to a specific named entity first (and pooling that
  entity's facts across every connector it appears in under the same name)
  before falling back to Graphiti's broader hybrid search. Other modules go
  through this instead of touching the Neo4j driver or Graphiti client
  directly.

### Status

| Layer | Status |
|---|---|
| Core data models (`app/models/`) | Implemented |
| Ontology (`app/ontology/`, `ontology/`) | Implemented and tested |
| Graph persistence (`app/graph/`) | Implemented and tested |
| Ingestion (`app/ingestion/`) | Eight connector types: `web`, `google_drive`, `sharepoint`, `gmail`, and `outlook_mail` are real live sources; `database`/`documents`/`email` read bundled demo data. Scheduled + on-demand sync, content-hash dedup, and an in-process queue decoupling a sync trigger from extraction |
| Retrieval (`app/retrieval/`) | Named-entity resolution (including cross-source pooling) tried first, Graphiti's hybrid search as a fallback -- see `GraphRepository.search_graphiti_facts` |
| Context composition (`app/context/`) | Synthesized answers, per-fact source attribution, a short-lived response cache, and per-query observability (`retrieval_path`/`cache_hit`/`cost_usd`) |
| API (`app/api/`) | `/health`, `/entities`, `/context/query`, `/graph/*` (role-based visibility), `/document-sets`, `/connectors` |
| MCP (`app/mcp/`) | `query_context_graph` and `list_available_sources` tools, same auth and query path as the HTTP API |
| Deployment | Live on Azure Container Apps + Neo4j AuraDB -- see [Deployment](#deployment) |

### Roadmap

Everything below is a real gap, not a stub -- each one is either blocked on
setup outside this repo's control, or genuinely not worth building yet at
this project's current scale. See [`CLAUDE.md`](CLAUDE.md) for the full
reasoning behind each.

- **Real-time push ingestion (CDC/webhooks).** Every connector today is
  polled (scheduled + on-demand), not pushed to. Debezium-based CDC for
  direct database access, and real webhook subscriptions for Drive/SharePoint,
  both need external, source-side registration (a verified webhook domain,
  subscription renewal management) that whoever owns those source accounts
  would need to set up -- not something this repo can stand up on its own.
  The ingestion queue (`app/graph/ingestion_queue.py`) is the actual
  prerequisite for this: a webhook receiver has to ack fast and can't block
  on an LLM call, which is what that now lets it do.
- **Stronger cross-source entity resolution.** Today's matching is exact
  name equality (see [Source connectors](#source-connectors)), which is
  real and deterministic but weaker than matching on a genuine shared key
  (an email address, an external system ID) -- worth revisiting once such a
  key actually exists in real client data.
- **A real admin plane.** `config/tenants.json` (or the `TENANT_API_KEYS` env
  var) is fine for a handful of clients managed by hand; a Postgres-backed
  admin plane, and an ontology-authoring UI on top of it (domain/client
  packs are hand-edited YAML today), would matter once there are enough
  clients that hand-editing doesn't scale.
- **Per-tenant Neo4j databases.** Today's isolation is `group_id`-scoped
  within one shared Neo4j Community database -- a reasonable baseline, not
  the strongest possible guarantee (see [API access](#api-access)). Worth a
  dedicated-database migration for a client whose compliance requirements
  call for it.
- **Formal usage-based billing** on top of the existing local spend-cap
  accounting (`app/graph/spend_limiter.py`), once there's a real paying
  client to bill.
