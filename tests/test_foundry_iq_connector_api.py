# app/api/connectors.py's foundry_iq-specific route logic --
# _create_foundry_iq_connector's validation/storage and
# _check_foundry_iq_connectivity's "Sync now" behavior. Needs a real,
# reachable Neo4j (GraphRepository(neo4j_client=None) opens a short-lived
# client per call -- see GraphRepository's own docstring), same pattern as
# test_purge_connector_data.py; FoundryIQRetriever itself is monkeypatched
# (its own request/response behavior is covered by test_foundry_iq_retriever.py).
import asyncio
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.api import connectors as connectors_api
from app.config import KnowledgeBase, TenantConfig
from app.graph import connectors
from app.graph.graph_repository import GraphRepository
from app.graph.token_crypto import decrypt_token

_TEST_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture
def repo():
    return GraphRepository()


class _FakeAppState:
    neo4j_client = None


class _FakeApp:
    state = _FakeAppState()


class _FakeRequest:
    app = _FakeApp()


def _tenant(group_id: str) -> TenantConfig:
    return TenantConfig(tenant_id=f"t_{uuid.uuid4().hex[:8]}", gemini_api_key="k", knowledge_bases=[KnowledgeBase(id=group_id, label="KB")])


def _req(**overrides):
    base = dict(
        name="Contoso Foundry IQ", type="foundry_iq", group_id="g1",
        url="https://contoso.search.windows.net", foundry_iq_api_key="raw-key-123",
        foundry_iq_knowledge_base="contoso-kb",
    )
    base.update(overrides)
    return connectors_api.CreateConnectorRequest(**base)


def test_create_foundry_iq_connector_stores_an_encrypted_key(monkeypatch):
    monkeypatch.setattr("app.graph.token_crypto.settings.token_encryption_key", _TEST_FERNET_KEY)
    tenant = _tenant("g1")
    req = _req(group_id="g1")

    serialized = connectors_api._create_foundry_iq_connector(req, _FakeRequest(), tenant)

    assert serialized["type"] == "foundry_iq"
    assert "foundry_iq_api_key" not in serialized
    credential = connectors.get_foundry_iq_credential(tenant.tenant_id, serialized["id"], repo=GraphRepository())
    assert decrypt_token(credential["api_key_enc"]) == "raw-key-123"


@pytest.mark.parametrize("missing_field", ["url", "foundry_iq_knowledge_base", "foundry_iq_api_key"])
def test_create_foundry_iq_connector_rejects_a_missing_required_field(monkeypatch, missing_field):
    monkeypatch.setattr("app.graph.token_crypto.settings.token_encryption_key", _TEST_FERNET_KEY)
    tenant = _tenant("g1")
    req = _req(group_id="g1", **{missing_field: None})

    with pytest.raises(HTTPException) as exc_info:
        connectors_api._create_foundry_iq_connector(req, _FakeRequest(), tenant)
    assert exc_info.value.status_code == 400


def test_create_foundry_iq_connector_fails_clearly_without_token_encryption_configured(monkeypatch):
    monkeypatch.setattr("app.graph.token_crypto.settings.token_encryption_key", "")
    tenant = _tenant("g1")
    req = _req(group_id="g1")

    with pytest.raises(HTTPException) as exc_info:
        connectors_api._create_foundry_iq_connector(req, _FakeRequest(), tenant)
    assert exc_info.value.status_code == 400
    assert "encrypt" in exc_info.value.detail.lower() or "token" in exc_info.value.detail.lower()


class _FakeRetriever:
    def __init__(self, facts, **kwargs):
        self._facts = facts
        self.kwargs = kwargs

    async def retrieve(self, query, num_results=1):
        return self._facts


def test_check_foundry_iq_connectivity_records_synced_on_a_real_result(monkeypatch, repo):
    monkeypatch.setattr("app.graph.token_crypto.settings.token_encryption_key", _TEST_FERNET_KEY)
    tenant = _tenant("g1")
    created = connectors_api._create_foundry_iq_connector(_req(group_id="g1"), _FakeRequest(), tenant)

    monkeypatch.setattr(connectors_api, "FoundryIQRetriever", lambda **kw: _FakeRetriever([{"fact": "x"}], **kw))
    asyncio.run(connectors_api._check_foundry_iq_connectivity(tenant, created, repo=repo))

    updated = connectors.get_connector(tenant.tenant_id, created["id"], repo=repo)
    assert updated["status"] == "synced"
    assert updated["last_error"] is None


def test_check_foundry_iq_connectivity_records_error_when_nothing_comes_back(monkeypatch, repo):
    monkeypatch.setattr("app.graph.token_crypto.settings.token_encryption_key", _TEST_FERNET_KEY)
    tenant = _tenant("g1")
    created = connectors_api._create_foundry_iq_connector(_req(group_id="g1"), _FakeRequest(), tenant)

    monkeypatch.setattr(connectors_api, "FoundryIQRetriever", lambda **kw: _FakeRetriever([], **kw))
    asyncio.run(connectors_api._check_foundry_iq_connectivity(tenant, created, repo=repo))

    updated = connectors.get_connector(tenant.tenant_id, created["id"], repo=repo)
    assert updated["status"] == "error"
    assert updated["last_error"]


def test_check_foundry_iq_connectivity_handles_a_missing_credential(repo):
    tenant = _tenant("g1")
    fake_connector = {"id": str(uuid.uuid4()), "group_id": "g1"}  # never actually created

    asyncio.run(connectors_api._check_foundry_iq_connectivity(tenant, fake_connector, repo=repo))

    updated = connectors.get_connector(tenant.tenant_id, fake_connector["id"], repo=repo)
    assert updated is None  # nothing to update -- the connector never existed; just must not raise
