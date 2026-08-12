from typing import Any
from pydantic import BaseModel, Field


class Entity(BaseModel):
    id: str
    type: str
    name: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
