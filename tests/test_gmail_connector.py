# GmailConnector tests: no real network, no real Google credentials, no
# real domain-wide delegation. Auth (service_account.Credentials.
# from_service_account_info/.with_subject/.refresh) and the Gmail REST calls
# (httpx.AsyncClient) are both monkeypatched, same spirit as
# test_google_drive_connector.py (this connector shares its credential JSON).
import asyncio
import base64
import json

import httpx
import pytest

from app.ingestion.connector_base import ConnectorFetchError
from app.ingestion.gmail_source import GmailConnector, _decode_body_data, _extract_body_text

_FAKE_SERVICE_ACCOUNT_JSON = json.dumps(
    {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "abc123",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        "client_email": "demo@test-project.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
)


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_rejects_a_non_email_mailbox():
    with pytest.raises(ConnectorFetchError):
        GmailConnector("not-an-email")


def test_source_description_includes_the_mailbox():
    connector = GmailConnector("alerts@example.com")
    assert "alerts@example.com" in connector.source_description()


def test_decode_body_data_handles_missing_padding():
    encoded = _b64("hello there")
    assert _decode_body_data(encoded) == "hello there"


def test_extract_body_text_prefers_plain_over_html():
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/html", "body": {"data": _b64("<p>hi</p>")}},
            {"mimeType": "text/plain", "body": {"data": _b64("hi")}},
        ],
    }
    assert _extract_body_text(payload) == "hi"


def test_extract_body_text_falls_back_to_html_stripped():
    payload = {"mimeType": "text/html", "body": {"data": _b64("<p>Only plaintext here.</p>")}}
    assert _extract_body_text(payload) == "Only plaintext here."


def test_fetch_fails_clearly_when_not_configured(monkeypatch):
    monkeypatch.setattr("app.ingestion.gmail_source.settings.google_drive_service_account_json", "")
    connector = GmailConnector("alerts@example.com")
    with pytest.raises(ConnectorFetchError, match="isn't configured"):
        asyncio.run(connector.fetch())


class _FakeCredentials:
    def __init__(self):
        self.token = None

    def with_subject(self, subject):
        return self

    def refresh(self, request):
        self.token = "fake-access-token"


def _patch_auth(monkeypatch):
    monkeypatch.setattr(
        "app.ingestion.gmail_source.settings.google_drive_service_account_json", _FAKE_SERVICE_ACCOUNT_JSON
    )
    monkeypatch.setattr(
        "app.ingestion.gmail_source.service_account.Credentials.from_service_account_info",
        lambda info, scopes: _FakeCredentials(),
    )


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


def test_fetch_reads_messages(monkeypatch):
    _patch_auth(monkeypatch)

    def _message_detail(message_id: str) -> dict:
        return {
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "Q3 numbers"},
                    {"name": "From", "value": "finance@example.com"},
                    {"name": "To", "value": "alerts@example.com"},
                    {"name": "Date", "value": "Sat, 1 Aug 2026 12:00:00 +0000"},
                ],
                "mimeType": "text/plain",
                "body": {"data": _b64("Q3 revenue is up 12%.")},
            }
        }

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            if url.endswith("/messages"):
                return _FakeResponse(200, json_data={"messages": [{"id": "m1"}]})
            if url.endswith("/messages/m1"):
                return _FakeResponse(200, json_data=_message_detail("m1"))
            raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    connector = GmailConnector("alerts@example.com")
    records = asyncio.run(connector.fetch())

    assert len(records) == 1
    assert "Q3 revenue is up 12%." in records[0].body
    assert "finance@example.com" in records[0].body
    assert records[0].name == "gmail-m1"


def test_fetch_raises_clear_error_on_403(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            return _FakeResponse(403)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    connector = GmailConnector("alerts@example.com")
    with pytest.raises(ConnectorFetchError, match="Access denied"):
        asyncio.run(connector.fetch())


def test_fetch_raises_when_no_messages(monkeypatch):
    _patch_auth(monkeypatch)

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None, params=None):
            return _FakeResponse(200, json_data={"messages": []})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    connector = GmailConnector("alerts@example.com")
    with pytest.raises(ConnectorFetchError, match="No readable messages"):
        asyncio.run(connector.fetch())
