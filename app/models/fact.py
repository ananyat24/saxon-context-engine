# Fact: a single, timestamped statement about an entity, e.g. "account-42's status
# is 'active', valid from 2024-01-01 onward". Facts are how this system tracks things
# that change over time (a "temporal" record) instead of just overwriting old values.
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class Fact(BaseModel):
    id: str

    # The entity this fact describes, e.g. "account-42".
    subject_id: str

    # What kind of statement this is, e.g. "status" or "account_owner".
    predicate: str

    # The actual value being asserted. Typed as `Any` because a fact's value could be
    # a string, a number, a date, etc. depending on the predicate -- there's no single
    # type that fits every kind of fact.
    value: Any

    # The time window during which this fact is considered true. Leaving valid_to
    # unset means "still true as of now"; setting it marks the fact as superseded
    # rather than deleting it, so the history stays intact.
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    # How confident the system is that this fact is correct (0.0-1.0), useful when
    # facts come from automated extraction rather than a verified source system.
    confidence: float | None = None

    # ids of Evidence records (see evidence.py) that back up this fact -- lets you
    # trace a statement back to the document, message, or record it came from.
    evidence_ids: list[str] = Field(default_factory=list)
