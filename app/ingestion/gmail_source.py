# The "gmail" connector type: pulls recent messages from one live Gmail
# inbox via the Gmail API, into the same SourceRecord shape every other
# source in this codebase produces.
#
# Authenticates with the same Google Cloud service account as the
# "google_drive" connector type (app/config.py's
# google_drive_service_account_json), but reading a mailbox needs a
# different access model than reading a shared Drive folder: there's no
# "share this inbox with the service account" action a user can take the way
# there is for a Drive folder. Instead this uses domain-wide delegation:
# a Google Workspace admin authorizes the service account (by its numeric
# client id, in the Workspace Admin console under Security -> API controls ->
# Domain-wide Delegation) to impersonate any user in the domain for the
# gmail.readonly scope, and this connector then acts as whichever mailbox its
# "url" field names (see Credentials.with_subject() below). That's an
# org-wide grant, same shape as SharePoint's Sites.Read.All: whoever
# authorizes it is authorizing this connector type to read any mailbox in
# the Workspace domain, not just the one a connector happens to point at.
# Domain-wide delegation is a Google Workspace (paid) feature; it isn't
# available for a plain personal Gmail account.
import asyncio
import base64
import json
import re
from typing import Iterator, Optional

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from app.config import settings
from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
from app.ingestion.file_source import SourceRecord
from app.ingestion.html_text import html_to_text

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"

# Keeps a single sync's cost and runtime bounded regardless of how full the
# mailbox is, same reasoning as the SharePoint/Drive connectors' file caps.
_MAX_MESSAGES = 20
_MAX_TEXT_CHARS = 5_000

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _decode_body_data(data: str) -> str:
    # Gmail's body data is URL-safe base64 without padding.
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _flatten_parts(payload: dict) -> Iterator[dict]:
    yield payload
    for part in payload.get("parts") or []:
        yield from _flatten_parts(part)


def _extract_body_text(payload: dict) -> str:
    """A message can be single-part or a MIME tree; prefer a text/plain part
    anywhere in that tree, falling back to text/html (stripped) if that's
    all there is, the same preference order a mail client uses."""
    parts = list(_flatten_parts(payload))
    for p in parts:
        if p.get("mimeType") == "text/plain" and (p.get("body") or {}).get("data"):
            return _decode_body_data(p["body"]["data"]).strip()
    for p in parts:
        if p.get("mimeType") == "text/html" and (p.get("body") or {}).get("data"):
            return html_to_text(_decode_body_data(p["body"]["data"]))
    return ""


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers") or []:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


class GmailConnector(SourceConnector):
    def __init__(self, mailbox: str):
        self.mailbox = mailbox.strip()
        if not _EMAIL_RE.match(self.mailbox):
            raise ConnectorFetchError(f"'{mailbox}' doesn't look like a mailbox address.")

    def _get_access_token(self) -> str:
        """Blocking (google-auth's own refresh() call is synchronous):
        always run this via asyncio.to_thread, never awaited directly."""
        raw = settings.google_drive_service_account_json
        if not raw:
            raise ConnectorFetchError(
                "Gmail isn't configured on this server -- ask your operator to set "
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON (the same service account Google Drive uses)."
            )
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConnectorFetchError(f"Google service account credentials are misconfigured: {e}") from e
        try:
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=_GMAIL_SCOPES
            ).with_subject(self.mailbox)
            credentials.refresh(GoogleAuthRequest())
        except ConnectorFetchError:
            raise
        except Exception as e:
            raise ConnectorFetchError(
                f"Could not authenticate to Gmail as '{self.mailbox}': {e}. This needs domain-wide "
                "delegation for the gmail.readonly scope, granted to the service account in the "
                "Google Workspace Admin console -- a per-folder share (what Drive uses) isn't enough."
            ) from e
        return credentials.token

    async def fetch(self) -> list[SourceRecord]:
        token = await asyncio.to_thread(self._get_access_token)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            message_ids = await self._list_message_ids(client, headers)
            records = []
            for message_id in message_ids:
                record = await self._fetch_message_as_record(client, headers, message_id)
                if record is not None:
                    records.append(record)
        if not records:
            raise ConnectorFetchError(f"No readable messages found in '{self.mailbox}'s inbox.")
        return records

    async def _list_message_ids(self, client: httpx.AsyncClient, headers: dict) -> list[str]:
        try:
            resp = await client.get(
                f"{_GMAIL_BASE}/users/{self.mailbox}/messages",
                headers=headers,
                params={"maxResults": _MAX_MESSAGES, "labelIds": "INBOX"},
            )
        except httpx.HTTPError as e:
            raise ConnectorFetchError(f"Could not reach Gmail: {e}") from e
        if resp.status_code == 404:
            raise ConnectorFetchError(f"No mailbox found for '{self.mailbox}'.")
        if resp.status_code == 403:
            raise ConnectorFetchError(
                "Access denied -- the service account needs domain-wide delegation for the "
                "gmail.readonly scope, granted in the Google Workspace Admin console."
            )
        if resp.status_code >= 400:
            raise ConnectorFetchError(f"Gmail returned an error listing messages (HTTP {resp.status_code}).")
        return [m["id"] for m in resp.json().get("messages", []) if "id" in m]

    async def _fetch_message_as_record(
        self, client: httpx.AsyncClient, headers: dict, message_id: str
    ) -> Optional[SourceRecord]:
        try:
            resp = await client.get(
                f"{_GMAIL_BASE}/users/{self.mailbox}/messages/{message_id}",
                headers=headers,
                params={"format": "full"},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            # One unreadable message shouldn't fail the whole inbox's sync.
            return None

        payload = resp.json().get("payload") or {}
        subject = _header(payload, "Subject") or "(no subject)"
        sender = _header(payload, "From") or "unknown sender"
        recipients = _header(payload, "To")
        date = _header(payload, "Date")

        try:
            body_text = _extract_body_text(payload)
        except Exception:
            return None

        descriptor_lines = [f"Email from {sender}", f'Subject: "{subject}"']
        if recipients:
            descriptor_lines.append(f"To: {recipients}")
        if date:
            descriptor_lines.append(f"Date: {date}")
        descriptor_lines.append("")
        descriptor_lines.append(body_text)
        text = "\n".join(descriptor_lines).strip()
        if not body_text:
            return None
        if len(text) > _MAX_TEXT_CHARS:
            text = text[:_MAX_TEXT_CHARS] + "\n\n[truncated -- message content exceeded the ingest size cap]"

        return SourceRecord(
            name=f"gmail-{message_id}",
            body=text,
            source_description=f"Gmail inbox ({self.mailbox}, \"{subject}\")",
        )

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        return f"Gmail inbox ({self.mailbox})"
