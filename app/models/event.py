from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class Event(BaseModel):
    id: str
    event_type: str
    timestamp: datetime | None = None
    participants: list[str] = Field(default_factory=list)
    related_entities: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
