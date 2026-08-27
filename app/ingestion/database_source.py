# The "structured database/CRM" connector type -- represents pulling rows
# from a live structured source (a CRM's API, a client's Postgres/MySQL
# database) via the same row-to-prose path app/ingestion/file_source.py's
# read_csv_records() already uses for the sample datasets.
#
# No live credentialed source is available yet (see CLAUDE.md's v1 note),
# so this reads bundled mock datasets rather than accepting an arbitrary path
# or URL from the request -- letting a tenant-supplied value choose which
# file on disk gets read would be a path-traversal / arbitrary-file-read risk
# for no real benefit while every account here is already fake demo data.
# Swapping this for a real DB/API client later is a contained change to
# _load_accounts() alone; nothing about the SourceConnector interface or the
# sync route needs to change.
import csv
from pathlib import Path

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
from app.ingestion.file_source import FileSourceSpec, SourceRecord, read_csv_records

_MOCK_CRM_DIR = Path("data/samples/mock_crm")

# accounts.csv is the original bundled dataset and keeps its hand-picked
# spec (record type + which column is the id/name) exactly as before. Any
# *other* CSV dropped into the same folder -- e.g. swapping in a client's own
# mock data -- doesn't have a hand-picked spec, so _infer_spec() below guesses
# one from the header instead of requiring an exact filename/column match.
_KNOWN_SPECS: dict[str, FileSourceSpec] = {
    "accounts.csv": FileSourceSpec("accounts.csv", "Account", "AccountID", name_column="CompanyName"),
}


def _infer_spec(filename: str, header: list[str]) -> FileSourceSpec:
    """Best-effort FileSourceSpec for a CSV with no hand-picked spec: the
    record type comes from the filename (accounts.csv -> Account), the id
    column is whichever header cell looks like an id (ending in "id", or
    just "id"), falling back to the first column, and the name column is
    whichever header cell mentions "name", if any."""
    stem = Path(filename).stem.replace("-", " ").replace("_", " ").title().replace(" ", "")
    record_type = stem[:-1] if stem.lower().endswith("s") and not stem.lower().endswith("ss") else stem
    id_column = next(
        (c for c in header if c.strip().lower() == "id" or c.strip().lower().endswith("id")), header[0]
    )
    name_column = next((c for c in header if "name" in c.strip().lower()), None)
    return FileSourceSpec(filename, record_type or "Record", id_column, name_column=name_column)


def _load_accounts() -> list[SourceRecord]:
    if not _MOCK_CRM_DIR.exists():
        raise ConnectorFetchError(f"Mock CRM folder not found at '{_MOCK_CRM_DIR}'.")
    csv_paths = sorted(_MOCK_CRM_DIR.glob("*.csv"))
    if not csv_paths:
        raise ConnectorFetchError(f"Mock CRM folder '{_MOCK_CRM_DIR}' had no CSV files to ingest.")

    records: list[SourceRecord] = []
    for path in csv_paths:
        spec = _KNOWN_SPECS.get(path.name)
        if spec is None:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f), [])
            if not header:
                continue
            spec = _infer_spec(path.name, header)
        records.extend(read_csv_records(path, spec))

    if not records:
        raise ConnectorFetchError("Mock CRM dataset(s) had no rows to ingest.")
    return records


class DatabaseConnector(SourceConnector):
    """Demo structured-source connector: every CSV bundled under
    data/samples/mock_crm/, standing in for a live CRM/database connection
    until one is available (see CLAUDE.md's v1 note on connector types)."""

    async def fetch(self) -> list[SourceRecord]:
        return _load_accounts()

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        return "Demo CRM accounts (mock structured data)"
