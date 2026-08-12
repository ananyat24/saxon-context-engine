# AISSIST Enterprise Ontology Package

This package implements a domain-neutral ontology foundation for the AISSIST
knowledge graph and context engine.

## Design principles

1. Enterprise Core first
2. Domain packs extend the core
3. Customer-specific vocabulary is configuration, not application code
4. Temporal facts/events are first-class
5. Provenance/evidence is handled by the context model rather than hidden in ontology
6. Avoid using RELATED_TO when a meaningful relationship exists
7. Prefer additive extensions over redefining core semantics

## Layering

Enterprise Core
  -> Domain Extension(s)
  -> Customer Extension

Example:

Entity
  -> Asset
      -> Machine (manufacturing extension)

Entity
  -> Document
      -> Policy
      -> SOP (pharma/manufacturing extension)

Entity
  -> Event
      -> Interaction
      -> CustomerMeeting (sales extension)

## Install

Requires:

    pip install pyyaml

Copy:
- `app/ontology/` into the existing application's `app/ontology/`
- `ontology/` into the repository root
- `tests/test_ontology.py` into tests
- `scripts/check_ontology.py` into scripts

Run:

    python scripts/check_ontology.py
    pytest tests/test_ontology.py

## Important

Do not encode enterprise-specific terms directly into the Context Engine.
The runtime should reason over generic concepts such as Entity, Event, Fact,
Relationship, Evidence, Metric, Decision and Context. Industry/customer
specificity belongs in domain/customer ontology extensions.
