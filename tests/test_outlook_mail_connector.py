# OutlookMailConnector tests -- no real network, no real Azure AD app
# registration. The token endpoint and Graph REST calls (httpx.AsyncClient)
# are both monkeypatched, same spirit as test_sharepoint_connector.py (this
# connector type shares its auth code path).
import asyncio

import httpx
import pytest

from app.ingestion.connector_base import ConnectorFetchError
from app.ingestion.outlook_mail_source import OutlookMailConnector


def test_rejects_a_non_email_mailbox():
    with pytest.raises(ConnectorFetchError):
        OutlookMailConnector("not-an-email")


def test_source_description_includes_the_mailbox():
    connector = OutlookMailConnector("alerts@contoso.com")
    assert "alerts@contoso.com" in connector.source_description()


def test_fetch_fails_clearly_when_not_configured(monkeypatch):
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_tenant_id", "")
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_client_id", "")
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_client_secret", "")
    connector = OutlookMailConnector("alerts@contoso.com")
    with pytest.raises(ConnectorFetchError, match="isn't configured"):
        asyncio.run(connector.fetch())


def _patch_auth(monkeypatch):
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_tenant_id", "tenant-123")
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_client_id", "client-abc")
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_client_secret", "secret-xyz")


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


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

    connector = OutlookMailConnector("alerts@contoso.com")
    with pytest.raises(ConnectorFetchError, match="Access denied"):
        asyncio.run(connector.fetch())


def test_fetch_parses_plain_and_html_messages(monkeypatch):
    _patch_auth(monkeypatch)

    messages = [
        {
            "id": "msg1",
            "subject": "Contract renewal",
            "from": {"emailAddress": {"address": "sarah@contoso.com"}},
            "toRecipients": [{"emailAddress": {"address": "alerts@contoso.com"}}],
            "receivedDateTime": "2026-08-01T12:00:00Z",
            "body": {"contentType": "text", "content": "Please review the attached renewal."},
        },
        {
            "id": "msg2",
            "subject": "Weekly digest",
            "from": {"emailAddress": {"address": "digest@contoso.com"}},
            "toRecipients": [],
            "receivedDateTime": "2026-08-02T09:00:00Z",
            "body": {"contentType": "html", "content": "<p>Top stories this week.</p>"},
        },
    ]

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            return _FakeResponse(200, json_data={"access_token": "fake-token"})

        async def get(self, url, headers=None, params=None):
            assert url.endswith("/users/alerts@contoso.com/mailFolders/inbox/messages")
            return _FakeResponse(200, json_data={"value": messages})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    connector = OutlookMailConnector("alerts@contoso.com")
    records = asyncio.run(connector.fetch())

    assert len(records) == 2
    renewal = next(r for r in records if "renewal" in r.body)
    assert "Please review the attached renewal." in renewal.body
    assert "sarah@contoso.com" in renewal.body
    digest = next(r for r in records if "digest" in r.source_description.lower() or "Weekly digest" in r.body)
    assert "Top stories this week." in digest.body
    assert "<p>" not in digest.body


def test_fetch_raises_when_no_messages(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, data=None):
            return _FakeResponse(200, json_data={"access_token": "fake-token"})

        async def get(self, url, headers=None, params=None):
            return _FakeResponse(200, json_data={"value": []})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    connector = OutlookMailConnector("alerts@contoso.com")
    with pytest.raises(ConnectorFetchError, match="No readable messages"):
        asyncio.run(connector.fetch())
