# Two "google_drive*" connector types, both reading the same subset of a
# user's Drive content into the same SourceRecord shape every other source
# in this codebase produces, but authenticating completely differently:
#
# - GoogleDriveConnector ("google_drive"): a Google Cloud service account
#   (see app/config.py's google_drive_service_account_json), not through any
#   individual user's own Google login. A service account needs no
#   interactive OAuth consent screen, which is what makes a server-side
#   "Sync now"/scheduled sync possible with zero per-connector setup beyond
#   sharing one folder, but getting a client to actually share a folder
#   with a service account's email is real onboarding friction.
#
# - GoogleDriveOAuthConnector ("google_drive_oauth"): a real per-user OAuth
#   consent flow, the one-click "Connect Google Drive" button (see
#   app/api/connectors.py's oauth/exchange + oauth/files routes, and
#   app/ingestion/google_oauth.py for the token exchange/refresh/revoke
#   calls). Scoped to drive.file, Google's narrowest Drive scope: this
#   connector can only ever read the specific files a user picked via the
#   Google Picker at connect time, nothing else in their Drive, and
#   drive.file needs no Google app-verification/security-audit process,
#   unlike the broader drive.readonly scope a folder-level grant would
#   require. The tradeoff is real and disclosed in the frontend copy: a
#   file added to the "same" folder later isn't automatically picked up;
#   the user has to reconnect and pick again.
import asyncio
import json
import re
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from app.config import settings
from app.ingestion.connector_base import ConnectorFetchError, SourceConnector, hash_records
from app.ingestion.document_text_extraction import (
    BINARY_TEXT_PARSERS as _BINARY_PARSERS,
    DOCX_MIME as _DOCX_MIME,
    MAX_BINARY_BYTES as _MAX_BINARY_BYTES,
    PDF_MIME as _PDF_MIME,
    PLAIN_TEXT_MIME_TYPES as _SUPPORTED_FILE_MIME_TYPES,
)
from app.ingestion.file_source import SourceRecord

_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Keeps a single sync's cost and runtime bounded regardless of how large the
# shared folder is, same reasoning as web_source.py's own caps.
_MAX_FILES = 20
_MAX_TEXT_CHARS = 20_000

_GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
# Google's own native formats (Docs/Sheets/Slides) aren't stored as plain
# files; they have to be exported as one, via a separate Drive API call
# from a regular file download (see _fetch_file_as_record). This maps each
# native type to the export format that gets the most useful plain text out
# of it: Sheets as CSV (keeps row/column structure legible), Slides and Docs
# as plain text.
_GOOGLE_NATIVE_EXPORT_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
# Plain-text mime types and PDF/DOCX parsing (_SUPPORTED_FILE_MIME_TYPES,
# _BINARY_PARSERS, _MAX_BINARY_BYTES) come from
# app/ingestion/document_text_extraction.py, shared with the SharePoint
# connector rather than duplicated here.

# A real Drive file/folder id is a specific base64url-ish alphabet. This
# also doubles as a defensive check before the id is interpolated into a
# Drive API `q` search-query string below.
_DRIVE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# Drive's own mimeType field for a regular (non-Google-native) file isn't
# always reliable. Depending on how a file got into the folder (drag-drop
# from certain OS file managers, Drive for Desktop sync, some third-party
# upload tools), a genuine .csv/.txt/.md can come back tagged as something
# generic like application/octet-stream instead of text/csv, which used to
# make every file in an otherwise-valid folder look unsupported and fail the
# whole sync with "No supported files found" even though the files were
# exactly the supported kind. SharePoint's connector already has to work
# around the equivalent Graph API quirk for the same reason (see
# sharepoint_source.py's own _TEXT_EXTENSIONS): extension is checked as a
# fallback signal alongside mimeType here too, not instead of it.
_TEXT_EXTENSIONS = (".txt", ".md", ".csv")
_BINARY_MIME_BY_EXTENSION = {".pdf": _PDF_MIME, ".docx": _DOCX_MIME}


def _extract_folder_id(url_or_id: str) -> str:
    """Accepts either a bare Drive folder id or a full folder URL
    (https://drive.google.com/drive/folders/<id>?...) and returns just the id."""
    value = url_or_id.strip()
    if "drive.google.com" in value:
        match = re.search(r"/folders/([a-zA-Z0-9_-]+)", value)
        if match:
            value = match.group(1)
        else:
            qs_id = parse_qs(urlparse(value).query).get("id")
            if qs_id:
                value = qs_id[0]
    if not _DRIVE_ID_RE.match(value):
        raise ConnectorFetchError(f"'{url_or_id}' doesn't look like a valid Drive folder id or link.")
    return value


class GoogleDriveConnector(SourceConnector):
    def __init__(self, folder_url_or_id: str):
        self.folder_id = _extract_folder_id(folder_url_or_id)

    def _get_access_token(self) -> str:
        """Blocking (google-auth's own refresh() call is synchronous):
        always run this via asyncio.to_thread, never awaited directly."""
        raw = settings.google_drive_service_account_json
        if not raw:
            raise ConnectorFetchError(
                "Google Drive isn't configured on this server -- ask your operator to set "
                "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON."
            )
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConnectorFetchError(f"Google Drive service account credentials are misconfigured: {e}") from e
        try:
            credentials = service_account.Credentials.from_service_account_info(info, scopes=_DRIVE_SCOPES)
            credentials.refresh(GoogleAuthRequest())
        except ConnectorFetchError:
            raise
        except Exception as e:
            raise ConnectorFetchError(f"Could not authenticate to Google Drive: {e}") from e
        return credentials.token

    async def fetch(self) -> list[SourceRecord]:
        token = await asyncio.to_thread(self._get_access_token)
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            files = await self._list_files(client, headers)
            records = []
            for f in files:
                record = await _fetch_file_as_record(client, headers, f)
                if record is not None:
                    records.append(record)
        if not records:
            raise ConnectorFetchError(
                "No supported files found in that Drive folder -- only plain text, Markdown, "
                "CSV, PDF, and Word (.docx) files, and Google Docs/Sheets/Slides, are read today "
                "(and a scanned/image-only PDF has no extractable text)."
            )
        return records

    async def _list_files(self, client: httpx.AsyncClient, headers: dict) -> list[dict]:
        # Escapes a literal single-quote inside the id before it's embedded
        # in Drive's own query-string language. _DRIVE_ID_RE already
        # rejects one in practice, but this keeps the query construction
        # itself safe even if that check is ever loosened.
        folder_id_escaped = self.folder_id.replace("'", "\\'")
        params = {
            "q": f"'{folder_id_escaped}' in parents and trashed = false",
            "fields": "files(id,name,mimeType)",
            "pageSize": _MAX_FILES,
        }
        try:
            resp = await client.get("https://www.googleapis.com/drive/v3/files", headers=headers, params=params)
        except httpx.HTTPError as e:
            raise ConnectorFetchError(f"Could not reach Google Drive: {e}") from e
        if resp.status_code == 404:
            raise ConnectorFetchError(
                "That Drive folder wasn't found, or hasn't been shared with the service account yet."
            )
        if resp.status_code == 403:
            raise ConnectorFetchError(
                "Access denied -- share that Drive folder with the service account's email first."
            )
        if resp.status_code >= 400:
            raise ConnectorFetchError(f"Google Drive returned an error (HTTP {resp.status_code}).")
        return resp.json().get("files", [])

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        return f"Google Drive folder ({self.folder_id})"


async def _fetch_file_as_record(client: httpx.AsyncClient, headers: dict, f: dict) -> Optional[SourceRecord]:
    """Shared by both connector types below. Doesn't touch either one's
    auth, so it takes the access-token headers as a plain dict rather than
    being a method on either class."""
    mime = f.get("mimeType", "")
    file_id = f.get("id")
    name = f.get("name", file_id)
    if not file_id or mime == _GOOGLE_FOLDER_MIME:
        return None  # not recursing into subfolders in this MVP

    export_mime = _GOOGLE_NATIVE_EXPORT_MIME_TYPES.get(mime)
    is_plain_text = mime in _SUPPORTED_FILE_MIME_TYPES or name.lower().endswith(_TEXT_EXTENSIONS)
    parser = _BINARY_PARSERS.get(mime) or _BINARY_PARSERS.get(
        _BINARY_MIME_BY_EXTENSION.get(Path(name).suffix.lower(), "")
    )
    try:
        if export_mime:
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                headers=headers,
                params={"mimeType": export_mime},
            )
            resp.raise_for_status()
            text = resp.text.strip()
        elif is_plain_text:
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                headers=headers,
                params={"alt": "media"},
            )
            resp.raise_for_status()
            text = resp.text.strip()
        elif parser:
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                headers=headers,
                params={"alt": "media"},
            )
            resp.raise_for_status()
            if len(resp.content) > _MAX_BINARY_BYTES:
                return None
            # CPU-bound parsing, off the event loop so one large file
            # doesn't stall every other request this process is serving.
            text = (await asyncio.to_thread(parser, resp.content)).strip()
        else:
            return None  # unsupported type: skip rather than guess
    except httpx.HTTPError:
        # One unreadable file shouldn't fail the whole sync.
        return None
    except Exception:
        # A corrupt/encrypted/malformed file, or a parsing library error,
        # gets the same "skip this one file" treatment, not a whole-sync
        # failure.
        return None

    if not text:
        return None
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + "\n\n[truncated -- file content exceeded the ingest size cap]"

    return SourceRecord(
        name=f"gdrive-{file_id}",
        body=text,
        source_description=f"Google Drive ({name})",
    )


class GoogleDriveOAuthConnector(SourceConnector):
    """The one-click "Connect Google Drive" connector type
    ("google_drive_oauth"): reads exactly the files a user picked via the
    Google Picker at connect time (app/api/connectors.py's oauth/files
    route), authenticating as that user's own OAuth grant rather than an
    operator-configured service account. See this module's own top
    docstring for the drive.file scope tradeoff.

    file_ids identifies which files to read; tenant_id/connector_id are
    only used to look up and refresh this connector's own stored OAuth
    grant (app/graph/connectors.py); this class holds no credentials
    itself between calls, both instances are cheap to construct per-sync."""

    def __init__(self, file_ids: list[str], tenant_id: str, connector_id: str):
        self.file_ids = file_ids
        self.tenant_id = tenant_id
        self.connector_id = connector_id

    async def _get_access_token(self) -> str:
        from app.graph import connectors as connectors_store
        from app.graph.token_crypto import TokenEncryptionNotConfigured, decrypt_token
        from app.ingestion.google_oauth import refresh_access_token
        from cryptography.fernet import InvalidToken

        encrypted = connectors_store.get_oauth_refresh_token(self.tenant_id, self.connector_id)
        if not encrypted:
            raise ConnectorFetchError(
                "This Drive connection is missing its access grant -- reconnect it from the connectors list."
            )
        try:
            refresh_token = decrypt_token(encrypted)
        except TokenEncryptionNotConfigured as e:
            raise ConnectorFetchError(str(e)) from e
        except InvalidToken as e:
            raise ConnectorFetchError(
                "This Drive connection can't be decrypted (the server's encryption key changed) "
                "-- reconnect it from the connectors list."
            ) from e
        return await refresh_access_token(refresh_token)

    async def fetch(self) -> list[SourceRecord]:
        if not self.file_ids:
            raise ConnectorFetchError("No files were selected for this Drive connection.")
        token = await self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        records: list[SourceRecord] = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            for file_id in self.file_ids:
                meta = await self._get_file_metadata(client, headers, file_id)
                if meta is None:
                    continue  # deleted, or access to this one file was individually revoked
                record = await _fetch_file_as_record(client, headers, meta)
                if record is not None:
                    records.append(record)
        if not records:
            raise ConnectorFetchError(
                "None of the selected Drive files could be read -- they may have been deleted, or "
                "this connection's access to them was revoked. Reconnect and pick files again."
            )
        return records

    async def _get_file_metadata(self, client: httpx.AsyncClient, headers: dict, file_id: str) -> Optional[dict]:
        try:
            resp = await client.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}",
                headers=headers,
                params={"fields": "id,name,mimeType"},
            )
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400:
            return None  # one missing/inaccessible file shouldn't fail the whole sync
        return resp.json()

    def content_hash(self, records: list[SourceRecord]) -> str:
        return hash_records(records)

    def source_description(self) -> str:
        return f"Google Drive ({len(self.file_ids)} selected file(s))"
