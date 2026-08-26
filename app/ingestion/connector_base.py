# The interface every source connector type implements (web today; SharePoint,
# Google Drive, a CRM API, etc. are meant to slot in later as new classes
# registered in app/api/connectors.py's dispatch table, without touching that
# route, IngestionPipeline, or ontology handling).
#
# Kept deliberately small: a connector's only job is turning its source's
# current content into SourceRecords (the same shape every other ingestion
# path in this codebase already produces -- see app/ingestion/file_source.py)
# and producing a fingerprint of that content, so app/api/connectors.py's
# sync flow, and the content-hash dedup-before-extraction guard, work
# identically regardless of source type.
import hashlib
from abc import ABC, abstractmethod

from app.ingestion.file_source import SourceRecord


class ConnectorFetchError(Exception):
    """A connector couldn't reach its source, or the source returned nothing
    usable. Raised by fetch() instead of a source-specific/library exception,
    so app/api/connectors.py's sync flow can catch one error type regardless
    of which connector type is running."""


class SourceConnector(ABC):
    @abstractmethod
    async def fetch(self) -> list[SourceRecord]:
        """Pulls this connector's current content and returns it as one or
        more SourceRecords, ready for IngestionPipeline.ingest_episode().
        Raises ConnectorFetchError on anything that should stop a sync."""

    @abstractmethod
    def content_hash(self, records: list[SourceRecord]) -> str:
        """A cheap fingerprint of `records` (already the result of fetch()),
        used to skip re-ingesting -- and re-paying for extraction on -- a
        sync that found no real change since the last one."""

    @abstractmethod
    def source_description(self) -> str:
        """A short human-readable description of where this connector reads
        from, e.g. for error messages and logging."""


def hash_records(records: list[SourceRecord]) -> str:
    """Shared content_hash() implementation for a connector that fetches
    several records at once (database rows, a folder of documents/emails):
    a fingerprint of every record's body, in order, so any addition, removal,
    or edit among them changes the hash -- not just an edit to one record
    that happens to be first."""
    if not records:
        return ""
    joined = "\x1e".join(r.body for r in records)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
