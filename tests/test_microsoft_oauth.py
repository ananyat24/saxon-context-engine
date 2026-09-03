# app/ingestion/microsoft_oauth.py -- the Fabric IQ Ontology / Work IQ
# delegated OAuth plumbing. No real Microsoft tenant: httpx is
# monkeypatched, same spirit as test_sharepoint_connector.py; encode_state/
# decode_state use real Fernet encryption (fast, no network) against a
# monkeypatched TOKEN_ENCRYPTION_KEY.
import asyncio
import time

import httpx
import pytest
from cryptography.fernet import Fernet

from app.ingestion import microsoft_oauth
from app.ingestion.connector_base import ConnectorFetchError

_TEST_KEY = Fernet.generate_key().decode()


def _configure(monkeypatch):
    monkeypatch.setattr(microsoft_oauth.settings, "microsoft_oauth_tenant_id", "tenant-123")
    monkeypatch.setattr(microsoft_oauth.settings, "microsoft_oauth_client_id", "client-abc")
    monkeypatch.setattr(microsoft_oauth.settings, "microsoft_oauth_client_secret", "secret-xyz")
    monkeypatch.setattr(microsoft_oauth.settings, "public_base_url", "https://saxon.example.com")
    monkeypatch.setattr(microsoft_oauth.settings, "token_encryption_key", _TEST_KEY)


def test_redirect_uri_is_built_from_public_base_url(monkeypatch):
    _configure(monkeypatch)
    assert microsoft_oauth.redirect_uri() == "https://saxon.example.com/static/microsoft-oauth-callback.html"


def test_redirect_uri_fails_clearly_without_public_base_url(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(microsoft_oauth.settings, "public_base_url", "")
    with pytest.raises(ConnectorFetchError, match="PUBLIC_BASE_URL"):
        microsoft_oauth.redirect_uri()


def test_build_authorize_url_includes_tenant_client_scope_and_state(monkeypatch):
    _configure(monkeypatch)
    url = microsoft_oauth.build_authorize_url("McpServers.FabricIQOntology.All", "opaque-state-123")
    assert url.startswith("https://login.microsoftonline.com/tenant-123/oauth2/v2.0/authorize?")
    assert "client_id=client-abc" in url
    assert "state=opaque-state-123" in url
    assert "McpServers.FabricIQOntology.All" in url
    assert "offline_access" in url


def test_encode_decode_state_round_trips(monkeypatch):
    _configure(monkeypatch)
    payload = {"tenant_id": "t1", "provider": "work_iq", "name": "My Work IQ", "group_id": "g1"}
    state = microsoft_oauth.encode_state(payload)
    assert microsoft_oauth.decode_state(state) == payload


def test_decode_state_rejects_a_tampered_value(monkeypatch):
    _configure(monkeypatch)
    state = microsoft_oauth.encode_state({"a": 1})
    with pytest.raises(ConnectorFetchError, match="expired or is invalid"):
        microsoft_oauth.decode_state(state[:-2] + "xx")


def test_decode_state_rejects_a_value_encrypted_under_a_different_key(monkeypatch):
    _configure(monkeypatch)
    state = microsoft_oauth.encode_state({"a": 1})
    monkeypatch.setattr(microsoft_oauth.settings, "token_encryption_key", Fernet.generate_key().decode())
    with pytest.raises(ConnectorFetchError, match="expired or is invalid"):
        microsoft_oauth.decode_state(state)


def test_decode_state_rejects_an_expired_value(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(microsoft_oauth, "_STATE_TTL_SECONDS", 0)
    state = microsoft_oauth.encode_state({"a": 1})
    time.sleep(1.1)
    with pytest.raises(ConnectorFetchError, match="expired or is invalid"):
        microsoft_oauth.decode_state(state)


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data or {}

    def json(self):
        return self._json_data


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None):
        return self._response


def test_exchange_code_returns_the_token_body(monkeypatch):
    _configure(monkeypatch)
    response = _FakeResponse(200, {"access_token": "at", "refresh_token": "rt"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(response))

    result = asyncio.run(microsoft_oauth.exchange_code("auth-code", "some.scope"))
    assert result == {"access_token": "at", "refresh_token": "rt"}


def test_exchange_code_fails_clearly_when_no_refresh_token_comes_back(monkeypatch):
    _configure(monkeypatch)
    response = _FakeResponse(200, {"access_token": "at"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(response))

    with pytest.raises(ConnectorFetchError, match="refresh token"):
        asyncio.run(microsoft_oauth.exchange_code("auth-code", "some.scope"))


def test_exchange_code_fails_clearly_on_a_rejected_code(monkeypatch):
    _configure(monkeypatch)
    response = _FakeResponse(400, {"error": "invalid_grant"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(response))

    with pytest.raises(ConnectorFetchError, match="rejected"):
        asyncio.run(microsoft_oauth.exchange_code("auth-code", "some.scope"))


def test_refresh_access_token_returns_the_new_access_token(monkeypatch):
    _configure(monkeypatch)
    response = _FakeResponse(200, {"access_token": "fresh-token"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(response))

    token = asyncio.run(microsoft_oauth.refresh_access_token("stored-refresh-token", "some.scope"))
    assert token == "fresh-token"


def test_refresh_access_token_fails_clearly_when_revoked(monkeypatch):
    _configure(monkeypatch)
    response = _FakeResponse(400, {"error": "invalid_grant"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(response))

    with pytest.raises(ConnectorFetchError, match="revoked or has expired"):
        asyncio.run(microsoft_oauth.refresh_access_token("stored-refresh-token", "some.scope"))


def test_functions_fail_clearly_when_not_configured(monkeypatch):
    monkeypatch.setattr(microsoft_oauth.settings, "microsoft_oauth_tenant_id", "")
    monkeypatch.setattr(microsoft_oauth.settings, "microsoft_oauth_client_id", "")
    monkeypatch.setattr(microsoft_oauth.settings, "microsoft_oauth_client_secret", "")
    with pytest.raises(microsoft_oauth.MicrosoftOAuthNotConfigured):
        microsoft_oauth.build_authorize_url("scope", "state")
