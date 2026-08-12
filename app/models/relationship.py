from typing import Any
from pydantic import BaseModel, Field


class Relationship(BaseModel):
    source_id: str
    relationship_type: str
    target_id: str
    properties: dict[str, Any] = Field(default_factory=dict)
