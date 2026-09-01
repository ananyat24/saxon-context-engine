# POST /api/v1/admin/connectors/{id}/reassign-tenant (app/api/admin.py) --
# moves a connector's management ownership to a different tenant without
# touching its config, sync state, or already-ingested graph data. Built
# for consolidating a connector created under a throwaway tenant (to
# unblock ingestion into a knowledge base before the real tenant had it in
# its own list) into the real tenant, without re-ingesting.
#
# Needs a real, reachable Neo4j -- same pattern as test_purge_connector_data.py.
import uuid

import pytest
from fastapi import HTTPException

from app.api import admin
from app.graph import connectors
from app.graph.graph_repository import GraphRepository


class _FakeAppState:
    def __init__(self):
        self.neo4j_client = None


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()


class _FakeRequest:
    def __init__(self):
        self.app = _FakeApp()


@pytest.fixture
def repo():
    return GraphRepository()


def test_reassign_moves_the_connector_to_the_new_tenant(repo):
    old_tenant = f"old_tenant_{uuid.uuid4().hex[:8]}"
    new_tenant = f"new_tenant_{uuid.uuid4().hex[:8]}"
    group_id = f"kb_{uuid.uuid4().hex[:8]}"
    connector = connectors.create_connector(old_tenant, "Test Connector", "web", group_id, "https://example.com", repo=repo)
    try:
        result = admin.reassign_connector_tenant(
            connector["id"], admin.ReassignConnectorTenantRequest(tenant_id=new_tenant), _FakeRequest()
        )
        assert result == {"connector_id": connector["id"], "tenant_id": new_tenant}

        assert connectors.get_connector(old_tenant, connector["id"], repo=repo) is None
        moved = connectors.get_connector(new_tenant, connector["id"], repo=repo)
        assert moved is not None
        assert moved["name"] == "Test Connector"
        assert moved["group_id"] == group_id
        assert moved["url"] == "https://example.com"
    finally:
        connectors.delete_connector(new_tenant, connector["id"], repo=repo)


def test_reassign_leaves_already_ingested_graph_data_untouched(repo):
    # The actual point of this endpoint: consolidating management ownership
    # must never require (or trigger) re-ingesting real data.
    old_tenant = f"old_tenant_{uuid.uuid4().hex[:8]}"
    new_tenant = f"new_tenant_{uuid.uuid4().hex[:8]}"
    group_id = f"kb_{uuid.uuid4().hex[:8]}"
    connector = connectors.create_connector(old_tenant, "Test Connector", "web", group_id, "https://example.com", repo=repo)
    node_uuid = str(uuid.uuid4())
    try:
        repo.execute_cypher(
            "CREATE (n:Entity {uuid: $u, group_id: $g, name: 'Untouched Entity'})", {"u": node_uuid, "g": group_id}
        )
        admin.reassign_connector_tenant(
            connector["id"], admin.ReassignConnectorTenantRequest(tenant_id=new_tenant), _FakeRequest()
        )
        remaining = repo.execute_cypher("MATCH (n:Entity {uuid: $u}) RETURN n.name AS name", {"u": node_uuid})
        assert remaining == [{"name": "Untouched Entity"}]
    finally:
        connectors.delete_connector(new_tenant, connector["id"], repo=repo)
        repo.execute_cypher("MATCH (n:Entity {uuid: $u}) DETACH DELETE n", {"u": node_uuid})


def test_reassign_404s_for_an_unknown_connector_id():
    with pytest.raises(HTTPException) as exc_info:
        admin.reassign_connector_tenant(
            "no-such-connector", admin.ReassignConnectorTenantRequest(tenant_id="whatever"), _FakeRequest()
        )
    assert exc_info.value.status_code == 404
