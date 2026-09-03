# The "outlook_mail" connector type: pulls recent messages from one live
# Microsoft 365 mailbox's inbox via Microsoft Graph, into the same
# SourceRecord shape every other source in this codebase produces.
#
# Authenticates with the same Azure AD (Entra ID) app registration as the
# "sharepoint" connector type (app/config.py's sharepoint_tenant_id/
# client_id/client_secret) via the OAuth2 client credentials flow: one
# operator-wide app registration backs both connector types, not a second
# credential to configure. The app registration additionally needs Microsoft
# Graph's Mail.Read application permission (admin-consented) for this
# connector type specifically; Sites.Read.All alone (what SharePoint needs)
# isn't enough, see app/config.py's docstring for both.
#
# Like SharePoint's Sites.Read.All, Mail.Read as an application permission is
# an org-wide grant: once consented, this connector type can read any
# mailbox in the tenant Graph is asked for, not just the one a connector
# happens to point at. The connector's "url" field holds which mailbox
# (a user's email address) to read; that's the only thing scoping a given
# connector to one inbox.
import re

import httpx

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
from app.ingestion.file_source import SourceRecord
from app.ingestion.graph_auth import get_graph_access_token
from app.ingestion.html_text import html_to_text

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Keeps a single sync's cost and runtime bounded regardless of how full the
# mailbox is, same reasoning as the SharePoint/Drive connectors' file caps.
_MAX_MESSAGES = 20
_MAX_TEXT_CHARS = 5_000

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OutlookMailConnector(SourceConnector):
    def __init__(self, mailbox: str):
        self.mailbox = mailbox.strip()
        if not _EMAIL_RE.match(self.mailbox):
            raise ConnectorFetchError(f"'{mailbox}' doesn't look like a mailbox address.")

    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        return await get_graph_access_token(client, missing_permission_hint="Mail.Read")

    async def fetch(self) -> list[SourceRecord]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token = await self._get_access_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            try:
                resp = await client.get(
                    f"{_GRAPH_BASE}/users/{self.mailbox}/mailFolders/inbox/messages",
                    headers=headers,
                    params={
                        "$top": _MAX_MESSAGES,
                        "$orderby": "receivedDateTime desc",
                        "$select": "id,subject,from,toRecipients,receivedDateTime,body",
                    },
                )
            except httpx.HTTPError as e:
                raise ConnectorFetchError(f"Could not reach Microsoft Graph: {e}") from e
            if resp.status_code == 404:
                raise ConnectorFetchError(f"No mailbox found for '{self.mailbox}'.")
            if resp.status_code == 403:
                raise ConnectorFetchError(
                    "Access denied -- the app registration needs the Mail.Read Microsoft Graph "
                    "application permission, admin-consented."
                )
            if resp.status_code >= 400:
                raise ConnectorFetchError(f"Microsoft Graph returned an error listing messages (HTTP {resp.status_code}).")
            messages = resp.json().get("value", [])

        records = [r for m in messages if (r := self._message_to_record(m)) is not None]
        if not records:
            raise ConnectorFetchError(f"No readable messages found in '{self.mailbox}'s inbox.")
        return records

    def _message_to_record(self, m: dict) -> SourceRecord | None:
        message_id = m.get("id")
        if not message_id:
            return None
        subject = m.get("subject") or "(no subject)"
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "unknown sender")
        recipients = ", ".join(
            (r.get("emailAddress") or {}).get("address", "") for r in m.get("toRecipients", [])
        )
        received = m.get("receivedDateTime", "")

        body_obj = m.get("body") or {}
        raw_body = body_obj.get("content", "")
        body_text = html_to_text(raw_body) if body_obj.get("contentType") == "html" else raw_body.strip()

        descriptor_lines = [f"Email from {sender}", f'Subject: "{subject}"']
        if recipients:
            descriptor_lines.append(f"To: {recipients}")
        if received:
            descriptor_lines.append(f"Date: {received}")
        descriptor_lines.append("")
        descriptor_lines.append(body_text)
        text = "\n".join(descriptor_lines).strip()
        if not text:
            return None
        if len(text) > _MAX_TEXT_CHARS:
            text = text[:_MAX_TEXT_CHARS] + "\n\n[truncated -- message content exceeded the ingest size cap]"

        return SourceRecord(
            name=f"outlook-{message_id}",
            body=text,
            source_description=f"Outlook inbox ({self.mailbox}, \"{subject}\")",
        )

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        return f"Outlook mailbox ({self.mailbox})"
