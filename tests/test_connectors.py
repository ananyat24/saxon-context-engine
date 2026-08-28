# Tests the SourceConnector interface itself (app/ingestion/connector_base.py),
# not just the one connector type built on it -- so a future connector type
# (SharePoint, Google Drive, ...) has a contract to test against, per the
# CLAUDE.md v0.5 exit criteria: adding a type should only mean implementing
# this interface and registering it in app/api/connectors.py's dispatch
# table, with nothing else to change.
import asyncio

import pytest

from app.ingestion.connector_base import ConnectorFetchError, SourceConnector
from app.ingestion.database_source import DatabaseConnector
from app.ingestion.document_source import DocumentConnector
from app.ingestion.email_source import EmailConnector
from app.ingestion.file_source import SourceRecord
from app.ingestion.web_source import WebConnector, WebFetchError, content_hash, fetch_web_record, _assert_fetchable


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
    # socket.getaddrinfo is also monkeypatched so the SSRF guard's DNS lookup
    # doesn't depend on real network access in tests.
    import socket as socket_module

    import httpx

    monkeypatch.setattr(
        socket_module, "getaddrinfo", lambda *a, **kw: [(socket_module.AF_INET, None, None, "", ("93.184.216.34", 0))]
    )

    class _FakeResponse:
        headers = {"content-type": "application/pdf"}
        content = b"%PDF-1.4"
        text = ""
        is_redirect = False

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


# --- Demo connector types (database/documents/email) -------------------------
# These read bundled mock data under data/samples/mock_*/ rather than a live
# source (see each module's docstring for why) -- these tests confirm the
# bundled data actually round-trips into valid SourceRecords via the same
# interface WebConnector implements, using real repo-bundled files rather
# than mocks, since reading them is free (no network, no LLM call).


def test_database_connector_reads_mock_accounts():
    connector = DatabaseConnector()
    records = asyncio.run(connector.fetch())

    assert len(records) == 6
    riverton = next(r for r in records if "Riverton Robotics" in r.body)
    assert "ACC-1001" in riverton.body
    assert connector.content_hash(records) == connector.content_hash(records)


def test_database_connector_hash_changes_with_content():
    connector = DatabaseConnector()
    records = asyncio.run(connector.fetch())
    mutated = records[:-1]

    assert connector.content_hash(records) != connector.content_hash(mutated)


def test_document_connector_reads_mock_docs():
    connector = DocumentConnector()
    records = asyncio.run(connector.fetch())

    names = {r.name for r in records}
    assert "onboarding-guide" in names
    assert "security-policy" in names
    onboarding = next(r for r in records if r.name == "onboarding-guide")
    assert "Riverton Robotics" in onboarding.body


def test_email_connector_parses_headers_and_body():
    connector = EmailConnector()
    records = asyncio.run(connector.fetch())

    assert len(records) == 3
    escalation = next(r for r in records if "Fenwick" in r.body)
    assert "sarah.chen@saxon.ai" in escalation.body
    assert "Subject:" in escalation.body
    assert "missed two check-in calls" in escalation.body


def test_email_connector_source_description():
    assert "mock" in EmailConnector().source_description().lower()


def test_document_connector_reads_a_dropped_in_docx(tmp_path, monkeypatch):
    # Drive/SharePoint already ingest PDF/DOCX; this confirms the local mock
    # documents connector -- what "drop your own mock data in" actually
    # targets -- picked up the same capability rather than staying .txt-only.
    import app.ingestion.document_source as document_source
    from docx import Document

    docs_dir = tmp_path / "mock_docs"
    docs_dir.mkdir()
    (docs_dir / "onboarding-guide.txt").write_text("Riverton Robotics onboarding.", encoding="utf-8")
    doc = Document()
    doc.add_paragraph("Contoso vendor policy: net 30 payment terms.")
    doc.save(docs_dir / "vendor-policy.docx")
    monkeypatch.setattr(document_source, "_MOCK_DOCS_DIR", docs_dir)

    records = asyncio.run(DocumentConnector().fetch())

    names = {r.name for r in records}
    assert "onboarding-guide" in names
    assert "vendor-policy" in names
    policy = next(r for r in records if r.name == "vendor-policy")
    assert "net 30 payment terms" in policy.body


# --- SSRF guard on the web connector -----------------------------------------
# A tenant supplies the URL and this server fetches it, so an unvalidated
# fetch is a ready-made "make the server hit its own internal network"
# primitive. These confirm the guard actually blocks the addresses that
# matter, without needing a real network call.


def test_web_connector_rejects_non_http_scheme():
    with pytest.raises(WebFetchError):
        _assert_fetchable("file:///etc/passwd")


def test_web_connector_rejects_loopback_host():
    with pytest.raises(WebFetchError):
        _assert_fetchable("http://127.0.0.1/")


def test_web_connector_rejects_localhost_hostname():
    with pytest.raises(WebFetchError):
        _assert_fetchable("http://localhost:8000/admin")


def test_web_connector_rejects_cloud_metadata_address():
    # 169.254.169.254 is the instance-metadata endpoint on Azure/AWS/GCP --
    # link-local, so covered by the same check, but worth its own explicit
    # test since it's the actual attack this guard exists to stop.
    with pytest.raises(WebFetchError):
        _assert_fetchable("http://169.254.169.254/metadata/instance")


def test_web_connector_allows_a_public_address():
    # 93.184.216.34 is example.com's (public, IANA-reserved-for-documentation
    # in the DNS sense, but a real routable unicast address) -- just needs to
    # not be loopback/private/link-local/reserved/multicast.
    _assert_fetchable("https://93.184.216.34/")


def test_fetch_web_record_rejects_redirect_to_internal_address(monkeypatch):
    # A public URL that 302s to an internal address is the classic SSRF
    # bypass for a check that only validates the original URL -- this proves
    # each redirect hop is re-validated, not just the first one. socket.
    # getaddrinfo is monkeypatched so this doesn't depend on real DNS.
    import socket as socket_module

    import httpx

    def fake_getaddrinfo(host, *args, **kwargs):
        if host == "example.com":
            return [(socket_module.AF_INET, None, None, "", ("93.184.216.34", 0))]
        return [(socket_module.AF_INET, None, None, "", ("169.254.169.254", 0))]

    monkeypatch.setattr(socket_module, "getaddrinfo", fake_getaddrinfo)

    class _RedirectResponse:
        status_code = 302
        headers = {"location": "http://169.254.169.254/metadata"}
        is_redirect = True

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _RedirectResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient())

    with pytest.raises(WebFetchError):
        asyncio.run(fetch_web_record("https://example.com"))


# --- Database connector: dropping in extra/replacement mock CSVs ------------


def test_database_connector_picks_up_a_second_csv_with_no_known_spec(tmp_path, monkeypatch):
    # Simulates "drop your own mock data in" -- a CSV that isn't accounts.csv
    # and has no hand-picked FileSourceSpec should still ingest via the
    # inferred id/name columns, not be silently skipped.
    import app.ingestion.database_source as database_source

    mock_dir = tmp_path / "mock_crm"
    mock_dir.mkdir()
    (mock_dir / "accounts.csv").write_text(
        "AccountID,CompanyName\nACC-1,Acme\n", encoding="utf-8"
    )
    (mock_dir / "vendors.csv").write_text(
        "VendorId,VendorName,Region\nV-1,Contoso Supply,EMEA\n", encoding="utf-8"
    )
    monkeypatch.setattr(database_source, "_MOCK_CRM_DIR", mock_dir)

    connector = DatabaseConnector()
    records = asyncio.run(connector.fetch())

    assert any("Acme" in r.body for r in records)
    vendor_record = next(r for r in records if "Contoso Supply" in r.body)
    assert "V-1" in vendor_record.body


def test_database_connector_errors_clearly_when_folder_has_no_csvs(tmp_path, monkeypatch):
    import app.ingestion.database_source as database_source

    mock_dir = tmp_path / "mock_crm"
    mock_dir.mkdir()
    monkeypatch.setattr(database_source, "_MOCK_CRM_DIR", mock_dir)

    with pytest.raises(ConnectorFetchError):
        asyncio.run(DatabaseConnector().fetch())


# --- Database connector: per-connector uploaded CSVs (the "easily droppable
# CSV" fix) -- a connector reads from its OWN upload folder, populated only
# by POST /connectors/{id}/files, falling back to the shared demo dataset
# when nothing's been uploaded to it. ----------------------------------------


def test_database_connector_prefers_its_own_uploaded_csvs(tmp_path, monkeypatch):
    import app.ingestion.database_source as database_source

    monkeypatch.setattr(database_source, "UPLOADS_ROOT", tmp_path / "uploads")
    upload_dir = database_source.connector_upload_dir("conn-123")
    upload_dir.mkdir(parents=True)
    (upload_dir / "widgets.csv").write_text("WidgetID,WidgetName\nW-1,Left Bracket\n", encoding="utf-8")

    connector = DatabaseConnector("conn-123")
    records = asyncio.run(connector.fetch())

    assert len(records) == 1
    assert "Left Bracket" in records[0].body
    assert "Left Bracket" not in connector.source_description()
    assert "Uploaded" in connector.source_description()


def test_database_connector_falls_back_to_demo_data_with_no_uploads(tmp_path, monkeypatch):
    import app.ingestion.database_source as database_source

    monkeypatch.setattr(database_source, "UPLOADS_ROOT", tmp_path / "uploads")

    connector = DatabaseConnector("conn-with-nothing-uploaded")
    records = asyncio.run(connector.fetch())

    # Same bundled demo dataset as DatabaseConnector() with no id at all.
    assert len(records) == 6
    assert connector.source_description() == "Demo CRM accounts (mock structured data)"


def test_database_connector_with_no_id_always_uses_demo_data(tmp_path, monkeypatch):
    # The pre-creation validation call in app/api/connectors.py probes a
    # type's factory with {} before a connector (and its id) exists --
    # DatabaseConnector("") must never resolve to the shared uploads root.
    import app.ingestion.database_source as database_source

    uploads_root = tmp_path / "uploads"
    uploads_root.mkdir()
    (uploads_root / "stray.csv").write_text("Id,Name\n1,Should not be read\n", encoding="utf-8")
    monkeypatch.setattr(database_source, "UPLOADS_ROOT", uploads_root)

    connector = DatabaseConnector("")
    records = asyncio.run(connector.fetch())

    assert not any("Should not be read" in r.body for r in records)
    assert connector.source_description() == "Demo CRM accounts (mock structured data)"


# --- POST /connectors/{id}/files -- the HTTP side of "easily droppable CSV" -
# Needs a real, reachable Neo4j (same caveat as test_source_authority.py):
# creates and cleans up its own throwaway :Connector node. Calls the route
# function directly with a fake Request and Depends() resolved by hand (same
# pattern test_odata.py uses), rather than a full TestClient -- an UploadFile
# can be constructed directly (FastAPI supports this), so no ASGI/HTTP layer
# is needed to exercise the real filesystem-write + validation logic.
import io
import uuid as uuid_module

from fastapi import HTTPException, UploadFile

from app.api import connectors as connectors_api
from app.config import KnowledgeBase, TenantConfig
from app.graph import connectors as connectors_storage
from app.graph.graph_repository import GraphRepository


class _FakeUploadAppState:
    def __init__(self):
        self.neo4j_client = None


class _FakeUploadApp:
    def __init__(self):
        self.state = _FakeUploadAppState()


class _FakeUploadRequest:
    def __init__(self):
        self.app = _FakeUploadApp()


def _upload_tenant(tenant_id: str) -> TenantConfig:
    return TenantConfig(tenant_id=tenant_id, gemini_api_key="fake", knowledge_bases=[KnowledgeBase(id="kb1", label="KB")])


def _csv_upload(filename: str, content: bytes = b"Id,Name\n1,Test\n") -> UploadFile:
    return UploadFile(file=io.BytesIO(content), filename=filename)


@pytest.fixture
def upload_repo():
    return GraphRepository()


def test_upload_saves_a_csv_under_the_connectors_own_folder(tmp_path, monkeypatch, upload_repo):
    import app.ingestion.database_source as database_source

    monkeypatch.setattr(database_source, "UPLOADS_ROOT", tmp_path / "uploads")
    tenant_id = f"test_upload_tenant_{uuid_module.uuid4().hex[:8]}"
    connector = connectors_storage.create_connector(
        tenant_id, "My CSVs", "database", "kb1", "Uploaded CSV data", repo=upload_repo,
    )
    try:
        result = asyncio.run(
            connectors_api.upload_connector_file(
                connector["id"], _csv_upload("widgets.csv", b"WidgetID,WidgetName\nW-1,Bracket\n"),
                _FakeUploadRequest(), tenant=_upload_tenant(tenant_id),
            )
        )
        assert result == {"filename": "widgets.csv", "size": len(b"WidgetID,WidgetName\nW-1,Bracket\n")}
        saved = database_source.connector_upload_dir(connector["id"]) / "widgets.csv"
        assert saved.read_bytes() == b"WidgetID,WidgetName\nW-1,Bracket\n"
    finally:
        connectors_storage.delete_connector(tenant_id, connector["id"], repo=upload_repo)


def test_upload_strips_any_path_traversal_from_the_filename(tmp_path, monkeypatch, upload_repo):
    import app.ingestion.database_source as database_source

    monkeypatch.setattr(database_source, "UPLOADS_ROOT", tmp_path / "uploads")
    tenant_id = f"test_upload_traversal_{uuid_module.uuid4().hex[:8]}"
    connector = connectors_storage.create_connector(
        tenant_id, "My CSVs", "database", "kb1", "Uploaded CSV data", repo=upload_repo,
    )
    try:
        result = asyncio.run(
            connectors_api.upload_connector_file(
                connector["id"], _csv_upload("../../evil.csv"), _FakeUploadRequest(), tenant=_upload_tenant(tenant_id),
            )
        )
        # The traversal components are stripped -- it lands as a normal file
        # under this connector's own folder, never above it.
        assert result["filename"] == "evil.csv"
        assert (database_source.connector_upload_dir(connector["id"]) / "evil.csv").exists()
        assert not (tmp_path / "evil.csv").exists()
    finally:
        connectors_storage.delete_connector(tenant_id, connector["id"], repo=upload_repo)


def test_upload_rejects_a_non_csv_file(upload_repo):
    tenant_id = f"test_upload_wrongext_{uuid_module.uuid4().hex[:8]}"
    connector = connectors_storage.create_connector(
        tenant_id, "My CSVs", "database", "kb1", "Uploaded CSV data", repo=upload_repo,
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                connectors_api.upload_connector_file(
                    connector["id"], _csv_upload("data.txt"), _FakeUploadRequest(), tenant=_upload_tenant(tenant_id),
                )
            )
        assert exc_info.value.status_code == 400
    finally:
        connectors_storage.delete_connector(tenant_id, connector["id"], repo=upload_repo)


def test_upload_rejects_a_non_database_connector(upload_repo):
    tenant_id = f"test_upload_wrongtype_{uuid_module.uuid4().hex[:8]}"
    connector = connectors_storage.create_connector(
        tenant_id, "A web source", "web", "kb1", "https://example.com", repo=upload_repo,
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                connectors_api.upload_connector_file(
                    connector["id"], _csv_upload("data.csv"), _FakeUploadRequest(), tenant=_upload_tenant(tenant_id),
                )
            )
        assert exc_info.value.status_code == 400
    finally:
        connectors_storage.delete_connector(tenant_id, connector["id"], repo=upload_repo)


def test_upload_404s_for_a_connector_belonging_to_another_tenant(upload_repo):
    owner_tenant = f"test_upload_owner_{uuid_module.uuid4().hex[:8]}"
    connector = connectors_storage.create_connector(
        owner_tenant, "My CSVs", "database", "kb1", "Uploaded CSV data", repo=upload_repo,
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                connectors_api.upload_connector_file(
                    connector["id"], _csv_upload("data.csv"), _FakeUploadRequest(),
                    tenant=_upload_tenant(f"someone_else_{uuid_module.uuid4().hex[:8]}"),
                )
            )
        assert exc_info.value.status_code == 404
    finally:
        connectors_storage.delete_connector(owner_tenant, connector["id"], repo=upload_repo)


def test_upload_rejects_a_file_over_the_size_limit(monkeypatch, upload_repo):
    monkeypatch.setattr(connectors_api, "_MAX_UPLOAD_BYTES", 10)
    tenant_id = f"test_upload_toolarge_{uuid_module.uuid4().hex[:8]}"
    connector = connectors_storage.create_connector(
        tenant_id, "My CSVs", "database", "kb1", "Uploaded CSV data", repo=upload_repo,
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                connectors_api.upload_connector_file(
                    connector["id"], _csv_upload("data.csv", b"Id,Name\n1,Way too much data for the limit\n"),
                    _FakeUploadRequest(), tenant=_upload_tenant(tenant_id),
                )
            )
        assert exc_info.value.status_code == 413
    finally:
        connectors_storage.delete_connector(tenant_id, connector["id"], repo=upload_repo)
