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
  document, record, or message it came from.
- Adapts to each client's vocabulary through configuration. A hospital, a
  factory, and a law firm each use different terms for similar concepts. The
  underlying structure stays the same across clients; the vocabulary is
  swapped in as config rather than rewritten as code.

The goal is for this layer to be built once and reused across client
engagements, instead of every project starting from scratch on getting data
into a shape an AI can use.

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
   note, an email, a maintenance log entry.
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
   tagged with when it became true.
5. Later, someone or something asks a question. Retrieval searches the graph
   for facts relevant to that question, matching by meaning rather than exact
   keywords, and respecting time so only currently true facts are returned
   unless history is specifically requested.
6. The relevant facts are assembled into a context packet: a structured
   bundle of exactly the relevant information, with sources attached, which
   is what actually gets handed to the AI assistant answering the question.

## Current status

The ontology and graph storage layers are built and tested. Ingestion,
retrieval, and context assembly have their interfaces and file structure in
place, with a working path for graph-based retrieval, but most of the actual
logic is still stubbed out. See [Status](#status) below for a layer-by-layer
breakdown.

## Setup

### Prerequisites

- Python 3.11+
- [Neo4j](https://neo4j.com/). The easiest way to run one locally is
  [Neo4j Desktop](https://neo4j.com/download/): install it, create a local
  database, and start it.
- A [Google Gemini API key](https://aistudio.google.com/), used for entity
  extraction and search. Gemini has a usable free tier.

### Install and configure

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env with your Neo4j connection details and Gemini API key
```

`.env` needs:

```
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<your Neo4j password>
GOOGLE_API_KEY=<your Gemini API key>
```

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
Entity types: 28
Relationship types: 46
```

This loads and merges every ontology YAML file, the same way the app does
at startup. It doesn't touch Neo4j. If you've edited or added a domain file,
this is how to check it's valid before running anything else.

### 3. Set up Neo4j's indices

```bash
python scripts/init_neo4j.py
```

One-time setup so Neo4j can look things up quickly. Safe to run again later;
it just re-applies the same schema.

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

Expected output, a `ContextPacket` with a plain-text summary of the current
facts:

```json
{"query":"who manages the Contoso account", "entities":[], "relationships":[],
 "facts":[], "events":[], "evidence":[], "timeline":[], "confidence":null,
 "metadata":{"group_ids":["acme_demo"],"summary":"Contoso Ltd's account is managed by Marcus Lee\n..."}}
```

This proves the same thing step 5 did, but over HTTP with the tenant
isolation described in [API access](#api-access) rather than by running a
Python script directly. `entities`/`relationships`/`facts`/`events` come back
empty for now; the summary in `metadata` is the only thing actually filled
in today (see [Status](#status)).

Run the test suite at any point (the ontology and model tests don't need
Neo4j running; the graph tests do):

```bash
pytest
```

## Ingesting data

Steps 4 and 5 above ingest one hand-written sentence at a time. To load an
actual dataset, `scripts/ingest_samples.py` reads the files in
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
- Start the API and try it in a browser:

  ```bash
  uvicorn app.main:app --reload
  ```

  Visit `http://localhost:8000/docs` for interactive API docs, or
  `http://localhost:8000/api/v1/entities` for the full list of entity and
  relationship types the ontology currently defines. Querying
  `/api/v1/context/query` requires an `X-API-Key` header; see
  [API access](#api-access) below.

## API access

Neo4j's free (Community) edition doesn't support separate databases per
client the way its paid edition does, so one client's data is kept separate
from another's within a single database using Graphiti's `group_id`. On its
own that separation is only as strong as whatever calls the API: if a caller
could put any `group_id` it wanted directly into a request, the separation
would be advisory rather than enforced.

To close that gap, `POST /api/v1/context/query` requires an `X-API-Key`
header. `app/security.py` looks the key up in `config/tenants.json` and
returns that tenant's config: their `group_id`, and their own Gemini API
key. The request body has no `group_id` or key field, so there's nothing for
a caller to override. An invalid or missing key is rejected before any Neo4j
or Gemini call is made.

```bash
curl -X POST http://localhost:8000/api/v1/context/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: local-dev-key" \
  -d '{"query": "who manages the Contoso account"}'
```

Each tenant also brings their own Gemini API key rather than sharing one
operator-owned key. Building a Graphiti client (LLM + embedder + reranker
setup) is real overhead, so this isn't done fresh on every request: each
tenant gets one client, built on their first request and cached after that --
see `app/graph/tenant_graphiti_pool.py`.

This is a reasonable baseline for one shared database serving multiple
clients, not the strongest possible guarantee. Full separation would mean a
dedicated database or deployment per client, which costs more in
infrastructure but removes any risk of an application bug leaking one
client's data into another's. Worth revisiting if a client's compliance
requirements call for it.

### Adding a client's API key

No code editing or hand-written JSON required -- use `scripts/manage_tenants.py`,
which reads and writes `config/tenants.json` for you:

```bash
# Add a new client, generating a random API key for them
python scripts/manage_tenants.py add --name "Acme Corp" --gemini-key <their Gemini API key>

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

---

## Technical documentation

### Folder structure

```text
saxon-context-engine/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── app/
│   ├── main.py                  # FastAPI app entry point
│   ├── config.py                # Settings, loaded from .env
│   ├── security.py              # API key -> tenant (group_id) lookup
│   ├── models/                  # Entity, Relationship, Fact, Event, Evidence, ContextPacket
│   ├── ontology/                # Loads, validates, and merges ontology YAML files
│   │   ├── loader.py
│   │   ├── validator.py
│   │   ├── registry.py
│   │   └── bootstrap.py
│   ├── graph/                   # Neo4j connection and Graphiti integration
│   │   ├── neo4j_client.py
│   │   ├── graph_repository.py
│   │   ├── graphiti_adapter.py
│   │   └── tenant_graphiti_pool.py  # One cached Graphiti client per tenant
│   ├── ingestion/               # Turning raw text/records into graph writes
│   ├── retrieval/                # Querying graph, semantic, and live data sources
│   │   └── base.py               # Shared interface for query-based retrievers
│   ├── context/                  # Planning, ranking, and composing query responses
│   └── api/                     # FastAPI routes: /health, /entities, /context
├── ontology/
│   ├── README.md                # Ontology design principles and layering
│   ├── core.yaml                # Enterprise-wide entity/relationship definitions
│   ├── customer-extension-template.yaml
│   └── domains/                 # Industry-specific extensions
├── data/                         # raw/processed/sample data
├── scripts/                      # Standalone runnable scripts, see Setup above
├── tests/                        # pytest test suite
└── eval/                         # question/expected-answer pairs for eval work
```

### Ontology layer

`ontology/core.yaml` defines domain-neutral concepts (Entity, Organization,
Person, Event, Document, Metric...) and relationships (OWNS, MANAGES,
PART_OF...) that apply to any business. Industry-specific packs under
`ontology/domains/*.yaml` extend that core additively: a domain pack can
specialize an existing type but can't invent an unrelated one. A client
deployment can extend further via
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
  `GraphitiAdapter` is a small helper for writing a plain record straight to
  Neo4j, bypassing extraction, used in tests.
- `app/graph/graph_repository.py`'s `GraphRepository` runs Cypher directly
  and wraps Graphiti's own search for time-aware fact lookups. Other modules
  go through this instead of touching the Neo4j driver or Graphiti client
  directly.

### Status

| Layer | Status |
|---|---|
| Core data models (`app/models/`) | Implemented |
| Ontology (`app/ontology/`, `ontology/`) | Implemented and tested |
| Graph persistence (`app/graph/`) | Implemented and tested |
| Ingestion (`app/ingestion/`) | Works for files: CSV and text sources are read, converted to prose, and extracted against the ontology. No connectors to live source systems (CRM/ERP APIs) yet |
| Retrieval (`app/retrieval/`) | Graph retrieval works; semantic and live-data retrieval are stubs |
| Context composition (`app/context/`) | Basic assembly from graph facts works; planning, ranking, and richer composition are stubs |
| API (`app/api/`) | `/health`, `/entities`, `/context/query` implemented |

### Roadmap

- **Connectors to live source systems.** File-based ingestion works (see
  "Ingesting data" above), but nothing yet pulls from a CRM or ERP's API on a
  schedule. That needs per-system connector code plus incremental sync, so a
  nightly run picks up only what changed rather than reprocessing everything.
- **Ontology authoring UI.** Domain and client ontology packs are hand-edited
  YAML today. A UI for configuring a new client's vocabulary without editing
  YAML directly is planned, not started.
- **Entity resolution.** An earlier stub for this was removed since it only
  trimmed whitespace and Graphiti already does semantic entity
  deduplication during extraction. Worth adding back only if a concrete gap
  shows up, such as a cross-system ID match that Graphiti's matching misses.
- **Semantic search and live data retrieval.** Not built yet. The retrieval
  layer already has a shared interface (`app/retrieval/base.py`) and
  `ContextOrchestrator` takes a list of retrievers, so adding semantic
  search later means appending to that list rather than restructuring it.
  Live-data retrieval looks up one entity at a time rather than answering a
  free-text query, so it's left out of that shared interface.
- **Hosting, once this needs to leave one laptop.** Everything currently
  runs locally. A phased hosting/cost plan exists internally, not published
  in this repo.
