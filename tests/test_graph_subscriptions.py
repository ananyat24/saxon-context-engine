# app/ingestion/graph_subscriptions.py -- no real network, no real Azure AD
# app registration. The token endpoint and Graph REST calls (httpx.AsyncClient)
# are both monkeypatched, same spirit as test_sharepoint_connector.py /
# test_outlook_mail_connector.py.
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.ingestion.connector_base import ConnectorFetchError
from app.ingestion.graph_subscriptions import (
    create_mail_subscription,
    delete_subscription,
    new_client_state,
    renew_subscription,
)


def _patch_auth(monkeypatch):
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_tenant_id", "tenant-123")
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_client_id", "client-abc")
    monkeypatch.setattr("app.ingestion.graph_auth.settings.sharepoint_client_secret", "secret-xyz")


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        return self._json_data


def test_new_client_state_is_random_and_nonempty():
    a, b = new_client_state(), new_client_state()
    assert a != b
    assert len(a) > 10


def test_create_mail_subscription_returns_id_and_expiry(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None, data=None):
            if "login.microsoftonline.com" in url:
                return _FakeResponse(200, json_data={"access_token": "fake-token"})
            assert url.endswith("/subscriptions")
            assert json["resource"] == "/users/alerts@contoso.com/mailFolders('Inbox')/messages"
            assert json["changeType"] == "created"
            assert json["clientState"] == "the-secret"
            return _FakeResponse(200, json_data={"id": "sub-123", "expirationDateTime": json["expirationDateTime"]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    subscription_id, expires_at = asyncio.run(
        create_mail_subscription("alerts@contoso.com", "https://example.com/api/v1/webhooks/graph", "the-secret")
    )
    assert subscription_id == "sub-123"
    assert expires_at > datetime.now(timezone.utc)


def test_create_mail_subscription_raises_clearly_on_rejection(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None, data=None):
            if "login.microsoftonline.com" in url:
                return _FakeResponse(200, json_data={"access_token": "fake-token"})
            return _FakeResponse(400, text="Invalid notificationUrl")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    with pytest.raises(ConnectorFetchError, match="refused"):
        asyncio.run(create_mail_subscription("alerts@contoso.com", "https://example.com/webhooks/graph", "secret"))


def test_renew_subscription_returns_new_expiry(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None, data=None):
            return _FakeResponse(200, json_data={"access_token": "fake-token"})

        async def patch(self, url, headers=None, json=None):
            assert url.endswith("/subscriptions/sub-123")
            return _FakeResponse(200, json_data={"id": "sub-123"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    new_expiry = asyncio.run(renew_subscription("sub-123"))
    assert new_expiry > datetime.now(timezone.utc) + timedelta(days=1)


def test_renew_subscription_raises_when_graph_rejects_it(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None, data=None):
            return _FakeResponse(200, json_data={"access_token": "fake-token"})

        async def patch(self, url, headers=None, json=None):
            return _FakeResponse(404)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    with pytest.raises(ConnectorFetchError):
        asyncio.run(renew_subscription("sub-gone"))


def test_delete_subscription_never_raises(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None, data=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    asyncio.run(delete_subscription("sub-123"))  # must not raise
