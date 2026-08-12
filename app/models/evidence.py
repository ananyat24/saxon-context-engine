# Evidence: a pointer back to the original source material (an email, a CRM record,
# a PDF, a database row) that a Fact or Event was derived from. This is what lets
# the context engine answer "why do you believe that?" instead of just stating
# things as if they came from nowhere.
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    id: str

    # Where this evidence came from at a high level, e.g. "email", "crm_export", "pdf".
    source_type: str

    # id of the record in the source system, if it has one (e.g. a CRM record id).
    source_id: str | None = None

    # A link or path to the original source document, if available.
    source_uri: str | None = None

    # The specific snippet of text that supports the fact/event, so a human can
    # quickly see the relevant passage without opening the whole source document.
    excerpt: str | None = None

    created_at: datetime | None = None

    # Any additional source-specific detail that doesn't warrant its own field.
    metadata: dict[str, Any] = Field(default_factory=dict)
