# The "email/inbox" connector type -- represents pulling messages from a
# live mailbox (Gmail, Microsoft Graph) via a minimal From/To/Subject/Date
# header format, parsed into the same SourceRecord shape every other source
# in this codebase produces.
#
# Reads a small bundled folder of mock emails rather than an arbitrary
# tenant-supplied path, for the same path-traversal reason documented in
# app/ingestion/database_source.py.
import re
from pathlib import Path

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
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


class EmailConnector(SourceConnector):
    """Demo email connector: a small bundled folder of mock messages,
    standing in for a live Gmail/Microsoft Graph connection until one is
    available (see CLAUDE.md's v1 note on connector types)."""

    async def fetch(self) -> list[SourceRecord]:
        return _load_emails()

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        return "Demo inbox (mock email data)"
