from typing import Any


class OntologyValidationError(ValueError):
    pass


class OntologyValidator:
    """Lightweight structural validation for ontology definitions."""

    REQUIRED_ROOT_KEYS = {"ontology", "entities", "relationships"}

    def validate(self, ontology: dict[str, Any]) -> None:
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
                raise OntologyValidationError(
                    f"ontology.{field} is required"
                )

        entities = ontology["entities"]
        relationships = ontology["relationships"]

        if not isinstance(entities, dict):
            raise OntologyValidationError("'entities' must be a mapping")
        if not isinstance(relationships, dict):
            raise OntologyValidationError("'relationships' must be a mapping")

        for entity_name, entity in entities.items():
            if not isinstance(entity, dict):
                raise OntologyValidationError(
                    f"Entity '{entity_name}' must be a mapping"
                )
            if entity_name != "Entity" and "extends" not in entity:
                raise OntologyValidationError(
                    f"Entity '{entity_name}' must declare 'extends'"
                )

        for rel_name, rel in relationships.items():
            if not isinstance(rel, dict):
                raise OntologyValidationError(
                    f"Relationship '{rel_name}' must be a mapping"
                )
            for field in ("source", "target"):
                if not rel.get(field):
                    raise OntologyValidationError(
                        f"Relationship '{rel_name}' missing '{field}'"
                    )
