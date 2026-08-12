from pathlib import Path

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
