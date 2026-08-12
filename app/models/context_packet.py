# ContextPacket: the final, assembled answer to a query -- everything the system
# gathered (entities, relationships, facts, events, and the evidence behind them)
# bundled together so it can be handed to an LLM as grounding, or returned directly
# to a caller. Think of this as the "response object" for the whole context engine:
# every retrieval/composition step in app/context/ builds up one of these.
from typing import Any
from pydantic import BaseModel, Field

from .entity import Entity
from .relationship import Relationship
from .fact import Fact
from .event import Event
from .evidence import Evidence


class ContextPacket(BaseModel):
    # The original question or request this packet was assembled to answer.
    query: str

    entities: list[Entity] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    events: list[Event] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)

    # An ordered, human-readable view of events/facts across time, for callers that
    # want a narrative rather than separate lists. Left as loose dicts (rather than
    # a dedicated model) since the shape of a timeline entry can vary by use case.
    timeline: list[dict[str, Any]] = Field(default_factory=list)

    # Overall confidence in this packet's contents, when the retrieval pipeline can
    # estimate one (e.g. by averaging the confidence of the facts it contains).
    confidence: float | None = None

    # Anything else worth attaching -- which retrievers ran, timing info, etc.
    metadata: dict[str, Any] = Field(default_factory=dict)
