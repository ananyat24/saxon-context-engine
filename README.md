# AIssist Context Engine

A reusable "memory layer" for enterprise AI: it collects facts from a company's
different systems, keeps track of what's true *and when it was true*, and hands an
AI assistant exactly the relevant, up-to-date context it needs to answer a question
correctly -- instead of the AI guessing, hallucinating, or working off stale data.

This repo is the early build-out of that engine. Two pieces are implemented and
tested today: the **ontology** (the rulebook for what kinds of things and
relationships the graph is allowed to store) and the **graph persistence layer**
(how that data actually gets written to and read from a graph database). Everything
else -- ingestion, search, the API, live data -- is scaffolded as a clear next step
but not yet built out.

---

## 1. What this is, in plain business terms

Every company already has scattered knowledge: a CRM, an ERP, spreadsheets,
emails, support tickets, maintenance logs. When you plug an AI assistant into a
business, its answers are only as good as the context you feed it. Feed it nothing,
and it guesses. Feed it a messy pile of raw exports, and it gets confused or
confidently wrong.

The Context Engine's job is to sit between "all of a company's scattered data" and
"the AI assistant" and do the hard part in the middle:

- **Organize** the facts into a consistent structure, instead of a wall of raw text.
- **Track history**, not just current state -- so the AI knows that the account
  used to be managed by Sarah, and is now managed by Marcus, rather than only
  knowing the current answer with no memory of the change.
- **Cite its sources** -- every fact can be traced back to the document, record, or
  message it came from, so answers are auditable, not just plausible-sounding.
- **Adapt to each client** -- a hospital, a factory, and a law firm all have
  different vocabularies and different kinds of records. The engine is built so
  the *foundation* (how facts and relationships are structured) stays the same
  across every client, while the *vocabulary* (what an "Organization" or an
  "Event" specifically looks like in manufacturing vs. pharma vs. legal) is
  swapped in as configuration, not rewritten as code.

The strategic bet is that this middle layer -- built once, well -- can be reused
across many client engagements, instead of every new AI project starting from
scratch on "how do we get our data into a shape an AI can actually use."

## 2. How it works, simply

Picture a **corkboard with pins and string** -- the classic image of a detective
tracking down connections. Each pin is a *thing* (a person, a company, a machine,
an order). Each piece of string connecting two pins is a *relationship* (this
person *manages* this account; this machine *is located at* this factory). That
corkboard is the graph.

Now imagine every pin and every string has a little sticky note attached that says
*when* it became true, and whether it's still true today. That's what makes this a
"temporal" graph, and it's the single most important trick this project relies on:
nothing gets erased when it changes, it gets marked as **out of date** and the new
fact gets added alongside it. So if you ask "who manages this account," the system
can tell you the current answer *and* the fact that it used to be someone else,
with no extra effort.

Here's the flow, start to finish:

1. **Something happens** in the real world and gets written down somewhere -- a CRM
   note, an email, a maintenance log entry.
2. **Ingestion** takes that raw text and hands it to an AI model (an LLM) whose only
   job is to read it and pull out: what entities does this mention, and what facts
   does it state about them? ("Contoso Ltd is a customer." "Sarah Chen manages the
   Contoso account.")
3. Before anything gets stored, it's checked against the **ontology** -- the
   rulebook that says what kinds of things ("Organization", "Person", "Event"...)
   and what kinds of relationships ("MANAGES", "OWNS", "LOCATED_AT"...) are allowed
   to exist. This keeps the graph consistent instead of turning into a junk drawer
   of one-off, inconsistent labels.
4. The extracted facts get written into **Neo4j**, a database built specifically for
   storing pins-and-string data like this (a "graph database"), with each fact
   tagged with when it became true.
5. Later, someone (or some other piece of software) asks a question. **Retrieval**
   searches the graph for facts relevant to that question -- both by matching
   meaning (not just exact keywords) and by respecting time (only "currently true"
   facts, unless you specifically want history).
6. Those facts get assembled into a **context packet** -- a clean, structured bundle
   of exactly the relevant information, with sources attached -- which is what
   actually gets handed to the AI assistant that's answering the question.

The engine you're looking at in this repo currently has steps 3 and 4 built and
tested (ontology + graph persistence). Steps 2, 5, and 6 have their file structure
and interfaces in place but are placeholders waiting to be filled in -- see
[What's built vs. scaffolded](#whats-built-vs-scaffolded) below.

## 3. Setting it up and trying it yourself

### Prerequisites

- Python 3.11+
- [Neo4j](https://neo4j.com/) -- the easiest way to get one running locally is
  [Neo4j Desktop](https://neo4j.com/download/): install it, create a new local
  database ("DBMS"), and start it.
- A [Google Gemini API key](https://aistudio.google.com/) (used for the AI-powered
  entity extraction and search -- Gemini has a usable free tier).

### Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure your environment
cp .env.example .env
# then edit .env with your Neo4j connection details and Gemini API key
```

`.env` needs:

```
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<your Neo4j Desktop password>
GOOGLE_API_KEY=<your Gemini API key>
```

### Try it out, step by step

Each of these builds on the last -- run them in order the first time.

```bash
# Step 1: confirm you can actually reach your Neo4j database
python scripts/test_neo4j.py
# Expect: "Neo4j connection successful"

# Step 2: confirm the ontology (the rulebook of allowed entity/relationship types)
# loads and validates correctly
python scripts/check_ontology.py
# Expect: a list of "OK: ontology/..." lines, ending in entity/relationship counts

# Step 3: one-time setup of the indices Neo4j needs for fast lookups
python scripts/init_neo4j.py

# Step 4: the smallest real end-to-end example -- write one fact, then ask about it
python scripts/seed_core_graph.py
# Expect: the sentence you fed in, reflected back as an extracted fact

# Step 5: the full demo -- shows facts changing over time and the old fact getting
# marked invalid once a new one supersedes it
python scripts/test_graph.py
```

Run the automated test suite (the ontology and model tests don't need Neo4j
running; the graph tests do):

```bash
pytest
```

### Experimenting

- Open Neo4j Desktop's "Neo4j Browser" against your running database and run
  `MATCH (n) RETURN n LIMIT 50` to see the pins-and-strings graph visually after
  running the scripts above.
- Edit `scripts/seed_core_graph.py` or `scripts/test_graph.py` with your own
  sentences to see what the AI model extracts from different kinds of text.
- Look at `ontology/domains/manufacturing.yaml` (or any other domain file) and try
  adding a new entity type of your own, following the pattern described in
  [`ontology/README.md`](ontology/README.md). Then rerun
  `python scripts/check_ontology.py` to confirm it's valid.
- Start the (currently minimal) API and poke at it in your browser:

  ```bash
  uvicorn app.main:app --reload
  ```

  then visit `http://localhost:8000/docs` for interactive API documentation, or
  `http://localhost:8000/api/v1/entities` to see the full merged list of entity
  and relationship types the ontology currently defines. Querying
  `/api/v1/context/query` requires an `X-API-Key` header -- see
  [API access & tenant isolation](#api-access--tenant-isolation) below.

---

## Technical documentation

### Folder structure

```text
aissist-context/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── app/
│   ├── main.py                  # FastAPI app entry point
│   ├── config.py                # Settings, loaded from .env
│   ├── models/                  # Pydantic data models: Entity, Relationship, Fact,
│   │                             # Event, Evidence, ContextPacket
│   ├── ontology/                # Loads, validates, and merges ontology YAML files
│   │   ├── loader.py
│   │   ├── validator.py
│   │   ├── registry.py
│   │   └── bootstrap.py
│   ├── graph/                   # Neo4j connection + Graphiti integration
│   │   ├── neo4j_client.py
│   │   ├── graph_repository.py
│   │   └── graphiti_adapter.py
│   ├── ingestion/                # [scaffolded] turning raw text/records into graph writes
│   ├── retrieval/                 # [scaffolded] querying the graph/semantic/live data sources
│   │   └── base.py                # TextRetriever interface every query-based retriever implements
│   ├── context/                   # [scaffolded] planning, ranking, and composing query responses
│   ├── security.py               # API key -> tenant (group_id) lookup, see "API access" below
│   └── api/                      # FastAPI routes: /health, /entities, /context
├── ontology/
│   ├── README.md                 # Ontology design principles and layering model
│   ├── core.yaml                 # Enterprise-wide entity/relationship definitions
│   ├── customer-extension-template.yaml
│   └── domains/                  # Industry-specific extensions (finance, healthcare, ...)
├── data/                          # raw/processed/sample data (empty, gitignored contents)
├── scripts/                       # Standalone runnable scripts, see "Try it out" above
├── tests/                         # pytest test suite
└── eval/                          # question/expected-answer pairs for future eval work
```

### Ontology layer (`app/ontology/`, `ontology/`)

The ontology is the schema for the graph -- see [`ontology/README.md`](ontology/README.md)
for the full design. In short: `ontology/core.yaml` defines domain-neutral concepts
(`Entity`, `Organization`, `Person`, `Event`, `Document`, `Metric`, ...) and
relationships (`OWNS`, `MANAGES`, `PART_OF`, ...) that apply to any business.
Industry-specific packs under `ontology/domains/*.yaml` extend that core additively
(a domain pack can only specialize an existing type via `extends:`, not invent an
unrelated one), and a customer deployment can extend further via
`ontology/customer-extension-template.yaml`.

- `app/ontology/loader.py` -- reads a single YAML file into a Python dict.
- `app/ontology/validator.py` -- checks a loaded ontology has the required
  structure (raises `OntologyValidationError` with a specific message if not).
- `app/ontology/registry.py` -- merges any number of validated ontology files
  together (core, then domain packs, then customer extensions) into one queryable
  `OntologyRegistry`.
- `app/ontology/bootstrap.py` -- builds the app's single `registry` instance at
  import time, from `ontology/core.yaml` + everything under `ontology/domains/`.

Validate all ontology files at once: `python scripts/check_ontology.py`.

### Graph persistence layer (`app/graph/`)

This is how facts actually get read from and written to storage.

- `app/graph/neo4j_client.py` -- a thin wrapper around the official Neo4j Python
  driver (the connection pool used to run queries). `check_neo4j_connection()` is
  a non-raising health check used by the `/health` API endpoint.
- `app/graph/graphiti_adapter.py` -- `build_graphiti()` constructs a
  [Graphiti](https://github.com/getzep/graphiti) client, the library that turns
  plain-text "episodes" into extracted entities/facts (via an LLM) and stores them
  in Neo4j with built-in time tracking -- this is what implements the
  "facts get superseded, not overwritten" behavior described above.
  `GraphitiAdapter` is a small helper for writing a plain record straight to Neo4j
  as an `Episode` node, bypassing LLM extraction (used in tests/demos).
- `app/graph/graph_repository.py` -- `GraphRepository` is the single place that
  runs Cypher (Neo4j's query language) directly, and also wraps Graphiti's own
  `search()` for time-aware fact lookups. It's the layer other modules (like
  `app/retrieval/graph_retriever.py`) go through instead of touching the Neo4j
  driver or Graphiti client directly.

### What's built vs. scaffolded

| Layer | Status |
|---|---|
| Core data models (`app/models/`) | Implemented -- plain Pydantic schemas |
| Ontology (`app/ontology/`, `ontology/`) | Implemented and tested |
| Graph persistence (`app/graph/`) | Implemented and tested |
| Ingestion (`app/ingestion/`) | Scaffolded -- `IngestionPipeline` calls Graphiti correctly; structured/unstructured formatting helpers are stubs |
| Retrieval (`app/retrieval/`) | Scaffolded -- graph retrieval works via `GraphRepository` and implements the shared `TextRetriever` interface (`app/retrieval/base.py`) so semantic search can be added later without restructuring `ContextOrchestrator`; semantic and live-data retrieval themselves are stubs |
| Context composition (`app/context/`) | Scaffolded -- `ContextOrchestrator` assembles a basic response from graph facts; planning, ranking, and richer composition are stubs |
| API (`app/api/`) | Implemented for what exists above -- `/health`, `/entities`, `/context/query` |

### Running the API

```bash
uvicorn app.main:app --reload
```

- `GET /api/v1/health` -- is the app able to reach Neo4j right now?
- `GET /api/v1/entities` -- every entity/relationship type the currently loaded
  ontology defines.
- `POST /api/v1/context/query` -- `{"query": "..."}` with an `X-API-Key` header,
  returns a `ContextPacket` assembled from whatever the graph currently knows.
  See below for what that header controls.

### API access & tenant isolation

Neo4j Community Edition (what this project runs on) can't give each client its
own database the way Enterprise Edition can, so this project keeps one client's
data logically separate from another's inside a single database using Graphiti's
`group_id`. On its own, that separation is only as strong as whatever calls the
API -- if a client could put any `group_id` it wanted directly in a request, the
separation would be advisory, not enforced.

To close that gap, `POST /api/v1/context/query` requires an `X-API-Key` header.
`app/security.py` looks the key up in `TENANT_API_KEYS` (configured in `.env`,
see `.env.example`) and returns the one `group_id` that key is allowed to query --
the request body has no `group_id` field at all, so there's nothing for a caller
to override. An invalid or missing key gets a 401/422 before any Neo4j or Gemini
call is made.

```bash
curl -X POST http://localhost:8000/api/v1/context/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: local-dev-key" \
  -d '{"query": "who manages the Contoso account"}'
```

This is a practical baseline appropriate for one shared Neo4j instance serving
multiple logical tenants, not the strongest possible guarantee. The strongest
guarantee is physical separation -- a dedicated Neo4j database (Enterprise
Edition) or a fully separate deployment per client -- which trades infrastructure
cost for eliminating any risk of an application bug leaking data across tenants.
That tradeoff is worth revisiting once there's a real client whose compliance
requirements call for it.

### Roadmap / open decisions

- **Ontology authoring UI.** Domain and customer ontology packs are hand-edited
  YAML today (`ontology/domains/*.yaml`, `ontology/customer-extension-template.yaml`).
  A UI for a non-engineer to configure a new client's vocabulary without editing
  YAML directly is a planned next step, not yet started.
- **Entity resolution.** The earlier `EntityResolver` stub was removed -- it only
  trimmed whitespace and wasn't used anywhere, while Graphiti already does
  semantic entity deduplication as part of its own extraction. Reintroduce a real
  resolver only if a concrete gap shows up (e.g. Graphiti's matching missing an
  obvious cross-system ID match that a deterministic rule could catch).
- **Semantic search & live data (layers 9-10 of the original design).** Not
  built yet, and intentionally not rushed -- but `app/retrieval/base.py`'s
  `TextRetriever` interface and `ContextOrchestrator`'s retriever list already
  exist so that adding a semantic retriever later is additive (append it to the
  list) rather than a restructure. Live-data retrieval looks up one entity at a
  time rather than answering a free-text query, so it's deliberately left out of
  that shared interface rather than forced to fit it.
