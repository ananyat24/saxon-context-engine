import pytest
from fastapi import HTTPException

from app.config import KnowledgeBase, TenantConfig, settings
from app.security import require_tenant, resolve_knowledge_base


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
    with pytest.raises(HTTPException) as exc_info:
        require_tenant(x_api_key="not-a-real-key")
    assert exc_info.value.status_code == 401


def test_require_tenant_accepts_known_key(monkeypatch, tenant):
    monkeypatch.setattr(settings, "tenant_api_keys", {"real-key": tenant})
    assert require_tenant(x_api_key="real-key") is tenant


def test_tenant_config_requires_at_least_one_knowledge_base():
    with pytest.raises(ValueError):
        TenantConfig(tenant_id="acme", gemini_api_key="fake-key", knowledge_bases=[])
