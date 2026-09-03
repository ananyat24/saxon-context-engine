# GoogleDriveOAuthConnector (the "google_drive_oauth" connector type) --
# reads a fixed list of previously-picked file ids, authenticating via a
# stored-and-refreshed OAuth token rather than a service account. No real
# network/Neo4j/Google credentials: everything below it is monkeypatched,
# same spirit as test_google_drive_connector.py's fakes for the sibling
# service-account connector.
import asyncio

import httpx
import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.graph import connectors as connectors_store
from app.graph.token_crypto import encrypt_token
from app.ingestion.connector_base import ConnectorFetchError
from app.ingestion.google_drive_source import GoogleDriveOAuthConnector


def test_fetch_fails_clearly_with_no_files_selected():
    connector = GoogleDriveOAuthConnector([], tenant_id="t1", connector_id="c1")
    with pytest.raises(ConnectorFetchError, match="No files were selected"):
        asyncio.run(connector.fetch())


def test_fetch_fails_clearly_when_no_stored_grant_is_found(monkeypatch):
    monkeypatch.setattr(connectors_store, "get_oauth_refresh_token", lambda tenant_id, connector_id, repo=None: None)
    connector = GoogleDriveOAuthConnector(["file1"], tenant_id="t1", connector_id="c1")
    with pytest.raises(ConnectorFetchError, match="reconnect it"):
        asyncio.run(connector.fetch())


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


def _patch_stored_grant(monkeypatch):
    monkeypatch.setattr(settings, "token_encryption_key", Fernet.generate_key().decode())
    encrypted = encrypt_token("a-refresh-token")
    monkeypatch.setattr(
        connectors_store, "get_oauth_refresh_token", lambda tenant_id, connector_id, repo=None: encrypted
    )

    async def fake_refresh(refresh_token):
        assert refresh_token == "a-refresh-token"
        return "fresh-access-token"

    monkeypatch.setattr("app.ingestion.google_oauth.refresh_access_token", fake_refresh)


def test_fetch_reads_exactly_the_selected_files(monkeypatch):
    _patch_stored_grant(monkeypatch)

    metadata_by_id = {
        "file1": {"id": "file1", "name": "notes.txt", "mimeType": "text/plain"},
        "file2": {"id": "file2", "name": "deleted-or-revoked", "mimeType": ""},
    }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            if url.endswith("/file1") and "fields" in params:
                return _FakeResponse(200, metadata_by_id["file1"])
            if url.endswith("/file2") and "fields" in params:
                return _FakeResponse(404)  # revoked/deleted: skipped, not fatal
            if url.endswith("/file1") and params.get("alt") == "media":
                return _FakeResponse(200, text="hello from notes.txt")
            raise AssertionError(f"unexpected request: {url} {params}")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())

    connector = GoogleDriveOAuthConnector(["file1", "file2"], tenant_id="t1", connector_id="c1")
    records = asyncio.run(connector.fetch())

    assert len(records) == 1
    assert records[0].body == "hello from notes.txt"
    assert "notes.txt" in records[0].source_description


def test_fetch_raises_clearly_when_every_selected_file_is_unreadable(monkeypatch):
    _patch_stored_grant(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, params=None):
            return _FakeResponse(404)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient())

    connector = GoogleDriveOAuthConnector(["file1"], tenant_id="t1", connector_id="c1")
    with pytest.raises(ConnectorFetchError, match="None of the selected"):
        asyncio.run(connector.fetch())


def test_source_description_reports_the_file_count():
    connector = GoogleDriveOAuthConnector(["a", "b", "c"], tenant_id="t1", connector_id="c1")
    assert "3 selected file" in connector.source_description()
