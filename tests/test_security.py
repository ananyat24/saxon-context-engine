import pytest
from fastapi import HTTPException

from app.config import KnowledgeBase, TenantConfig, settings
from app.security import require_admin, require_tenant, resolve_knowledge_base


class _FakeAppState:
    neo4j_client = None


class _FakeApp:
    state = _FakeAppState()


class _FakeRequest:
    """Stands in for FastAPI's Request: require_tenant only ever reaches
    into request.app.state.neo4j_client, and only on the Neo4j-backed
    tenant fallback path (see app/graph/tenants.py), which these tests
    monkeypatch rather than hit a real database."""

    app = _FakeApp()


@pytest.fixture
def tenant():
    return TenantConfig(
        tenant_id="acme",
        gemini_api_key="fake-key",
        knowledge_bases=[
            KnowledgeBase(id="acme_demo", label="Demo"),
            KnowledgeBase(id="northwind", label="Northwind"),
        ],
    )


def test_resolve_knowledge_base_defaults_to_first(tenant):
    assert resolve_knowledge_base(tenant, None) == "acme_demo"


def test_resolve_knowledge_base_accepts_own_knowledge_base(tenant):
    assert resolve_knowledge_base(tenant, "northwind") == "northwind"


def test_resolve_knowledge_base_rejects_unknown_id(tenant):
    """The core isolation guarantee: a caller can only select among *this*
    tenant's own knowledge bases, never an arbitrary group_id."""
    with pytest.raises(HTTPException) as exc_info:
        resolve_knowledge_base(tenant, "someone_elses_group")
    assert exc_info.value.status_code == 400


def test_require_tenant_rejects_unknown_key(monkeypatch):
    monkeypatch.setattr(settings, "tenant_api_keys", {})
    monkeypatch.setattr("app.graph.tenants.find_tenant_by_api_key", lambda api_key, repo=None: None)
    with pytest.raises(HTTPException) as exc_info:
        require_tenant(_FakeRequest(), x_api_key="not-a-real-key")
    assert exc_info.value.status_code == 401


def test_require_tenant_accepts_known_key(monkeypatch, tenant):
    # Found in the static config: never reaches the Neo4j fallback at all.
    monkeypatch.setattr(settings, "tenant_api_keys", {"real-key": tenant})
    assert require_tenant(_FakeRequest(), x_api_key="real-key") is tenant


def test_require_tenant_falls_back_to_the_neo4j_backed_store(monkeypatch, tenant):
    # A tenant created through the admin API (app/api/admin.py) isn't in the
    # static config at all: require_tenant must still find it.
    monkeypatch.setattr(settings, "tenant_api_keys", {})
    monkeypatch.setattr("app.graph.tenants.find_tenant_by_api_key", lambda api_key, repo=None: tenant)
    assert require_tenant(_FakeRequest(), x_api_key="a-dynamic-tenants-key") is tenant


def test_require_admin_rejects_when_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "")
    with pytest.raises(HTTPException) as exc_info:
        require_admin(x_admin_key="anything")
    assert exc_info.value.status_code == 500


def test_require_admin_rejects_wrong_key(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "the-real-admin-key")
    with pytest.raises(HTTPException) as exc_info:
        require_admin(x_admin_key="wrong")
    assert exc_info.value.status_code == 401


def test_require_admin_accepts_correct_key(monkeypatch):
    monkeypatch.setattr(settings, "admin_api_key", "the-real-admin-key")
    assert require_admin(x_admin_key="the-real-admin-key") is None


def test_tenant_config_requires_at_least_one_knowledge_base():
    with pytest.raises(ValueError):
        TenantConfig(tenant_id="acme", gemini_api_key="fake-key", knowledge_bases=[])
