# Run with: python scripts/check_ontology.py
#
# Loads every ontology YAML file (core + all domain packs) and validates + merges
# them, the same way app/ontology/bootstrap.py does at app startup. Use this to
# check your YAML edits are valid without having to start the whole app. It fails
# loudly with a clear error if a file is malformed, instead of that error showing up
# later as a confusing crash somewhere else in the app.
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
