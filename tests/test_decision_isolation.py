# Needs a real, reachable Neo4j -- same caveat as test_decisions.py.
#
# Regression coverage for a real bug found in production: a :Decision node
# (app/graph/decisions.py's record_decision) is labeled :Entity too (the
# ontology models Decision as extending Event, which extends Entity), so
# without an explicit exclusion it was indistinguishable from real business
# data to every general-purpose entity/fact query in this codebase. Once a
# causal query about a real entity (e.g. "CX-17 Power Relay") had run once
# and recorded a Decision, every SUBSEQUENT query about that same entity --
# a plain Ask, a later "Explain why", the suggested-questions feature --
# started surfacing the Decision's own boilerplate INVOLVES-edge text
# ("Saxon generated this recommendation while analyzing: <query>") as if it
# were a real fact, and the Decision node's own auto-generated name
# ("Recommendation for: <query>") as if it were a real entity name.
import asyncio
import uuid
from unittest.mock import Mock

import pytest

from app.graph.decisions import ensure_decision_indexes, record_decision
from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    repo = GraphRepository(graphiti_instance=Mock())
    ensure_decision_indexes(repo=repo)
    return repo


def _node(repo, group_id, name):
    node_uuid = str(uuid.uuid4())
    repo.execute_cypher(
        "CREATE (n:Entity {uuid: $uuid, group_id: $group_id, name: $name, summary: $summary})",
        {"uuid": node_uuid, "group_id": group_id, "name": name, "summary": f"{name} summary"},
    )
    return node_uuid


def _real_edge(repo, source_uuid, target_uuid, fact, group_id, rel_type="RELATED_TO"):
    repo.execute_cypher(
        "MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
        "CREATE (a)-[:RELATES_TO {name: $rel_type, fact: $fact, group_id: $group_id, "
        "valid_at: datetime('2026-01-01T00:00:00Z'), invalid_at: null, expired_at: null}]->(b)",
        {"a": source_uuid, "b": target_uuid, "fact": fact, "group_id": group_id, "rel_type": rel_type},
    )


def test_a_recorded_decisions_involves_edge_never_shows_up_as_the_anchors_own_fact(repo):
    group_id = f"test_decision_isolation_{uuid.uuid4().hex[:8]}"
    try:
        anchor = _node(repo, group_id, "Decision Isolation CX-17 Power Relay")
        supplier = _node(repo, group_id, "Decision Isolation Supplier")
        _real_edge(
            repo, anchor, supplier, "Decision Isolation CX-17 Power Relay is sourced from Decision Isolation Supplier.",
            group_id, rel_type="SOURCED_FROM",
        )

        record_decision(
            repo,
            group_id=group_id,
            anchor_uuid=anchor,
            query="Why is Decision Isolation CX-17 Power Relay affected?",
            recommendation_text="Recommendation: qualify a second supplier.",
            rationale="Some rationale",
        )

        facts = repo.direct_facts_for(anchor, None)
        fact_texts = {f["fact"] for f in facts}

        # The real, pre-existing fact is still there...
        assert "Decision Isolation CX-17 Power Relay is sourced from Decision Isolation Supplier." in fact_texts
        # ...but the Decision's own boilerplate INVOLVES-edge text must not be.
        assert not any("Saxon generated this recommendation" in t for t in fact_texts)
        assert len(facts) == 1
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_a_decisions_own_name_never_resolves_as_a_query_anchor(repo):
    group_id = f"test_decision_isolation_resolve_{uuid.uuid4().hex[:8]}"
    try:
        anchor = _node(repo, group_id, "Decision Isolation Widget")
        query_text = "Why is Decision Isolation Widget at risk?"

        decision_id = record_decision(
            repo,
            group_id=group_id,
            anchor_uuid=anchor,
            query=query_text,
            recommendation_text="Recommendation: investigate further.",
            rationale="Some rationale",
        )
        # Sanity: the Decision node really was created with the name a
        # search for its own text would otherwise match against.
        decision_name = repo.execute_cypher(
            "MATCH (d:Decision {uuid: $uuid}) RETURN d.name AS name", {"uuid": decision_id}
        )[0]["name"]
        assert decision_name == f"Recommendation for: {query_text}"

        # A query naming the Decision's own auto-generated name must not
        # resolve the Decision node itself -- it's an audit record, not
        # something a person is asking about. The Decision's name happens to
        # embed the real anchor's name too (it's built from the original
        # query text), so the *real* entity correctly still resolves -- this
        # asserts specifically that the Decision node never does, not that
        # nothing does.
        anchor_result, _second, _facts = asyncio.run(
            repo.causal_chain_for_query(f"What's changed recently about {decision_name}?", [group_id], None)
        )
        assert anchor_result is not None
        assert anchor_result["uuid"] == anchor
        assert anchor_result["name"] == "Decision Isolation Widget"
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_causal_chain_never_walks_through_a_decision_node(repo):
    group_id = f"test_decision_isolation_walk_{uuid.uuid4().hex[:8]}"
    try:
        component = _node(repo, group_id, "Decision Isolation Component")
        supplier = _node(repo, group_id, "Decision Isolation Component Supplier")
        repo.execute_cypher(
            "MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
            "CREATE (a)-[:RELATES_TO {name: 'SOURCED_FROM', fact: 'Decision Isolation Component is sourced from Decision Isolation Component Supplier.', "
            "group_id: $group_id, valid_at: datetime('2026-01-01T00:00:00Z'), invalid_at: null, expired_at: null}]->(b)",
            {"a": component, "b": supplier, "group_id": group_id},
        )
        record_decision(
            repo, group_id=group_id, anchor_uuid=component,
            query="Why is Decision Isolation Component affected?",
            recommendation_text="Recommendation text.", rationale="Rationale",
        )

        anchor, second_entity, facts = asyncio.run(
            repo.causal_chain_for_query("What's going on with Decision Isolation Component?", [group_id], None)
        )
        fact_texts = {f["fact"] for f in facts}
        assert "Decision Isolation Component is sourced from Decision Isolation Component Supplier." in fact_texts
        assert not any("Saxon generated this recommendation" in t for t in fact_texts)
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})
