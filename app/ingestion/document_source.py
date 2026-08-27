# The "unstructured documents" connector type -- represents pulling files
# from a live document store (SharePoint, Google Drive) via the same
# whole-file ingestion path app/ingestion/file_source.py's
# read_text_records() already uses for the sample datasets.
#
# Reads a small bundled folder of mock documents rather than an arbitrary
# tenant-supplied path, for the same path-traversal reason documented in
# app/ingestion/database_source.py.
#
# Picks up .txt directly, plus .pdf/.docx via the same parsers the live
# Google Drive/SharePoint connectors use (app/ingestion/document_text_extraction.py)
# -- so dropping a client's real-shaped mock documents (a PDF policy doc, a
# Word onboarding guide) in alongside/instead of the bundled .txt files works
# without needing to convert them to plain text first.
from pathlib import Path

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
from app.ingestion.document_text_extraction import (
    BINARY_TEXT_PARSERS as _BINARY_PARSERS,
    MAX_BINARY_BYTES as _MAX_BINARY_BYTES,
    DOCX_MIME as _DOCX_MIME,
    PDF_MIME as _PDF_MIME,
)
from app.ingestion.file_source import SourceRecord, read_text_records
from app.ingestion.unstructured import UnstructuredIngestor

_MOCK_DOCS_DIR = Path("data/samples/mock_docs")

_EXTENSION_MIME = {".pdf": _PDF_MIME, ".docx": _DOCX_MIME}


def _load_binary_documents() -> list[SourceRecord]:
    ingestor = UnstructuredIngestor()
    records: list[SourceRecord] = []
    for ext, mime in _EXTENSION_MIME.items():
        for path in sorted(_MOCK_DOCS_DIR.glob(f"*{ext}")):
            data = path.read_bytes()
            if len(data) > _MAX_BINARY_BYTES:
                continue
            text = ingestor.clean_text(_BINARY_PARSERS[mime](data))
            if not text:
                continue
            records.append(
                SourceRecord(name=path.stem, body=text, source_description=f"Demo document store ({path.name})")
            )
    return records


def _load_documents() -> list[SourceRecord]:
    if not _MOCK_DOCS_DIR.exists():
        raise ConnectorFetchError(f"Mock document folder not found at '{_MOCK_DOCS_DIR}'.")
    records = list(read_text_records(_MOCK_DOCS_DIR, "Demo document store")) + _load_binary_documents()
    if not records:
        raise ConnectorFetchError("Mock document folder had no text, PDF, or DOCX files to ingest.")
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
