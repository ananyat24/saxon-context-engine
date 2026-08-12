from pathlib import Path

from app.ontology.bootstrap import build_scoped_registry
from app.ontology.graphiti_types import (
    build_edge_type_map,
    build_edge_types,
    build_entity_types,
)
from app.ontology.loader import OntologyLoader
from app.ontology.registry import OntologyRegistry
from app.ontology.validator import OntologyValidator


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


def test_scoped_registry_excludes_other_domains():
    # Scoping keeps the extraction prompt focused -- a manufacturing ingest
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


def test_edge_types_and_map_cover_domain_relationships():
    registry = build_scoped_registry(["manufacturing"])
    edge_types = build_edge_types(registry)
    edge_map = build_edge_type_map(registry)

    assert "MEASURED_ON" in edge_types
    assert edge_map[("SensorReading", "Machine")] == ["MEASURED_ON"]
