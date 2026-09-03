from pathlib import Path

import pytest

from app.ontology.bootstrap import build_scoped_registry
from app.ontology.graphiti_types import (
    build_edge_type_map,
    build_edge_types,
    build_entity_types,
)
from app.ontology.loader import OntologyLoader
from app.ontology.registry import OntologyRegistry
from app.ontology.validator import OntologyValidationError, OntologyValidator


def test_core_ontology_loads_and_validates():
    ontology = OntologyLoader.load(Path("ontology/core.yaml"))
    OntologyValidator().validate(ontology)

    assert "Organization" in ontology["entities"]
    assert "Event" in ontology["entities"]
    assert "RELATED_TO" in ontology["relationships"]


def test_registry_can_register_core_and_domain():
    # Registering core.yaml then a domain pack mirrors what
    # app/ontology/bootstrap.py does for the whole app at startup.
    registry = OntologyRegistry()

    core = OntologyLoader.load("ontology/core.yaml")
    manufacturing = OntologyLoader.load("ontology/domains/manufacturing.yaml")

    registry.register(core)
    registry.register(manufacturing)

    assert "Entity" in registry.entity_types()
    assert "Organization" in registry.entity_types()


def test_domain_packs_define_real_types():
    # manufacturing and legal were built against the sample datasets in
    # data/samples/; these assert they didn't regress back to empty stubs.
    manufacturing = build_scoped_registry(["manufacturing"])
    assert "Machine" in manufacturing.entity_types()
    assert "MachineFailure" in manufacturing.entity_types()
    assert "MEASURED_ON" in manufacturing.relationship_types()

    legal = build_scoped_registry(["legal"])
    assert "Clause" in legal.entity_types()
    assert "ContractTerm" in legal.entity_types()
    assert "HAS_CLAUSE" in legal.relationship_types()

    # sales and supply_chain were empty scaffolds until the Context
    # Graph/Layer/Engine pivot (see CLAUDE.md): these assert they were
    # actually populated with real, extends:-chained types rather than left
    # as stubs, and that the causal-chain example the pivot cites (an Order
    # -> Product -> Component -> Supplier -> QualityEvent) is representable.
    sales = build_scoped_registry(["sales"])
    assert "Account" in sales.entity_types()
    assert "Opportunity" in sales.entity_types()
    assert sales.get_entity_type("Account")["extends"] == "Organization"
    assert "RESULTED_IN_ORDER" in sales.relationship_types()

    supply_chain = build_scoped_registry(["supply_chain"])
    assert "Supplier" in supply_chain.entity_types()
    assert "Component" in supply_chain.entity_types()
    assert "QualityEvent" in supply_chain.entity_types()
    assert supply_chain.get_entity_type("Supplier")["extends"] == "Organization"
    assert "COMPOSED_OF" in supply_chain.relationship_types()
    assert "FLAGGED_BY" in supply_chain.relationship_types()


def test_scoped_registry_excludes_other_domains():
    # Scoping keeps the extraction prompt focused: a manufacturing ingest
    # shouldn't carry the legal vocabulary.
    manufacturing = build_scoped_registry(["manufacturing"])
    assert "Machine" in manufacturing.entity_types()
    assert "Clause" not in manufacturing.entity_types()


def test_entity_types_become_pydantic_models():
    registry = build_scoped_registry(["manufacturing"])
    entity_types = build_entity_types(registry)

    machine = entity_types["Machine"]
    # Graphiti puts the docstring in the extraction prompt, so it has to carry
    # the ontology's description rather than being empty.
    assert machine.__doc__
    assert "machine_type" in machine.model_fields

    # The abstract base type isn't a thing in the customer's world, so it
    # shouldn't be offered to the extractor.
    assert "Entity" not in entity_types


def _minimal_ontology(**overrides) -> dict:
    base = {
        "ontology": {"id": "test", "name": "Test", "version": "0.0.1"},
        "entities": {},
        "relationships": {},
        "event_types": {},
        "fact_types": {},
        "aliases": {},
    }
    base.update(overrides)
    return base


def test_registry_rejects_two_packs_aliasing_the_same_word_to_different_types():
    # The bug this guards against: aliases are a flat merged namespace across
    # every registered ontology file (see OntologyRegistry.register), and
    # unlike entities/relationships/event_types/fact_types (always dicts,
    # merged by updating properties), an alias is a plain string, so a
    # later pack's conflicting alias used to silently win with no warning.
    # Found for real this session: sales.yaml and supply_chain.yaml both
    # originally aliased "po" to a different type (Order vs. PurchaseOrder).
    registry = OntologyRegistry()
    registry.register(_minimal_ontology(aliases={"po": "Order"}))
    with pytest.raises(OntologyValidationError, match="po"):
        registry.register(_minimal_ontology(aliases={"po": "PurchaseOrder"}))


def test_registry_allows_two_packs_aliasing_the_same_word_identically():
    # Not every repeated alias is a conflict: two packs genuinely agreeing
    # ("customer" -> "Account" in both) shouldn't fail registration.
    registry = OntologyRegistry()
    registry.register(_minimal_ontology(aliases={"customer": "Account"}))
    registry.register(_minimal_ontology(aliases={"customer": "Account"}))
    assert registry._ontology["aliases"]["customer"] == "Account"


def test_registry_still_merges_two_packs_extending_the_same_entity_type():
    # Regression check: the fix above must not break the existing, intended
    # behavior of two packs both touching the same entity/relationship type
    # (a dict value): only scalar (alias) conflicts should raise. The merge
    # itself is a shallow dict.update at the entity level (not a deep merge
    # of nested "properties"), so this checks that shape: a second pack
    # adding a new top-level field (here, "description") to a type an
    # earlier pack already defined coexists with that earlier pack's fields,
    # rather than one wholesale-replacing the other.
    registry = OntologyRegistry()
    registry.register(_minimal_ontology(entities={"Organization": {"extends": "Entity", "properties": {"a": 1}}}))
    registry.register(_minimal_ontology(entities={"Organization": {"extends": "Entity", "description": "extra"}}))
    assert registry._ontology["entities"]["Organization"]["properties"] == {"a": 1}
    assert registry._ontology["entities"]["Organization"]["description"] == "extra"


def test_all_bundled_ontology_files_register_without_conflict():
    # The real regression check: every domain pack this repo actually ships
    # loads together cleanly under the stricter check above.
    registry = OntologyRegistry()
    registry.register(OntologyLoader.load("ontology/core.yaml"))
    for domain_file in sorted(Path("ontology/domains").glob("*.yaml")):
        registry.register(OntologyLoader.load(domain_file))


def test_edge_types_and_map_cover_domain_relationships():
    registry = build_scoped_registry(["manufacturing"])
    edge_types = build_edge_types(registry)
    edge_map = build_edge_type_map(registry)

    assert "MEASURED_ON" in edge_types
    assert edge_map[("SensorReading", "Machine")] == ["MEASURED_ON"]


# --- causal_relationship_types (drives GraphRepository's causal-chain
# walker: see that module's own _CAUSAL_RELATIONSHIP_TYPES docstring) ----


def test_causal_relationship_types_includes_core_generic_types():
    registry = OntologyRegistry()
    registry.register(OntologyLoader.load("ontology/core.yaml"))
    causal = registry.causal_relationship_types()
    for name in ("AFFECTS", "DEPENDS_ON", "CAUSED_BY", "RESULTED_IN", "SOURCED_FROM", "PRODUCES", "TRIGGERED_BY"):
        assert name in causal
    # A relationship with no causal flag at all must not show up just
    # because it's defined: RELATED_TO is core's generic catch-all, and
    # PROVIDES/PERFORMED_ON are administrative/classificatory rather than
    # causal (see ontology/core.yaml's own comments on PRODUCES/
    # TRIGGERED_BY for why those two specifically needed the flag: a real
    # root-cause chain routinely runs through exactly them ("supplier
    # PRODUCES a lot, that lot PRODUCES a defective component"), and the
    # causal-chain walker used to dead-end one hop short of a root cause
    # that was fully present and connected in the graph).
    for name in ("RELATED_TO", "PROVIDES", "PERFORMED_ON"):
        assert name not in causal


def test_causal_relationship_types_includes_a_flagged_domain_specific_type():
    # This is the real bug this test pins: supply_chain.yaml was written
    # specifically to represent the reference architecture's own worked
    # example (Order -> Product -> Component -> Supplier -> QualityEvent),
    # but GraphRepository's causal walker used to only recognize core's 5
    # generic types, none of supply_chain's own relationship names, so
    # that exact example could never produce a causal answer even with
    # perfectly-extracted data. See CLAUDE.md's v5/v2 follow-up notes.
    registry = build_scoped_registry(["supply_chain"])
    causal = registry.causal_relationship_types()
    for name in ("SUPPLIES", "COMPOSED_OF", "FLAGGED_BY", "QUALITY_ISSUE_ON"):
        assert name in causal
    # A real, non-causal supply_chain relationship (plain logistics
    # tracking, not "why did this happen") must still be excluded.
    assert "DELIVERED_TO" not in causal
