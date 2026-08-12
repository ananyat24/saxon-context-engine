from typing import Any, Dict


class StructuredIngestor:
    """Ingests structured payload dictionaries into natural language episodes."""

    def format_as_text(self, record: Dict[str, Any], record_type: str) -> str:
        fields = ", ".join([f"{k}: {v}" for k, v in record.items()])
        return f"{record_type} Record: {fields}"
