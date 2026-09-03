# app/api/connectors.py's microsoft_oauth_start/microsoft_oauth_finish
# routes: validation and the happy-path wiring. No real Microsoft
# tenant: ms_exchange_code is monkeypatched; state encode/decode uses real
# Fernet (fast, no network). Needs a real, reachable Neo4j for the
# connector-creation step, same pattern as test_foundry_iq_connector_api.py.
import asyncio
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.api import connectors as connectors_api
from app.config import KnowledgeBase, TenantConfig
from app.graph import connectors
from app.graph.graph_repository import GraphRepository
from app.ingestion import microsoft_oauth

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


def _configure(monkeypatch):
    monkeypatch.setattr(microsoft_oauth.settings, "microsoft_oauth_tenant_id", "tenant-123")
    monkeypatch.setattr(microsoft_oauth.settings, "microsoft_oauth_client_id", "client-abc")
    monkeypatch.setattr(microsoft_oauth.settings, "microsoft_oauth_client_secret", "secret-xyz")
    monkeypatch.setattr(microsoft_oauth.settings, "public_base_url", "https://saxon.example.com")
    monkeypatch.setattr(microsoft_oauth.settings, "token_encryption_key", _TEST_FERNET_KEY)
    monkeypatch.setattr(connectors_api.settings, "fabric_iq_ontology_scope", "McpServers.FabricIQOntology.All")
    monkeypatch.setattr(connectors_api.settings, "work_iq_scope", "WorkIQ.All")


def test_start_rejects_an_unknown_provider(monkeypatch):
    _configure(monkeypatch)
    tenant = _tenant("g1")
    req = connectors_api.MicrosoftOAuthStartRequest(provider="not_a_real_provider", name="X", group_id="g1")
    with pytest.raises(HTTPException) as exc_info:
        connectors_api.microsoft_oauth_start(req, tenant)
    assert exc_info.value.status_code == 400


def test_start_rejects_an_unknown_group_id(monkeypatch):
    _configure(monkeypatch)
    tenant = _tenant("g1")
    req = connectors_api.MicrosoftOAuthStartRequest(provider="work_iq", name="X", group_id="not-mine")
    with pytest.raises(HTTPException) as exc_info:
        connectors_api.microsoft_oauth_start(req, tenant)
    assert exc_info.value.status_code == 400


def test_start_requires_workspace_and_ontology_id_for_fabric_iq_ontology(monkeypatch):
    _configure(monkeypatch)
    tenant = _tenant("g1")
    req = connectors_api.MicrosoftOAuthStartRequest(provider="fabric_iq_ontology", name="X", group_id="g1")
    with pytest.raises(HTTPException) as exc_info:
        connectors_api.microsoft_oauth_start(req, tenant)
    assert exc_info.value.status_code == 400
    assert "workspace" in exc_info.value.detail.lower()


def test_start_fails_clearly_when_the_providers_scope_is_unconfigured(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(connectors_api.settings, "work_iq_scope", "")
    tenant = _tenant("g1")
    req = connectors_api.MicrosoftOAuthStartRequest(provider="work_iq", name="X", group_id="g1")
    with pytest.raises(HTTPException) as exc_info:
        connectors_api.microsoft_oauth_start(req, tenant)
    assert exc_info.value.status_code == 400


def test_start_returns_a_real_authorize_url_carrying_an_encoded_state(monkeypatch):
    _configure(monkeypatch)
    tenant = _tenant("g1")
    req = connectors_api.MicrosoftOAuthStartRequest(provider="work_iq", name="My Work IQ", group_id="g1")

    result = connectors_api.microsoft_oauth_start(req, tenant)

    assert result["authorize_url"].startswith("https://login.microsoftonline.com/tenant-123/")
    # The state param round-trips through decode_state back to what start() encoded.
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(result["authorize_url"]).query)["state"][0]
    decoded = microsoft_oauth.decode_state(state)
    assert decoded["provider"] == "work_iq"
    assert decoded["tenant_id"] == tenant.tenant_id
    assert decoded["group_id"] == "g1"


def test_finish_rejects_state_minted_for_a_different_tenant(monkeypatch):
    _configure(monkeypatch)
    state = microsoft_oauth.encode_state({
        "tenant_id": "someone-elses-tenant", "provider": "work_iq", "name": "X", "group_id": "g1",
        "workspace_id": None, "ontology_id": None,
    })
    req = connectors_api.MicrosoftOAuthFinishRequest(code="auth-code", state=state)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(connectors_api.microsoft_oauth_finish(req, _FakeRequest(), _tenant("g1")))
    assert exc_info.value.status_code == 400
    assert "isn't yours" in exc_info.value.detail


def test_finish_creates_a_work_iq_connector_on_a_valid_code(monkeypatch, repo):
    _configure(monkeypatch)
    tenant = _tenant("g1")
    state = microsoft_oauth.encode_state({
        "tenant_id": tenant.tenant_id, "provider": "work_iq", "name": "My Work IQ", "group_id": "g1",
        "workspace_id": None, "ontology_id": None,
    })
    req = connectors_api.MicrosoftOAuthFinishRequest(code="auth-code", state=state)

    async def fake_exchange(code, scope):
        assert code == "auth-code"
        assert scope == "WorkIQ.All"
        return {"access_token": "at", "refresh_token": "rt-123"}

    monkeypatch.setattr(connectors_api, "ms_exchange_code", fake_exchange)

    serialized = asyncio.run(connectors_api.microsoft_oauth_finish(req, _FakeRequest(), tenant))

    assert serialized["type"] == "work_iq"
    assert serialized["name"] == "My Work IQ"
    stored = connectors.get_microsoft_iq_credential(tenant.tenant_id, serialized["id"], "work_iq", repo=repo)
    from app.graph.token_crypto import decrypt_token
    assert decrypt_token(stored["oauth_refresh_token_enc"]) == "rt-123"


def test_finish_creates_a_fabric_iq_ontology_connector_with_its_extra_fields(monkeypatch, repo):
    _configure(monkeypatch)
    tenant = _tenant("g1")
    state = microsoft_oauth.encode_state({
        "tenant_id": tenant.tenant_id, "provider": "fabric_iq_ontology", "name": "My Ontology", "group_id": "g1",
        "workspace_id": "ws-9", "ontology_id": "ont-9",
    })
    req = connectors_api.MicrosoftOAuthFinishRequest(code="auth-code", state=state)

    async def fake_exchange(code, scope):
        return {"access_token": "at", "refresh_token": "rt-456"}

    monkeypatch.setattr(connectors_api, "ms_exchange_code", fake_exchange)

    serialized = asyncio.run(connectors_api.microsoft_oauth_finish(req, _FakeRequest(), tenant))

    assert serialized["type"] == "fabric_iq_ontology"
    stored = connectors.get_microsoft_iq_credential(tenant.tenant_id, serialized["id"], "fabric_iq_ontology", repo=repo)
    assert stored["fabric_iq_workspace_id"] == "ws-9"
    assert stored["fabric_iq_ontology_id"] == "ont-9"
