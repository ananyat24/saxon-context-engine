# Entity: a "thing" in the graph -- a person, an organization, a machine, a document, etc.
#
# This is a Pydantic model, not a plain Python class. Pydantic checks the types of the
# data you pass in at runtime and raises a clear error if something doesn't match, which
# is why these classes look like plain field lists with no __init__ method -- Pydantic
# generates that for you based on the fields declared below.
from typing import Any
from pydantic import BaseModel, Field


class Entity(BaseModel):
    # Unique identifier for this entity, e.g. "org-001". Assigned by whatever system
    # created the record (CRM, ERP, ingestion pipeline, etc.), not auto-generated here.
    id: str

    # The ontology type this entity belongs to, e.g. "Organization" or "Person".
    # This should match a type name defined in ontology/core.yaml or a domain pack --
    # see app/ontology/ for how those definitions are loaded and validated.
    type: str

    # Human-readable name. Optional because some entities (e.g. a raw data record)
    # may not have one. `str | None = None` is Python's modern way of writing
    # "this field is a string, or it can be left out / set to None".
    name: str | None = None

    # Free-form extra attributes that don't have their own dedicated field, e.g.
    # {"industry": "manufacturing", "employee_count": 500}. `Field(default_factory=dict)`
    # gives every new Entity its own empty dict instead of sharing one mutable dict
    # across instances (a classic Python bug if you instead wrote `= {}` here).
    properties: dict[str, Any] = Field(default_factory=dict)
