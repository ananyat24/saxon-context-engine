from pathlib import Path

from app.ontology.loader import OntologyLoader
from app.ontology.registry import OntologyRegistry
from app.ontology.validator import OntologyValidator


validator = OntologyValidator()
registry = OntologyRegistry()

paths = [Path("ontology/core.yaml")]
paths.extend(sorted(Path("ontology/domains").glob("*.yaml")))

for path in paths:
    ontology = OntologyLoader.load(path)
    validator.validate(ontology)
    registry.register(ontology)
    print(f"OK: {path}")

print()
print(f"Entity types: {len(registry.entity_types())}")
print(f"Relationship types: {len(registry.relationship_types())}")
