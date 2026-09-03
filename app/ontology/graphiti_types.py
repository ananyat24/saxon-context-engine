# Bridges the YAML ontology into the shape Graphiti's extraction expects.
#
# Why this exists: loading and validating the ontology (loader/validator/registry)
# only makes it available to *us*. Graphiti's LLM extraction doesn't see it unless
# it's passed to add_episode(), and without that the LLM invents its own type
# names: ingesting Northwind customers produced HAS_COMPANY_NAME,
# LOCATED_IN_CITY, and LOCATED_IN_COUNTRY, none of which are in the ontology,
# alongside LOCATED_AT and OWNS, which are. That inconsistency is exactly what
# an ontology is supposed to prevent, so the schema has to reach the extractor.
#
# Graphiti wants `dict[str, type[BaseModel]]`: actual Pydantic classes, whose
# names and docstrings it puts in the extraction prompt. So each ontology type
# is converted into a generated Pydantic model here.
from typing import Any, Optional

from pydantic import BaseModel, Field, create_model

from app.ontology.registry import OntologyRegistry

# Ontology property types -> Python types for the generated Pydantic models.
# Everything is Optional: the ontology marks few properties required, and a
# missing field should not make extraction fail.
_TYPE_MAP: dict[str, type] = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "datetime": str,  # kept as str; Graphiti handles temporal fields separately
    "array": list,
    "object": dict,
    "any": str,
}

# Types that describe the graph's own machinery rather than things in the
# customer's world. Passing them to the extractor invites it to model the
# ontology instead of the data.
_ABSTRACT_OR_META = {"Entity"}


def _model_for(name: str, definition: dict[str, Any]) -> type[BaseModel]:
    """Build one Pydantic model from an ontology entity/relationship definition."""
    fields: dict[str, Any] = {}
    for prop_name, prop in (definition.get("properties") or {}).items():
        if not isinstance(prop, dict):
            continue
        py_type = _TYPE_MAP.get(str(prop.get("type", "string")).lower(), str)
        fields[prop_name] = (Optional[py_type], Field(default=None))

    # Graphiti shows the docstring to the extraction LLM, so the ontology's
    # description is what actually steers which type gets chosen.
    description = definition.get("description") or f"{name} as defined by the ontology."
    model = create_model(name, **fields)
    model.__doc__ = description.strip()
    return model


def build_entity_types(registry: OntologyRegistry) -> dict[str, type[BaseModel]]:
    """Every concrete entity type in the registry, as Graphiti-ready models."""
    snapshot = registry.snapshot()
    return {
        name: _model_for(name, definition)
        for name, definition in snapshot["entities"].items()
        if name not in _ABSTRACT_OR_META and not definition.get("abstract")
    }


def build_edge_types(registry: OntologyRegistry) -> dict[str, type[BaseModel]]:
    """Every relationship type in the registry, as Graphiti-ready models."""
    snapshot = registry.snapshot()
    return {name: _model_for(name, definition) for name, definition in snapshot["relationships"].items()}


def build_edge_type_map(registry: OntologyRegistry) -> dict[tuple[str, str], list[str]]:
    """Which relationships are allowed between which entity types.

    Graphiti keys this by (source_type, target_type). The ontology declares
    source/target per relationship, but often as the abstract "Entity" (meaning
    "any type"), so those become a single ("Entity", "Entity") wildcard entry
    that Graphiti applies broadly rather than being expanded across every
    possible type pair.
    """
    snapshot = registry.snapshot()
    edge_map: dict[tuple[str, str], list[str]] = {}
    for name, definition in snapshot["relationships"].items():
        source = definition.get("source", "Entity")
        target = definition.get("target", "Entity")
        edge_map.setdefault((source, target), []).append(name)
    return edge_map


def build_graphiti_schema(
    registry: OntologyRegistry,
) -> tuple[dict[str, type[BaseModel]], dict[str, type[BaseModel]], dict[tuple[str, str], list[str]]]:
    """Convenience: all three pieces add_episode() needs, in one call."""
    return build_entity_types(registry), build_edge_types(registry), build_edge_type_map(registry)
