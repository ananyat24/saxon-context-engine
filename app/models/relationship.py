# Relationship: a directed edge between two entities in the graph, e.g.
# (Person "sarah-chen") --[MANAGES]--> (Organization "contoso-ltd").
from typing import Any
from pydantic import BaseModel, Field


class Relationship(BaseModel):
    # id of the Entity this relationship starts from.
    source_id: str

    # The type of relationship, e.g. "MANAGES" or "PURCHASED". Should match a
    # relationship name defined in the ontology (see ontology/core.yaml).
    relationship_type: str

    # id of the Entity this relationship points to.
    target_id: str

    # Extra details about the relationship itself (not about either entity),
    # e.g. {"since": "2024-01-01"}.
    properties: dict[str, Any] = Field(default_factory=dict)
