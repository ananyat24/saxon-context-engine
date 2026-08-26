# The "structured database/CRM" connector type -- represents pulling rows
# from a live structured source (a CRM's API, a client's Postgres/MySQL
# database) via the same row-to-prose path app/ingestion/file_source.py's
# read_csv_records() already uses for the sample datasets.
#
# No live credentialed source is available yet (see CLAUDE.md's v1 note),
# so this reads a small bundled mock dataset rather than accepting an
# arbitrary path or URL from the request -- letting a tenant-supplied value
# choose which file on disk gets read would be a path-traversal /
# arbitrary-file-read risk for no real benefit while every account here is
# already fake demo data. Swapping this for a real DB/API client later is a
# contained change to _load_accounts() alone; nothing about the
# SourceConnector interface or the sync route needs to change.
from pathlib import Path

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
from app.ingestion.file_source import FileSourceSpec, SourceRecord, read_csv_records

_MOCK_CRM_PATH = Path("data/samples/mock_crm/accounts.csv")
_SPEC = FileSourceSpec("accounts.csv", "Account", "AccountID", name_column="CompanyName")


def _load_accounts() -> list[SourceRecord]:
    if not _MOCK_CRM_PATH.exists():
        raise ConnectorFetchError(f"Mock CRM dataset not found at '{_MOCK_CRM_PATH}'.")
    records = list(read_csv_records(_MOCK_CRM_PATH, _SPEC))
    if not records:
        raise ConnectorFetchError("Mock CRM dataset had no rows to ingest.")
    return records


class DatabaseConnector(SourceConnector):
    """Demo structured-source connector: a small bundled mock CRM accounts
    table, standing in for a live CRM/database connection until one is
    available (see CLAUDE.md's v1 note on connector types)."""

    async def fetch(self) -> list[SourceRecord]:
        return _load_accounts()

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        return "Demo CRM accounts (mock structured data)"
