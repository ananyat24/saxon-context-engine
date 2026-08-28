# The "structured database/CRM" connector type -- represents pulling rows
# from a live structured source (a CRM's API, a client's Postgres/MySQL
# database) via the same row-to-prose path app/ingestion/file_source.py's
# read_csv_records() already uses for the sample datasets.
#
# No live credentialed source is available yet (see CLAUDE.md's v1 note), so
# this reads CSVs from disk rather than a real DB/API client -- but which
# CSVs is no longer a single bundled demo folder shared by every tenant (see
# CLAUDE.md's follow-up on the "easily droppable CSV" gap). A connector now
# reads from its OWN upload folder (data/uploads/<connector_id>/, populated
# only by POST /connectors/{id}/files -- see app/api/connectors.py), falling
# back to the original bundled mock CRM dataset if nothing's been uploaded to
# it yet. The folder path is still never taken from a tenant-supplied value
# (connector_id is server-generated), so the original path-traversal /
# arbitrary-file-read concern this module's docstring used to flag doesn't
# apply to this either -- only the upload endpoint's own filename handling
# needs to guard against that, which it does (Path(...).name strips any
# directory components before a file ever touches disk).
import csv
from pathlib import Path
from typing import Optional

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
from app.ingestion.file_source import FileSourceSpec, SourceRecord, read_csv_records

_MOCK_CRM_DIR = Path("data/samples/mock_crm")
UPLOADS_ROOT = Path("data/uploads")

# accounts.csv is the original bundled dataset and keeps its hand-picked
# spec (record type + which column is the id/name) exactly as before. Any
# *other* CSV -- dropped into the demo folder, or uploaded to a connector --
# doesn't have a hand-picked spec, so _infer_spec() below guesses one from
# the header instead of requiring an exact filename/column match.
_KNOWN_SPECS: dict[str, FileSourceSpec] = {
    "accounts.csv": FileSourceSpec("accounts.csv", "Account", "AccountID", name_column="CompanyName"),
}


def connector_upload_dir(connector_id: str) -> Path:
    return UPLOADS_ROOT / connector_id


def _load_csvs_from(folder: Path) -> list[SourceRecord]:
    """Reads every CSV in `folder`, inferring a spec per file (see
    _infer_spec) unless it's one of the hand-picked _KNOWN_SPECS. Shared by
    the bundled-demo-folder path and the per-connector upload folder path
    below, so a dropped-in CSV is handled identically either way."""
    records: list[SourceRecord] = []
    for path in sorted(folder.glob("*.csv")):
        spec = _KNOWN_SPECS.get(path.name)
        if spec is None:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                header = next(csv.reader(f), [])
            if not header:
                continue
            spec = _infer_spec(path.name, header)
        records.extend(read_csv_records(path, spec))
    return records


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
    records = _load_csvs_from(_MOCK_CRM_DIR)
    if not records:
        raise ConnectorFetchError("Mock CRM dataset(s) had no rows to ingest.")
    return records


class DatabaseConnector(SourceConnector):
    """Structured-source connector: ingests whatever CSVs have been uploaded
    to this specific connector (POST /connectors/{connector_id}/files --
    see app/api/connectors.py), one file per record type, the same layout
    the bundled sample datasets use (see data/samples/northwind/, etc.).
    Falls back to the bundled demo CRM dataset (data/samples/mock_crm/) when
    nothing's been uploaded to this connector yet, so an existing "Database
    / CRM (demo data)" connector that was never given real files keeps
    working exactly as it did before uploads existed."""

    def __init__(self, connector_id: str = ""):
        self._connector_id = connector_id

    def _upload_dir(self) -> Optional[Path]:
        # No id yet (see app/api/connectors.py's pre-creation validation
        # call, which probes a type's factory with {} before the connector
        # row -- and its id -- exists) -- nothing to look up, always fall
        # back to the bundled demo dataset rather than resolving to the
        # shared uploads root itself.
        return connector_upload_dir(self._connector_id) if self._connector_id else None

    async def fetch(self) -> list[SourceRecord]:
        upload_dir = self._upload_dir()
        if upload_dir is not None and upload_dir.is_dir() and any(upload_dir.glob("*.csv")):
            records = _load_csvs_from(upload_dir)
            if records:
                return records
        return _load_accounts()

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        upload_dir = self._upload_dir()
        if upload_dir is not None and upload_dir.is_dir() and any(upload_dir.glob("*.csv")):
            return "Uploaded CSV data"
        return "Demo CRM accounts (mock structured data)"
