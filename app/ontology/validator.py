# Structural validation for a single ontology YAML file (already parsed into a dict
# by OntologyLoader). This does NOT check the ontology's business meaning -- it only
# checks that the file has the shape the rest of the codebase expects, so a typo in
# a YAML file fails loudly at load time instead of causing a confusing error later
# when something tries to read a field that doesn't exist.
from typing import Any


class OntologyValidationError(ValueError):
    """Raised when an ontology YAML file is missing required structure."""


class OntologyValidator:
    """Checks that an ontology dict has the required top-level sections and that
    every entity/relationship entry is shaped correctly."""

    REQUIRED_ROOT_KEYS = {"ontology", "entities", "relationships"}

    def validate(self, ontology: dict[str, Any]) -> None:
        # Every ontology file must at least have these three top-level sections,
        # even if "entities" or "relationships" is just an empty dict (as it is in
        # the domain-pack templates before a client customizes them).
        missing = self.REQUIRED_ROOT_KEYS - set(ontology.keys())
        if missing:
            raise OntologyValidationError(
                f"Missing required ontology sections: {sorted(missing)}"
            )

        meta = ontology["ontology"]
        if not isinstance(meta, dict):
            raise OntologyValidationError("'ontology' must be a mapping")

        for field in ("id", "name", "version"):
            if not meta.get(field):
                raise OntologyValidationError(f"ontology.{field} is required")

        entities = ontology["entities"]
        relationships = ontology["relationships"]

        if not isinstance(entities, dict):
            raise OntologyValidationError("'entities' must be a mapping")
        if not isinstance(relationships, dict):
            raise OntologyValidationError("'relationships' must be a mapping")

        for entity_name, entity in entities.items():
            if not isinstance(entity, dict):
                raise OntologyValidationError(f"Entity '{entity_name}' must be a mapping")
            # Every entity type must extend an existing type (ultimately tracing back
            # to the base "Entity" type) -- this is what enforces the core-first
            # layering described in ontology/README.md: a domain pack can't invent an
            # entity out of nothing, only specialize something the core already defines.
            if entity_name != "Entity" and "extends" not in entity:
                raise OntologyValidationError(f"Entity '{entity_name}' must declare 'extends'")

        for rel_name, rel in relationships.items():
            if not isinstance(rel, dict):
                raise OntologyValidationError(f"Relationship '{rel_name}' must be a mapping")
            for field in ("source", "target"):
                if not rel.get(field):
                    raise OntologyValidationError(f"Relationship '{rel_name}' missing '{field}'")
