# The "unstructured documents" connector type -- represents pulling files
# from a live document store (SharePoint, Google Drive) via the same
# whole-file ingestion path app/ingestion/file_source.py's
# read_text_records() already uses for the sample datasets.
#
# Reads a small bundled folder of mock documents rather than an arbitrary
# tenant-supplied path, for the same path-traversal reason documented in
# app/ingestion/database_source.py.
from pathlib import Path

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
from app.ingestion.file_source import SourceRecord, read_text_records

_MOCK_DOCS_DIR = Path("data/samples/mock_docs")


def _load_documents() -> list[SourceRecord]:
    if not _MOCK_DOCS_DIR.exists():
        raise ConnectorFetchError(f"Mock document folder not found at '{_MOCK_DOCS_DIR}'.")
    records = list(read_text_records(_MOCK_DOCS_DIR, "Demo document store"))
    if not records:
        raise ConnectorFetchError("Mock document folder had no text files to ingest.")
    return records


class DocumentConnector(SourceConnector):
    """Demo unstructured-document connector: a small bundled folder of mock
    internal documents, standing in for a live SharePoint/Drive connection
    until one is available (see CLAUDE.md's v1 note on connector types)."""

    async def fetch(self) -> list[SourceRecord]:
        return _load_documents()

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        return "Demo document store (mock unstructured data)"
