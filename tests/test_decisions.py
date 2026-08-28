# Needs a real, reachable Neo4j. Covers app/graph/decisions.py's
# record_decision() -- the causal-chain retriever's recommendation gets
# logged as a real :Entity:Decision node (ontology/core.yaml's Decision
# type, previously defined but never written to the graph), so it's an
# auditable fact, not a throwaway string. See CLAUDE.md's pivot notes.
import uuid

import pytest

from app.graph.decisions import ensure_decision_indexes, record_decision
from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    repo = GraphRepository()
    ensure_decision_indexes(repo=repo)
    return repo


def test_record_decision_creates_an_entity_decision_node_linked_to_the_anchor(repo):
    group_id = f"test_decision_{uuid.uuid4().hex[:8]}"
    anchor_uuid = str(uuid.uuid4())
    try:
        repo.execute_cypher(
            "CREATE (n:Entity {uuid: $uuid, group_id: $group_id, name: 'Decision Test Anchor'})",
            {"uuid": anchor_uuid, "group_id": group_id},
        )

        decision_id = record_decision(
            repo,
            group_id=group_id,
            anchor_uuid=anchor_uuid,
            query="What is going on with Decision Test Anchor?",
            recommendation_text="Recommendation: escalate to the account owner.",
            rationale="Fact one; fact two",
        )

        rows = repo.execute_cypher(
            "MATCH (d:Entity:Decision {uuid: $uuid}) RETURN d.description AS description, "
            "d.decision_status AS status, d.source_system AS source_system, d.group_id AS group_id",
            {"uuid": decision_id},
        )
        assert rows[0]["description"] == "Recommendation: escalate to the account owner."
        assert rows[0]["status"] == "proposed"
        assert rows[0]["source_system"] == "saxon.causal_engine"
        assert rows[0]["group_id"] == group_id

        edge_rows = repo.execute_cypher(
            "MATCH (d:Decision {uuid: $uuid})-[r:RELATES_TO]->(n:Entity {uuid: $anchor}) "
            "RETURN r.name AS type",
            {"uuid": decision_id, "anchor": anchor_uuid},
        )
        assert edge_rows[0]["type"] == "INVOLVES"
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})
