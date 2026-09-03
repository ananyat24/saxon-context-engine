# The bug this pins: GET /graph/nodes and /graph/relationships only ever
# filtered by knowledge_base (group_id), so the connector preview modal
# ("what's been pulled into the graph from this connector") actually showed
# every OTHER connector feeding the same knowledge base too: a knowledge
# base commonly has more than one connector. Fixed via an optional
# ?connector_id= filter, backed by the Episodic.connector_id tag
# app/ingestion/connector_sync.py now writes on every synced episode (see
# that module and tests/test_connector_sync.py for the write side).
#
# Needs a real, reachable Neo4j (same caveat as test_odata.py): builds a
# small real graph shaped the way Graphiti actually writes it (:Episodic
# -[:MENTIONS]-> :Entity, and a RELATES_TO edge's own `episodes` property),
# rather than mocking Cypher results, since the whole point is the join
# logic in app/api/graph.py's real queries.
import uuid

import pytest

from app.api import graph as graph_api
from app.config import KnowledgeBase, TenantConfig
from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    return GraphRepository()


class _FakeAppState:
    def __init__(self):
        self.neo4j_client = None


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()


class _FakeRequest:
    def __init__(self):
        self.app = _FakeApp()


def _tenant(group_id: str) -> TenantConfig:
    return TenantConfig(
        tenant_id="test-graph-scope-tenant", gemini_api_key="fake", knowledge_bases=[KnowledgeBase(id=group_id, label="KB")]
    )


def test_connector_id_filter_excludes_another_connectors_facts_in_the_same_kb(repo):
    group_id = f"test_scope_{uuid.uuid4().hex[:8]}"
    connector_a = f"connector_a_{uuid.uuid4().hex[:8]}"
    connector_b = f"connector_b_{uuid.uuid4().hex[:8]}"
    ep_a, ep_b = f"ep_a_{uuid.uuid4().hex[:8]}", f"ep_b_{uuid.uuid4().hex[:8]}"
    node_a, node_b1, node_b2 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())

    try:
        # Connector A's own episode -> one entity, one fact.
        repo.execute_cypher(
            "CREATE (e:Episodic {uuid: $ep, group_id: $g, connector_id: $c, name: 'ep-a'})",
            {"ep": ep_a, "g": group_id, "c": connector_a},
        )
        repo.execute_cypher(
            "CREATE (n:Entity {uuid: $u, group_id: $g, name: 'Connector A Widget'})",
            {"u": node_a, "g": group_id},
        )
        repo.execute_cypher(
            "MATCH (e:Episodic {uuid: $ep}), (n:Entity {uuid: $u}) CREATE (e)-[:MENTIONS]->(n)",
            {"ep": ep_a, "u": node_a},
        )
        repo.execute_cypher(
            "MATCH (a:Entity {uuid: $a}) CREATE (a)-[:RELATES_TO {name: 'SELF_REFERENCES', fact: 'A fact', "
            "episodes: [$ep]}]->(a)",
            {"a": node_a, "ep": ep_a},
        )

        # Connector B, feeding the SAME knowledge base: this is the exact
        # shape of the bug: same group_id, different connector.
        repo.execute_cypher(
            "CREATE (e:Episodic {uuid: $ep, group_id: $g, connector_id: $c, name: 'ep-b'})",
            {"ep": ep_b, "g": group_id, "c": connector_b},
        )
        repo.execute_cypher(
            "CREATE (n1:Entity {uuid: $u1, group_id: $g, name: 'Connector B Gadget'}), "
            "(n2:Entity {uuid: $u2, group_id: $g, name: 'Connector B Gizmo'})",
            {"u1": node_b1, "u2": node_b2, "g": group_id},
        )
        repo.execute_cypher(
            "MATCH (e:Episodic {uuid: $ep}), (n1:Entity {uuid: $u1}), (n2:Entity {uuid: $u2}) "
            "CREATE (e)-[:MENTIONS]->(n1), (e)-[:MENTIONS]->(n2)",
            {"ep": ep_b, "u1": node_b1, "u2": node_b2},
        )
        repo.execute_cypher(
            "MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
            "CREATE (a)-[:RELATES_TO {name: 'CONNECTED_TO', fact: 'B fact', episodes: [$ep]}]->(b)",
            {"a": node_b1, "b": node_b2, "ep": ep_b},
        )

        request = _FakeRequest()
        tenant = _tenant(group_id)

        # Without connector_id (the pre-fix behavior), sees everything in
        # the knowledge base, both connectors' entities.
        all_nodes = graph_api.get_nodes(request, knowledge_base=group_id, tenant=tenant)
        assert {n["name"] for n in all_nodes} == {"Connector A Widget", "Connector B Gadget", "Connector B Gizmo"}

        # With connector_id=A, only A's own entity and fact, not B's,
        # despite B feeding the same group_id.
        a_nodes = graph_api.get_nodes(request, knowledge_base=group_id, connector_id=connector_a, tenant=tenant)
        assert [n["name"] for n in a_nodes] == ["Connector A Widget"]

        a_rels = graph_api.get_relationships(request, knowledge_base=group_id, connector_id=connector_a, tenant=tenant)
        assert len(a_rels) == 1
        assert a_rels[0]["fact"] == "A fact"

        # With connector_id=B: only B's own two entities and one fact.
        b_nodes = graph_api.get_nodes(request, knowledge_base=group_id, connector_id=connector_b, tenant=tenant)
        assert {n["name"] for n in b_nodes} == {"Connector B Gadget", "Connector B Gizmo"}

        b_rels = graph_api.get_relationships(request, knowledge_base=group_id, connector_id=connector_b, tenant=tenant)
        assert len(b_rels) == 1
        assert b_rels[0]["fact"] == "B fact"

        # An id with no episodes at all: genuinely nothing, not an error.
        assert graph_api.get_nodes(request, knowledge_base=group_id, connector_id="no-such-connector", tenant=tenant) == []
        assert (
            graph_api.get_relationships(request, knowledge_base=group_id, connector_id="no-such-connector", tenant=tenant)
            == []
        )
    finally:
        repo.execute_cypher(
            "MATCH (n) WHERE n.group_id = $g OR n.uuid IN [$ep_a, $ep_b] DETACH DELETE n",
            {"g": group_id, "ep_a": ep_a, "ep_b": ep_b},
        )
