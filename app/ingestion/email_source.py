# The "email/inbox" connector type -- represents pulling messages from a
# live mailbox (Gmail, Microsoft Graph) via a minimal From/To/Subject/Date
# header format, parsed into the same SourceRecord shape every other source
# in this codebase produces.
#
# Like "database"/DatabaseConnector, this connector now also reads from its
# own upload folder (data/uploads/<connector_id>/, populated by
# POST /connectors/{id}/files -- see app/api/connectors.py) before falling
# back to the bundled mock inbox -- a real client-supplied email export
# (Gmail/Outlook "download my data" style: a JSON array of
# {from, to, subject, date, body} objects) is dropped in the same way an
# uploaded CSV is for the database connector, rather than needing a live
# OAuth-connected mailbox (the "gmail"/"outlook_mail" connector types) just
# to get an existing export ingested.
import json
import re
from pathlib import Path
from typing import Optional

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
from app.ingestion.database_source import connector_upload_dir
from app.ingestion.file_source import SourceRecord

_MOCK_EMAIL_DIR = Path("data/samples/mock_email")

# Matches a simple "Header: value" line at the start of a message -- good
# enough for the bundled mock .txt files (a real Gmail/Graph connector would
# get these fields structured from the API instead of parsing text).
_HEADER_RE = re.compile(r"^(From|To|Subject|Date):\s*(.*)$")


def _parse_email(path: Path) -> SourceRecord:
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()

    headers: dict[str, str] = {}
    body_start = 0
    for i, line in enumerate(lines):
        match = _HEADER_RE.match(line)
        if match:
            headers[match.group(1)] = match.group(2).strip()
            body_start = i + 1
        elif line.strip() == "":
            body_start = i + 1
            break
        else:
            break

    body_text = "\n".join(lines[body_start:]).strip()
    subject = headers.get("Subject", path.stem)
    sender = headers.get("From", "unknown sender")
    date = headers.get("Date", "")

    descriptor_lines = [f"Email from {sender}", f'Subject: "{subject}"']
    if headers.get("To"):
        descriptor_lines.append(f"To: {headers['To']}")
    if date:
        descriptor_lines.append(f"Date: {date}")
    descriptor_lines.append("")
    descriptor_lines.append(body_text)

    return SourceRecord(
        name=f"email-{path.stem}",
        body="\n".join(descriptor_lines),
        source_description=f"Demo inbox ({path.name})",
    )


def _load_emails() -> list[SourceRecord]:
    if not _MOCK_EMAIL_DIR.exists():
        raise ConnectorFetchError(f"Mock email folder not found at '{_MOCK_EMAIL_DIR}'.")
    records = [_parse_email(p) for p in sorted(_MOCK_EMAIL_DIR.glob("*.txt"))]
    if not records:
        raise ConnectorFetchError("Mock email folder had no messages to ingest.")
    return records


def _message_to_record(msg: dict, source_label: str, index: int) -> Optional[SourceRecord]:
    """One {from, to, subject, date, body} object (a single element of an
    uploaded export's JSON array) -> a SourceRecord, same descriptive shape
    _parse_email builds for the .txt mock format above so extraction sees
    consistent input regardless of which path produced it."""
    body_text = str(msg.get("body", "")).strip()
    if not body_text:
        return None
    subject = str(msg.get("subject", "")).strip() or "(no subject)"
    sender = str(msg.get("from", "")).strip() or "unknown sender"
    recipient = str(msg.get("to", "")).strip()
    date = str(msg.get("date", "")).strip()

    descriptor_lines = [f"Email from {sender}", f'Subject: "{subject}"']
    if recipient:
        descriptor_lines.append(f"To: {recipient}")
    if date:
        descriptor_lines.append(f"Date: {date}")
    descriptor_lines.append("")
    descriptor_lines.append(body_text)

    return SourceRecord(
        name=f"email-{source_label}-{index}",
        body="\n".join(descriptor_lines),
        source_description=f"{source_label} ({subject})",
    )


def _load_json_exports_from(folder: Path, source_label: str) -> list[SourceRecord]:
    """Reads every *.json file in `folder` as an array of
    {from, to, subject, date, body} objects -- the shape Gmail/Outlook
    "export your data" tooling (or a small script hitting either API) would
    reasonably produce. A file that isn't a JSON array of objects is skipped
    rather than failing the whole folder -- one malformed export shouldn't
    block every other one uploaded alongside it."""
    records: list[SourceRecord] = []
    for path in sorted(folder.glob("*.json")):
        try:
            messages = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(messages, list):
            continue
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            record = _message_to_record(msg, source_label, f"{path.stem}-{i}")
            if record is not None:
                records.append(record)
    return records


class EmailConnector(SourceConnector):
    """Reads whatever's been uploaded to this specific connector
    (POST /connectors/{connector_id}/files -- a JSON array export, one file
    per batch/day) -- see this module's own docstring. Falls back to the
    bundled mock inbox (data/samples/mock_email/) when nothing's been
    uploaded yet, same "own folder, demo fallback" pattern
    DatabaseConnector uses. `source_label` distinguishes an otherwise
    identical "email" connector's own uploaded messages in the UI/evidence
    (e.g. "Gmail" vs "Outlook") -- purely descriptive, doesn't change how
    the upload folder is resolved."""

    def __init__(self, connector_id: str = "", source_label: str = "Email"):
        self._connector_id = connector_id
        self._source_label = source_label

    def _upload_dir(self) -> Optional[Path]:
        return connector_upload_dir(self._connector_id) if self._connector_id else None

    async def fetch(self) -> list[SourceRecord]:
        upload_dir = self._upload_dir()
        if upload_dir is not None and upload_dir.is_dir() and any(upload_dir.glob("*.json")):
            records = _load_json_exports_from(upload_dir, self._source_label)
            if records:
                return records
        return _load_emails()

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        upload_dir = self._upload_dir()
        if upload_dir is not None and upload_dir.is_dir() and any(upload_dir.glob("*.json")):
            return f"{self._source_label} (uploaded export)"
        return "Demo inbox (mock email data)"
