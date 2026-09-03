# SharePointConnector tests: no real network, no real Azure AD app
# registration. The token endpoint and Graph REST calls (httpx.AsyncClient)
# are both monkeypatched, same spirit as test_google_drive_connector.py.
import asyncio

import httpx
import pytest

from app.ingestion.connector_base import ConnectorFetchError
from app.ingestion.sharepoint_source import SharePointConnector, _parse_site_url


def test_parse_site_url_splits_hostname_and_path():
    hostname, path = _parse_site_url("https://contoso.sharepoint.com/sites/Marketing")
    assert hostname == "contoso.sharepoint.com"
    assert path == "sites/Marketing"


def test_parse_site_url_rejects_a_bare_hostname_with_no_site_path():
    with pytest.raises(ConnectorFetchError):
        _parse_site_url("https://contoso.sharepoint.com")


def test_parse_site_url_rejects_garbage():
    with pytest.raises(ConnectorFetchError):
        _parse_site_url("not a url at all")


def test_source_description_includes_the_site_url():
    connector = SharePointConnector("https://contoso.sharepoint.com/sites/Marketing")
    assert "contoso.sharepoint.com/sites/Marketing" in connector.source_description()


def test_content_hash_matches_shared_hash_records_helper():
    from app.ingestion.connector_base import hash_records
    from app.ingestion.file_source import SourceRecord

    records = [SourceRecord(name="a", body="x", source_description="d")]
    connector = SharePointConnector("https://contoso.sharepoint.com/sites/Marketing")
    assert connector.content_hash(records) == hash_records(records)


def test_fetch_fails_clearly_when_not_configured(monkeypatch):
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_tenant_id", "")
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_client_id", "")
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_client_secret", "")
    connector = SharePointConnector("https://contoso.sharepoint.com/sites/Marketing")
    with pytest.raises(ConnectorFetchError, match="isn't configured"):
        asyncio.run(connector.fetch())


def _patch_auth(monkeypatch):
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_tenant_id", "tenant-123")
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_client_id", "client-abc")
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_client_secret", "secret-xyz")


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", content=None):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self.content = content if content is not None else text.encode()

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


def test_fetch_fails_clearly_on_auth_failure(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            return _FakeResponse(401)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    connector = SharePointConnector("https://contoso.sharepoint.com/sites/Marketing")
    with pytest.raises(ConnectorFetchError, match="Could not authenticate"):
        asyncio.run(connector.fetch())


def test_fetch_raises_clear_error_when_site_not_found(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            return _FakeResponse(200, json_data={"access_token": "fake-token"})

        async def get(self, url, headers=None, params=None):
            if "/sites/" in url:
                return _FakeResponse(404)
            raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    connector = SharePointConnector("https://contoso.sharepoint.com/sites/Marketing")
    with pytest.raises(ConnectorFetchError, match="No SharePoint site found"):
        asyncio.run(connector.fetch())


def test_fetch_raises_clear_error_on_403(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            return _FakeResponse(200, json_data={"access_token": "fake-token"})

        async def get(self, url, headers=None, params=None):
            return _FakeResponse(403)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    connector = SharePointConnector("https://contoso.sharepoint.com/sites/Marketing")
    with pytest.raises(ConnectorFetchError, match="Access denied"):
        asyncio.run(connector.fetch())


def test_fetch_reads_supported_files_and_skips_the_rest(monkeypatch):
    _patch_auth(monkeypatch)

    files = [
        {"id": "txt1", "name": "readme.txt", "file": {"mimeType": "text/plain"}},
        {"id": "md1", "name": "notes.md", "file": {"mimeType": "application/octet-stream"}},  # extension fallback
        {"id": "img1", "name": "photo.png", "file": {"mimeType": "image/png"}},
        {"id": "sub1", "name": "Subfolder", "folder": {}},
    ]

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            return _FakeResponse(200, json_data={"access_token": "fake-token"})

        async def get(self, url, headers=None, params=None):
            if url.endswith(":/sites/Marketing"):
                return _FakeResponse(200, json_data={"id": "site-id-1"})
            if url.endswith("/sites/site-id-1/drive"):
                return _FakeResponse(200, json_data={"id": "drive-id-1"})
            if url.endswith("/drives/drive-id-1/root/children"):
                return _FakeResponse(200, json_data={"value": files})
            if url.endswith("/items/txt1/content"):
                return _FakeResponse(200, text="Plain text content.")
            if url.endswith("/items/md1/content"):
                return _FakeResponse(200, text="Markdown content.")
            raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    connector = SharePointConnector("https://contoso.sharepoint.com/sites/Marketing")
    records = asyncio.run(connector.fetch())

    assert len(records) == 2
    bodies = {r.body for r in records}
    assert "Plain text content." in bodies
    assert "Markdown content." in bodies
    names = {r.source_description for r in records}
    assert "SharePoint (readme.txt)" in names
    assert "SharePoint (notes.md)" in names


def test_fetch_reads_pdf_via_the_binary_parsers(monkeypatch):
    _patch_auth(monkeypatch)

    files = [{"id": "pdf1", "name": "report.pdf", "file": {"mimeType": "application/pdf"}}]

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            return _FakeResponse(200, json_data={"access_token": "fake-token"})

        async def get(self, url, headers=None, params=None):
            if url.endswith(":/sites/Marketing"):
                return _FakeResponse(200, json_data={"id": "site-id-1"})
            if url.endswith("/sites/site-id-1/drive"):
                return _FakeResponse(200, json_data={"id": "drive-id-1"})
            if url.endswith("/drives/drive-id-1/root/children"):
                return _FakeResponse(200, json_data={"value": files})
            if url.endswith("/items/pdf1/content"):
                return _FakeResponse(200, content=b"fake-pdf-bytes")
            raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())
    monkeypatch.setattr(
        "app.ingestion.sharepoint_source.BINARY_TEXT_PARSERS",
        {"application/pdf": lambda data: f"parsed:{data.decode()}"},
    )

    connector = SharePointConnector("https://contoso.sharepoint.com/sites/Marketing")
    records = asyncio.run(connector.fetch())

    assert len(records) == 1
    assert records[0].body == "parsed:fake-pdf-bytes"


def test_fetch_raises_when_no_supported_files(monkeypatch):
    _patch_auth(monkeypatch)

    files = [{"id": "img1", "name": "photo.png", "file": {"mimeType": "image/png"}}]

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            return _FakeResponse(200, json_data={"access_token": "fake-token"})

        async def get(self, url, headers=None, params=None):
            if url.endswith(":/sites/Marketing"):
                return _FakeResponse(200, json_data={"id": "site-id-1"})
            if url.endswith("/sites/site-id-1/drive"):
                return _FakeResponse(200, json_data={"id": "drive-id-1"})
            if url.endswith("/drives/drive-id-1/root/children"):
                return _FakeResponse(200, json_data={"value": files})
            raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    connector = SharePointConnector("https://contoso.sharepoint.com/sites/Marketing")
    with pytest.raises(ConnectorFetchError, match="No supported files"):
        asyncio.run(connector.fetch())
