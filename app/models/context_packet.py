from typing import Any
from pydantic import BaseModel, Field

from .entity import Entity
from .relationship import Relationship
from .fact import Fact
from .event import Event
from .evidence import Evidence


class ContextPacket(BaseModel):
    query: str
    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
