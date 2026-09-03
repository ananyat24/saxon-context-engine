# The "sharepoint" connector type: pulls text content from every
# supported file in one SharePoint site's default document library, via
# Microsoft Graph, into the same SourceRecord shape every other source in
# this codebase produces.
#
# Authenticates as an Azure AD (Entra ID) app registration using the OAuth2
# client credentials flow (see app/config.py's sharepoint_tenant_id/
# client_id/client_secret), the SharePoint/Graph equivalent of the Google
# Drive connector's service account: no interactive user login, works
# headless for a server-side "Sync now"/scheduled sync.
#
# One real difference from Drive worth calling out, not glossing over:
# Drive's access boundary is per-folder (the service account only sees a
# folder once someone explicitly shares it). This app registration's access
# boundary is a single org-wide Microsoft Graph application permission
# (Sites.Read.All, admin-consented): once granted, this connector type can
# read any SharePoint site in the tenant, not just the ones a connector
# happens to be pointed at. Whoever grants that permission is making that
# org-wide call, not a per-site one.
import asyncio
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
from app.ingestion.document_text_extraction import BINARY_TEXT_PARSERS, MAX_BINARY_BYTES, PLAIN_TEXT_MIME_TYPES
from app.ingestion.file_source import SourceRecord
from app.ingestion.graph_auth import get_graph_access_token

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Keeps a single sync's cost and runtime bounded regardless of how large the
# document library is, same reasoning as the Google Drive connector's caps.
_MAX_FILES = 20
_MAX_TEXT_CHARS = 20_000

# Graph sometimes reports a generic/blank mimeType for a plain-text-ish file
# (a known quirk for less common extensions like .md), so extension is
# checked as a fallback alongside mimeType, not instead of it.
_TEXT_EXTENSIONS = (".txt", ".md", ".csv")


def _parse_site_url(url: str) -> tuple[str, str]:
    """Splits a SharePoint site URL like
    https://contoso.sharepoint.com/sites/Marketing into (hostname, site_path),
    the two pieces Graph's /sites/{hostname}:/{site-path} lookup needs."""
    parsed = urlparse(url.strip())
    hostname = parsed.hostname
    path = parsed.path.strip("/")
    if not hostname or not path:
        raise ConnectorFetchError(f"'{url}' doesn't look like a valid SharePoint site URL.")
    return hostname, path


class SharePointConnector(SourceConnector):
    def __init__(self, site_url: str):
        self.site_url = site_url.strip()
        self.hostname, self.site_path = _parse_site_url(self.site_url)

    async def _get_access_token(self, client: httpx.AsyncClient) -> str:
        return await get_graph_access_token(client, missing_permission_hint="Sites.Read.All")

    async def _resolve_drive_id(self, client: httpx.AsyncClient, headers: dict) -> str:
        try:
            site_resp = await client.get(f"{_GRAPH_BASE}/sites/{self.hostname}:/{self.site_path}", headers=headers)
        except httpx.HTTPError as e:
            raise ConnectorFetchError(f"Could not reach Microsoft Graph: {e}") from e
        if site_resp.status_code == 404:
            raise ConnectorFetchError(f"No SharePoint site found at '{self.site_url}'.")
        if site_resp.status_code == 403:
            raise ConnectorFetchError(
                "Access denied -- the app registration needs the Sites.Read.All Microsoft Graph "
                "application permission, admin-consented."
            )
        if site_resp.status_code >= 400:
            raise ConnectorFetchError(f"Microsoft Graph returned an error looking up the site (HTTP {site_resp.status_code}).")
        site_id = site_resp.json().get("id")
        if not site_id:
            raise ConnectorFetchError(f"Could not resolve a site id for '{self.site_url}'.")

        drive_resp = await client.get(f"{_GRAPH_BASE}/sites/{site_id}/drive", headers=headers)
        if drive_resp.status_code >= 400:
            raise ConnectorFetchError(
                f"Microsoft Graph returned an error looking up that site's document library "
                f"(HTTP {drive_resp.status_code})."
            )
        drive_id = drive_resp.json().get("id")
        if not drive_id:
            raise ConnectorFetchError(f"'{self.site_url}' doesn't have a document library Graph could find.")
        return drive_id

    async def _list_files(self, client: httpx.AsyncClient, headers: dict, drive_id: str) -> list[dict]:
        try:
            resp = await client.get(
                f"{_GRAPH_BASE}/drives/{drive_id}/root/children",
                headers=headers,
                params={"$top": _MAX_FILES},
            )
        except httpx.HTTPError as e:
            raise ConnectorFetchError(f"Could not reach Microsoft Graph: {e}") from e
        if resp.status_code >= 400:
            raise ConnectorFetchError(f"Microsoft Graph returned an error listing files (HTTP {resp.status_code}).")
        return resp.json().get("value", [])

    async def fetch(self) -> list[SourceRecord]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            token = await self._get_access_token(client)
            headers = {"Authorization": f"Bearer {token}"}
            drive_id = await self._resolve_drive_id(client, headers)
            files = await self._list_files(client, headers, drive_id)

            records = []
            for f in files:
                record = await self._fetch_file_as_record(client, headers, drive_id, f)
                if record is not None:
                    records.append(record)

        if not records:
            raise ConnectorFetchError(
                "No supported files found in that site's document library -- only plain text, "
                "Markdown, CSV, PDF, and Word (.docx) files are read today (and a scanned/"
                "image-only PDF has no extractable text)."
            )
        return records

    async def _fetch_file_as_record(
        self, client: httpx.AsyncClient, headers: dict, drive_id: str, f: dict
    ) -> Optional[SourceRecord]:
        item_id = f.get("id")
        name = f.get("name", item_id)
        if not item_id or "folder" in f:
            return None  # not recursing into subfolders in this MVP

        mime = (f.get("file") or {}).get("mimeType", "")
        is_plain_text = mime in PLAIN_TEXT_MIME_TYPES or name.lower().endswith(_TEXT_EXTENSIONS)
        parser = BINARY_TEXT_PARSERS.get(mime)
        if not is_plain_text and not parser:
            return None  # unsupported type: skip rather than guess

        try:
            resp = await client.get(f"{_GRAPH_BASE}/drives/{drive_id}/items/{item_id}/content", headers=headers)
            resp.raise_for_status()
        except httpx.HTTPError:
            # One unreadable file shouldn't fail the whole library's sync.
            return None

        try:
            if parser:
                if len(resp.content) > MAX_BINARY_BYTES:
                    return None
                text = (await asyncio.to_thread(parser, resp.content)).strip()
            else:
                text = resp.text.strip()
        except Exception:
            # A corrupt/malformed file, or a parsing library error, gets
            # the same "skip this one file" treatment, not a whole-sync
            # failure.
            return None

        if not text:
            return None
        if len(text) > _MAX_TEXT_CHARS:
            text = text[:_MAX_TEXT_CHARS] + "\n\n[truncated -- file content exceeded the ingest size cap]"

        return SourceRecord(
            name=f"sharepoint-{item_id}",
            body=text,
            source_description=f"SharePoint ({name})",
        )

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        return f"SharePoint site ({self.site_url})"
