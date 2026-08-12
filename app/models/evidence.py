from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    id: str
    source_type: str
    source_id: str | None = None
    source_uri: str | None = None
    excerpt: str | None = None
    created_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
