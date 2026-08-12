from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class Fact(BaseModel):
    id: str
    subject_id: str
    predicate: str
    value: Any
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: float | None = None
    evidence_ids: list[str] = Field(default_factory=list)
