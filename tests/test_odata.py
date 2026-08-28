# Needs a real, reachable Neo4j. Calls app/api/odata.py's route functions
# directly against a fake Request (same "fake Request, real Neo4j" pattern
# test_webhooks.py uses for app.state), rather than a full FastAPI
# TestClient -- these are plain functions with FastAPI Depends() resolved
# by hand (tenant/knowledge_base passed straight in), so no ASGI app/HTTP
# layer is needed to exercise the actual Cypher + OData-shape logic.
#
# Covers the BI surface added for the Context Graph/Layer/Engine pivot (see
# CLAUDE.md): Power BI's OData Feed connector points at these routes
# directly, tenant-scoped the same way every other route in this app is.
import uuid

import pytest

from app.api import odata
from app.config import KnowledgeBase, TenantConfig
from app.graph.graph_repository import GraphRepository


class _FakeAppState:
    def __init__(self, neo4j_client=None):
        self.neo4j_client = neo4j_client


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()


class _FakeURL:
    def __str__(self):
        return "http://testserver/"


class _FakeRequest:
    def __init__(self):
        self.app = _FakeApp()
        self.base_url = _FakeURL()


@pytest.fixture
def repo():
    return GraphRepository()


def _tenant(group_id: str) -> TenantConfig:
    return TenantConfig(
        tenant_id="test-odata-tenant", gemini_api_key="fake", knowledge_bases=[KnowledgeBase(id=group_id, label="KB")]
    )


def test_service_document_lists_entities_and_facts():
    doc = odata.service_document(_FakeRequest(), tenant=_tenant("kb1"))
    names = {row["name"] for row in doc["value"]}
    assert names == {"Entities", "Facts"}
    assert doc["@odata.context"].endswith("/$metadata")


def test_metadata_document_is_xml_and_declares_both_entity_types():
    resp = odata.metadata_document()
    assert resp.media_type == "application/xml"
    body = resp.body.decode()
    assert '<EntityType Name="Entity">' in body
    assert '<EntityType Name="Fact">' in body


def test_list_entities_odata_returns_only_this_groups_nodes(repo):
    group_id = f"test_odata_entities_{uuid.uuid4().hex[:8]}"
    other_group = f"test_odata_entities_other_{uuid.uuid4().hex[:8]}"
    try:
        repo.execute_cypher(
            "CREATE (n:Entity:Organization {uuid: $u, group_id: $g, name: 'OData Test Co', summary: 'a company'})",
            {"u": str(uuid.uuid4()), "g": group_id},
        )
        repo.execute_cypher(
            "CREATE (n:Entity {uuid: $u, group_id: $g, name: 'Other Tenant Co'})",
            {"u": str(uuid.uuid4()), "g": other_group},
        )

        request = _FakeRequest()
        result = odata.list_entities_odata(request, top=None, knowledge_base=None, tenant=_tenant(group_id))

        names = {row["name"] for row in result["value"]}
        assert "OData Test Co" in names
        assert "Other Tenant Co" not in names
        row = next(r for r in result["value"] if r["name"] == "OData Test Co")
        assert row["entity_type"] == "Organization"
        assert result["@odata.context"].endswith("/$metadata#Entities")
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": other_group})


def test_list_facts_odata_reports_temporal_validity(repo):
    group_id = f"test_odata_facts_{uuid.uuid4().hex[:8]}"
    try:
        a = str(uuid.uuid4())
        b = str(uuid.uuid4())
        repo.execute_cypher(
            "CREATE (a:Entity {uuid: $a, group_id: $g, name: 'OData Fact Source'}), "
            "(b:Entity {uuid: $b, group_id: $g, name: 'OData Fact Target'}), "
            "(a)-[:RELATES_TO {name: 'DEPENDS_ON', fact: 'OData Fact Source depends on OData Fact Target.', "
            "group_id: $g, valid_at: datetime('2026-01-01T00:00:00Z'), invalid_at: null, expired_at: null}]->(b)",
            {"a": a, "b": b, "g": group_id},
        )

        request = _FakeRequest()
        result = odata.list_facts_odata(request, top=None, knowledge_base=None, tenant=_tenant(group_id))

        row = next(r for r in result["value"] if r["fact"] == "OData Fact Source depends on OData Fact Target.")
        assert row["is_valid"] is True
        assert row["relationship_type"] == "DEPENDS_ON"
        assert row["source"] == "OData Fact Source"
        assert row["target"] == "OData Fact Target"
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_top_is_clamped_to_the_configured_max():
    assert odata._clamp_top(None) == odata._DEFAULT_TOP
    assert odata._clamp_top(999999) == odata._MAX_TOP
    assert odata._clamp_top(0) == 1
    assert odata._clamp_top(50) == 50
