# Needs a real, reachable Neo4j: same caveat as test_graph.py/
# test_entity_reconciliation.py. Creates and cleans up its own throwaway
# :Tenant nodes under randomly-suffixed tenant_ids, so this never touches a
# real tenant and is safe to run repeatedly.
import uuid

import pytest

from app.config import KnowledgeBase
from app.graph.graph_repository import GraphRepository
from app.graph import tenants


@pytest.fixture
def repo():
    repo = GraphRepository()
    tenants.ensure_tenant_indexes(repo=repo)  # the uniqueness constraint duplicate-id tests rely on
    return repo


def test_create_then_find_tenant_by_the_returned_api_key(repo):
    tenant_id = f"test_tenant_{uuid.uuid4().hex[:8]}"
    try:
        raw_key, summary = tenants.create_tenant(
            tenant_id, "fake-gemini-key", [KnowledgeBase(id="kb1", label="KB One")], repo=repo
        )
        assert summary["tenant_id"] == tenant_id
        assert summary["api_key_last4"] == raw_key[-4:]

        found = tenants.find_tenant_by_api_key(raw_key, repo=repo)
        assert found is not None
        assert found.tenant_id == tenant_id
        assert found.gemini_api_key == "fake-gemini-key"
        assert found.knowledge_base_ids() == {"kb1"}
    finally:
        tenants.delete_tenant(tenant_id, repo=repo)


def test_find_tenant_by_api_key_never_matches_the_raw_key_against_a_stored_hash(repo):
    # The point of hashing (see app/graph/tenants.py's module docstring):
    # looking up by anything other than the actual raw key: including the
    # hash itself: must never resolve to the tenant.
    tenant_id = f"test_tenant_nohash_{uuid.uuid4().hex[:8]}"
    try:
        raw_key, _ = tenants.create_tenant(
            tenant_id, "fake-gemini-key", [KnowledgeBase(id="kb1", label="KB One")], repo=repo
        )
        assert tenants.find_tenant_by_api_key("not-the-real-key", repo=repo) is None
        assert tenants.find_tenant_by_api_key(tenants._hash_api_key(raw_key), repo=repo) is None
    finally:
        tenants.delete_tenant(tenant_id, repo=repo)


def test_create_tenant_rejects_a_duplicate_tenant_id(repo):
    tenant_id = f"test_tenant_dupe_{uuid.uuid4().hex[:8]}"
    try:
        tenants.create_tenant(tenant_id, "key-a", [KnowledgeBase(id="kb1", label="KB")], repo=repo)
        with pytest.raises(ValueError, match="already exists"):
            tenants.create_tenant(tenant_id, "key-b", [KnowledgeBase(id="kb2", label="KB2")], repo=repo)
    finally:
        tenants.delete_tenant(tenant_id, repo=repo)


def test_list_tenants_never_includes_the_api_key_or_its_hash(repo):
    tenant_id = f"test_tenant_list_{uuid.uuid4().hex[:8]}"
    try:
        raw_key, _ = tenants.create_tenant(
            tenant_id, "fake-gemini-key", [KnowledgeBase(id="kb1", label="KB One")], repo=repo
        )
        rows = tenants.list_tenants(repo=repo)
        row = next(r for r in rows if r["tenant_id"] == tenant_id)
        assert row["api_key_last4"] == raw_key[-4:]
        assert "api_key" not in row
        assert "api_key_hash" not in row
        assert "gemini_api_key" not in row
    finally:
        tenants.delete_tenant(tenant_id, repo=repo)


def test_delete_tenant_removes_it_and_returns_false_when_already_gone(repo):
    tenant_id = f"test_tenant_delete_{uuid.uuid4().hex[:8]}"
    tenants.create_tenant(tenant_id, "fake-gemini-key", [KnowledgeBase(id="kb1", label="KB")], repo=repo)

    assert tenants.delete_tenant(tenant_id, repo=repo) is True
    assert tenants.delete_tenant(tenant_id, repo=repo) is False


def test_hash_is_deterministic_but_not_reversible_looking():
    h1 = tenants._hash_api_key("some-key")
    h2 = tenants._hash_api_key("some-key")
    assert h1 == h2
    assert h1 != "some-key"
    assert len(h1) == 64  # sha256 hex digest


def test_find_tenant_by_tenant_id_checks_static_config_first(monkeypatch, repo):
    from app.config import KnowledgeBase, TenantConfig

    static_tenant = TenantConfig(
        tenant_id="static1", gemini_api_key="static-key", knowledge_bases=[KnowledgeBase(id="kb1", label="KB")]
    )
    monkeypatch.setattr("app.graph.tenants.settings.tenant_api_keys", {"any-key": static_tenant})

    found = tenants.find_tenant_by_tenant_id("static1", repo=repo)
    assert found is static_tenant


def test_find_tenant_by_tenant_id_falls_back_to_neo4j(monkeypatch, repo):
    monkeypatch.setattr("app.graph.tenants.settings.tenant_api_keys", {})
    tenant_id = f"test_tenant_by_id_{uuid.uuid4().hex[:8]}"
    try:
        tenants.create_tenant(tenant_id, "fake-gemini-key", [KnowledgeBase(id="kb1", label="KB")], repo=repo)
        found = tenants.find_tenant_by_tenant_id(tenant_id, repo=repo)
        assert found is not None
        assert found.tenant_id == tenant_id
    finally:
        tenants.delete_tenant(tenant_id, repo=repo)


def test_find_tenant_by_tenant_id_returns_none_when_nowhere(monkeypatch, repo):
    monkeypatch.setattr("app.graph.tenants.settings.tenant_api_keys", {})
    assert tenants.find_tenant_by_tenant_id("no-such-tenant-anywhere", repo=repo) is None
