# Tests the SourceConnector interface itself (app/ingestion/connector_base.py),
# not just the one connector type built on it -- so a future connector type
# (SharePoint, Google Drive, ...) has a contract to test against, per the
# CLAUDE.md v0.5 exit criteria: adding a type should only mean implementing
# this interface and registering it in app/api/connectors.py's dispatch
# table, with nothing else to change.
import asyncio

import pytest

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector
from app.ingestion.file_source import SourceRecord
from app.ingestion.web_source import WebConnector, WebFetchError, content_hash, fetch_web_record


def test_source_connector_is_abstract():
    # Can't instantiate the interface directly -- forces every real connector
    # to actually implement fetch()/content_hash()/source_description().
    with pytest.raises(TypeError):
        SourceConnector()


def test_incomplete_connector_cannot_be_instantiated():
    class HalfConnector(SourceConnector):
        async def fetch(self) -> list[SourceRecord]:
            return []

        # content_hash() and source_description() deliberately left unimplemented.

    with pytest.raises(TypeError):
        HalfConnector()


def test_web_fetch_error_is_a_connector_fetch_error():
    # app/api/connectors.py's sync route catches ConnectorFetchError generically
    # so it isn't tied to any one connector type's own exception -- a type-specific
    # error therefore has to actually be one.
    assert issubclass(WebFetchError, ConnectorFetchError)


class _FakeConnector(SourceConnector):
    """A minimal second implementation, standing in for a future connector
    type, used to prove the interface -- not just WebConnector -- is what
    app/api/connectors.py's dispatch table actually depends on."""

    def __init__(self, records: list[SourceRecord]):
        self._records = records

    async def fetch(self) -> list[SourceRecord]:
        return self._records

    def content_hash(self, records: list[SourceRecord]) -> str:
        return "|".join(r.body for r in records)

    def source_description(self) -> str:
        return "Fake source"


def test_fake_connector_satisfies_the_interface():
    records = [SourceRecord(name="a", body="hello", source_description="fake")]
    connector = _FakeConnector(records)

    fetched = asyncio.run(connector.fetch())
    assert fetched == records
    assert connector.content_hash(fetched) == "hello"
    assert connector.source_description() == "Fake source"


def test_web_connector_fetch_wraps_fetch_web_record(monkeypatch):
    record = SourceRecord(name="web-abc", body="page text", source_description="Web page (https://example.com)")

    async def fake_fetch(url: str) -> SourceRecord:
        assert url == "https://example.com"
        return record

    monkeypatch.setattr("app.ingestion.web_source.fetch_web_record", fake_fetch)

    connector = WebConnector("https://example.com")
    records = asyncio.run(connector.fetch())

    assert records == [record]
    assert connector.source_description() == "Web page (https://example.com)"


def test_web_connector_content_hash_matches_the_plain_function():
    # WebConnector.content_hash() is meant to be a thin wrapper -- same
    # fingerprint the earlier function-based dedup check already relied on,
    # not a second, divergent hashing scheme.
    record = SourceRecord(name="web-abc", body="same text", source_description="Web page")
    connector = WebConnector("https://example.com")

    assert connector.content_hash([record]) == content_hash(record)


def test_web_connector_content_hash_empty_list():
    connector = WebConnector("https://example.com")
    assert connector.content_hash([]) == ""


def test_fetch_web_record_rejects_non_text_content(monkeypatch):
    # No real network call -- monkeypatches httpx to return a non-text
    # content-type, confirming the connector layer surfaces a clear
    # ConnectorFetchError rather than trying to extract text from a PDF/image.
    import httpx

    class _FakeResponse:
        headers = {"content-type": "application/pdf"}
        content = b"%PDF-1.4"
        text = ""

        def raise_for_status(self):
            pass

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    with pytest.raises(WebFetchError):
        asyncio.run(fetch_web_record("https://example.com/file.pdf"))
