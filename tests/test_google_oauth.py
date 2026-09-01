# app/ingestion/google_oauth.py -- the three plain-HTTP calls the
# "google_drive_oauth" connector type makes against Google's OAuth endpoints.
# No real network or Google credentials -- httpx.AsyncClient is monkeypatched,
# same spirit as test_google_drive_connector.py's fakes.
import asyncio

import httpx
import pytest

from app.config import settings
from app.ingestion.connector_base import ConnectorFetchError
from app.ingestion.google_oauth import GoogleOAuthNotConfigured, exchange_code, refresh_access_token, revoke_token


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "test-client-secret")


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


class _FakeClient:
    def __init__(self, response=None, error=None, capture=None):
        self._response = response
        self._error = error
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, params=None):
        if self._capture is not None:
            self._capture["url"] = url
            self._capture["data"] = data
            self._capture["params"] = params
        if self._error:
            raise self._error
        return self._response


def test_exchange_code_requires_client_credentials(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_id", "")
    monkeypatch.setattr(settings, "google_oauth_client_secret", "")
    with pytest.raises(GoogleOAuthNotConfigured):
        asyncio.run(exchange_code("some-code"))


def test_exchange_code_sends_the_postmessage_redirect_uri_and_returns_tokens(monkeypatch):
    _configure(monkeypatch)
    capture = {}
    fake_response = _FakeResponse(200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(response=fake_response, capture=capture))

    body = asyncio.run(exchange_code("auth-code-123"))

    assert body["access_token"] == "at-1"
    assert body["refresh_token"] == "rt-1"
    assert capture["data"]["code"] == "auth-code-123"
    assert capture["data"]["redirect_uri"] == "postmessage"
    assert capture["data"]["grant_type"] == "authorization_code"
    assert capture["data"]["client_id"] == "test-client-id"
    assert capture["data"]["client_secret"] == "test-client-secret"


def test_exchange_code_raises_clearly_when_google_rejects_it(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(response=_FakeResponse(400)))
    with pytest.raises(ConnectorFetchError, match="rejected"):
        asyncio.run(exchange_code("expired-code"))


def test_exchange_code_raises_when_no_refresh_token_comes_back(monkeypatch):
    # Real, disclosed edge case (see the module's own comment): Google skips
    # re-issuing a refresh token if this same grant was already consented to.
    _configure(monkeypatch)
    fake_response = _FakeResponse(200, {"access_token": "at-1", "expires_in": 3600})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(response=fake_response))
    with pytest.raises(ConnectorFetchError, match="refresh token"):
        asyncio.run(exchange_code("auth-code-123"))


def test_refresh_access_token_sends_the_refresh_grant(monkeypatch):
    _configure(monkeypatch)
    capture = {}
    fake_response = _FakeResponse(200, {"access_token": "fresh-at"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(response=fake_response, capture=capture))

    token = asyncio.run(refresh_access_token("stored-refresh-token"))

    assert token == "fresh-at"
    assert capture["data"]["refresh_token"] == "stored-refresh-token"
    assert capture["data"]["grant_type"] == "refresh_token"


def test_refresh_access_token_raises_clearly_when_revoked(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(response=_FakeResponse(400)))
    with pytest.raises(ConnectorFetchError, match="revoked or has expired"):
        asyncio.run(refresh_access_token("stale-refresh-token"))


def test_revoke_token_never_raises_even_on_network_failure(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: _FakeClient(error=httpx.ConnectError("down"))
    )
    asyncio.run(revoke_token("some-token"))  # doesn't raise
