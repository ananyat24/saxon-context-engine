# Graphiti's extraction is built for natural-language text, not structured rows.
# This turns a structured record (e.g. a CRM export row) into a plain-English
# sentence so it can be handed to IngestionPipeline.ingest_episode() like any
# other piece of text -- see scripts/test_graph.py for an example of what the
# resulting text looks like when hand-written for a similar CRM/ERP scenario.
from typing import Any


class StructuredIngestor:
    """Converts structured payload dictionaries into natural-language episode text."""

    def format_as_text(self, record: dict[str, Any], record_type: str) -> str:
        fields = ", ".join(f"{k}: {v}" for k, v in record.items())
        return f"{record_type} Record: {fields}"
