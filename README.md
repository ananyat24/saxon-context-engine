# AIssist Context Engine (`aissist-context`)

An enterprise Context Engine Architecture combining an **Ontology Layer**, **Graphiti Temporal Knowledge Graph**, and **Neo4j Graph Database**.

## Folder Structure

```text
aissist-context/
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── app/
│   ├── main.py
│   ├── config.py
│   ├── models/
│   │   ├── entity.py
│   │   ├── relationship.py
│   │   ├── fact.py
│   │   ├── event.py
│   │   ├── evidence.py
│   │   └── context_packet.py
│   ├── ontology/
│   │   ├── loader.py
│   │   ├── validator.py
│   │   ├── registry.py
│   │   └── bootstrap.py
│   ├── graph/
│   │   ├── neo4j_client.py
│   │   ├── graph_repository.py
│   │   └── graphiti_adapter.py
│   ├── ingestion/
│   │   ├── pipeline.py
│   │   ├── structured.py
│   │   ├── unstructured.py
│   │   ├── extractor.py
│   │   └── entity_resolver.py
│   ├── retrieval/
│   │   ├── graph_retriever.py
│   │   ├── semantic_retriever.py
│   │   └── live_data_retriever.py
│   ├── context/
│   │   ├── planner.py
│   │   ├── orchestrator.py
│   │   ├── composer.py
│   │   └── ranker.py
│   └── api/
│       ├── context.py
│       ├── entities.py
│       └── health.py
├── ontology/
│   ├── README.md
│   ├── core.yaml
│   ├── customer-extension-template.yaml
│   └── domains/
│       ├── finance.yaml
│       ├── healthcare.yaml
│       ├── legal.yaml
│       ├── manufacturing.yaml
│       ├── pharma.yaml
│       ├── retail.yaml
│       ├── sales.yaml
│       ├── supply_chain.yaml
│       └── technology.yaml
├── data/
│   ├── raw/
│   ├── processed/
│   └── samples/
├── scripts/
│   ├── init_neo4j.py
│   ├── seed_core_graph.py
│   ├── test_graph.py
│   ├── test_neo4j.py
│   └── check_ontology.py
├── tests/
│   ├── test_models.py
│   ├── test_ontology.py
│   ├── test_graph.py
│   ├── test_ingestion.py
│   └── test_context.py
└── eval/
    ├── questions.json
    └── expected_results.json
```

## Ontology Layer

The ontology is a layered, YAML-defined schema: **Enterprise Core** (`ontology/core.yaml`)
provides domain-neutral concepts (`Entity`, `Organization`, `Event`, `Document`, `Metric`, ...),
which industry **domain packs** (`ontology/domains/*.yaml`) extend additively, and which
customer deployments further extend via `ontology/customer-extension-template.yaml`. See
[`ontology/README.md`](ontology/README.md) for the layering model and extension rules.

At runtime, `app.ontology.bootstrap.registry` loads the core ontology plus every domain pack
into a merged `OntologyRegistry`. Validate the ontology files at any time with:

```bash
python scripts/check_ontology.py
```

## Setup & Running

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Set environment variables in `.env`:
   ```bash
   cp .env.example .env
   # Edit .env with your NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, and GOOGLE_API_KEY
   ```
3. Run test graph script:
   ```bash
   python scripts/test_graph.py
   ```
