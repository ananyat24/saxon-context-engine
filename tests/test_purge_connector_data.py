# app/graph/connectors.py's purge_connector_data -- undoes what a specific
# connector's own syncs wrote (entities, facts, episodes) without touching
# the connector row or anything another connector/sync contributed. Built
# for real incident recovery (a bad sync that wrote wrong or partial data)
# so a retry starts genuinely clean instead of layering on top of a mess.
#
# Needs a real, reachable Neo4j -- same pattern as test_graph_connector_scope.py.
import uuid

import pytest

from app.graph import connectors
from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    return GraphRepository()


def test_purge_removes_only_this_connectors_own_entities_and_facts(repo):
    group_id = f"test_purge_{uuid.uuid4().hex[:8]}"
    connector_a = f"conn_a_{uuid.uuid4().hex[:8]}"
    connector_b = f"conn_b_{uuid.uuid4().hex[:8]}"
    ep_a, ep_b = f"ep_a_{uuid.uuid4().hex[:8]}", f"ep_b_{uuid.uuid4().hex[:8]}"
    node_a, node_b1, node_b2 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())

    try:
        repo.execute_cypher(
            "CREATE (e:Episodic {uuid: $ep, group_id: $g, connector_id: $c, name: 'ep-a'})",
            {"ep": ep_a, "g": group_id, "c": connector_a},
        )
        repo.execute_cypher(
            "CREATE (n:Entity {uuid: $u, group_id: $g, name: 'A Only Widget'})", {"u": node_a, "g": group_id}
        )
        repo.execute_cypher(
            "MATCH (e:Episodic {uuid: $ep}), (n:Entity {uuid: $u}) CREATE (e)-[:MENTIONS]->(n)",
            {"ep": ep_a, "u": node_a},
        )
        repo.execute_cypher(
            "MATCH (a:Entity {uuid: $a}) CREATE (a)-[:RELATES_TO {name: 'SELF', fact: 'A fact', episodes: [$ep]}]->(a)",
            {"a": node_a, "ep": ep_a},
        )

        repo.execute_cypher(
            "CREATE (e:Episodic {uuid: $ep, group_id: $g, connector_id: $c, name: 'ep-b'})",
            {"ep": ep_b, "g": group_id, "c": connector_b},
        )
        repo.execute_cypher(
            "CREATE (n1:Entity {uuid: $u1, group_id: $g, name: 'B Gadget'}), "
            "(n2:Entity {uuid: $u2, group_id: $g, name: 'B Gizmo'})",
            {"u1": node_b1, "u2": node_b2, "g": group_id},
        )
        repo.execute_cypher(
            "MATCH (e:Episodic {uuid: $ep}), (n1:Entity {uuid: $u1}), (n2:Entity {uuid: $u2}) "
            "CREATE (e)-[:MENTIONS]->(n1), (e)-[:MENTIONS]->(n2)",
            {"ep": ep_b, "u1": node_b1, "u2": node_b2},
        )
        repo.execute_cypher(
            "MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
            "CREATE (a)-[:RELATES_TO {name: 'CONNECTED', fact: 'B fact', episodes: [$ep]}]->(b)",
            {"a": node_b1, "b": node_b2, "ep": ep_b},
        )

        result = connectors.purge_connector_data(connector_a, group_id, repo=repo)
        assert result["facts_deleted"] == 1
        assert result["entities_deleted"] == 1
        assert result["episodes_deleted"] == 1

        remaining = repo.execute_cypher(
            "MATCH (n:Entity {group_id: $g}) RETURN n.name AS name ORDER BY n.name", {"g": group_id}
        )
        assert [r["name"] for r in remaining] == ["B Gadget", "B Gizmo"]

        remaining_facts = repo.execute_cypher(
            "MATCH ()-[r:RELATES_TO]->() WHERE r.fact IN ['A fact', 'B fact'] RETURN r.fact AS fact"
        )
        assert [r["fact"] for r in remaining_facts] == ["B fact"]

        # Connector A's episode is really gone, B's is untouched.
        assert repo.execute_cypher("MATCH (e:Episodic {uuid: $ep}) RETURN e", {"ep": ep_a}) == []
        assert len(repo.execute_cypher("MATCH (e:Episodic {uuid: $ep}) RETURN e", {"ep": ep_b})) == 1
    finally:
        repo.execute_cypher("MATCH (n) WHERE n.group_id = $g DETACH DELETE n", {"g": group_id})


def test_purge_only_strips_this_connectors_episode_from_a_shared_fact_not_the_whole_fact(repo):
    # A fact two different connectors' episodes both touched (e.g. the same
    # relationship re-confirmed by a later sync) must survive purging just
    # ONE of those connectors -- it's still real, sourced by the other one.
    group_id = f"test_purge_shared_{uuid.uuid4().hex[:8]}"
    connector_a = f"conn_a_{uuid.uuid4().hex[:8]}"
    connector_b = f"conn_b_{uuid.uuid4().hex[:8]}"
    ep_a, ep_b = f"ep_a_{uuid.uuid4().hex[:8]}", f"ep_b_{uuid.uuid4().hex[:8]}"
    node_x, node_y = str(uuid.uuid4()), str(uuid.uuid4())

    try:
        repo.execute_cypher(
            "CREATE (e:Episodic {uuid: $ep, group_id: $g, connector_id: $c, name: 'ep-a'})",
            {"ep": ep_a, "g": group_id, "c": connector_a},
        )
        repo.execute_cypher(
            "CREATE (e:Episodic {uuid: $ep, group_id: $g, connector_id: $c, name: 'ep-b'})",
            {"ep": ep_b, "g": group_id, "c": connector_b},
        )
        repo.execute_cypher(
            "CREATE (x:Entity {uuid: $x, group_id: $g, name: 'Shared X'}), "
            "(y:Entity {uuid: $y, group_id: $g, name: 'Shared Y'})",
            {"x": node_x, "y": node_y, "g": group_id},
        )
        repo.execute_cypher(
            "MATCH (x:Entity {uuid: $x}), (y:Entity {uuid: $y}) "
            "CREATE (x)-[:RELATES_TO {name: 'REL', fact: 'Shared fact', episodes: [$ep_a, $ep_b]}]->(y)",
            {"x": node_x, "y": node_y, "ep_a": ep_a, "ep_b": ep_b},
        )

        result = connectors.purge_connector_data(connector_a, group_id, repo=repo)
        assert result["facts_deleted"] == 0
        assert result["facts_detached"] == 1
        assert result["entities_deleted"] == 0  # both entities still back the surviving fact

        remaining = repo.execute_cypher(
            "MATCH ()-[r:RELATES_TO {fact: 'Shared fact'}]->() RETURN r.episodes AS episodes"
        )
        assert remaining == [{"episodes": [ep_b]}]
    finally:
        repo.execute_cypher("MATCH (n) WHERE n.group_id = $g DETACH DELETE n", {"g": group_id})


def test_purge_resets_the_connectors_own_sync_bookkeeping(repo):
    tenant_id = f"test_purge_tenant_{uuid.uuid4().hex[:8]}"
    group_id = f"test_purge_kb_{uuid.uuid4().hex[:8]}"
    connector = connectors.create_connector(tenant_id, "Purge Test", "database", group_id, "desc", repo=repo)
    try:
        connectors.record_sync_result(tenant_id, connector["id"], status="synced", content_hash="abc123", repo=repo)
        connectors.purge_connector_data(connector["id"], group_id, repo=repo)
        after = connectors.get_connector(tenant_id, connector["id"], repo=repo)
        assert after["status"] == "never_synced"
        assert after["content_hash"] is None
        assert after["last_synced_at"] is None
    finally:
        connectors.delete_connector(tenant_id, connector["id"], repo=repo)
