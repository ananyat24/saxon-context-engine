# Event: something that happened at a specific point in time, e.g. "order shipped"
# or "contract signed". Distinct from a Fact (a state that holds over a period of
# time) -- an Event is a discrete occurrence.
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class Event(BaseModel):
    id: str

    # What kind of event this is, e.g. "Transaction" or "Decision". Should match
    # one of the event_types defined in the ontology.
    event_type: str

    timestamp: datetime | None = None

    # ids of entities that took part in the event (e.g. the people involved in a
    # meeting), as opposed to related_entities below, which is a looser association.
    participants: list[str] = Field(default_factory=list)

    # ids of any other entities this event touches or affects, without implying
    # active participation (e.g. the account a support ticket was filed against).
    related_entities: list[str] = Field(default_factory=list)

    properties: dict[str, Any] = Field(default_factory=dict)

    # ids of Evidence records that document this event actually happened.
    evidence_ids: list[str] = Field(default_factory=list)
