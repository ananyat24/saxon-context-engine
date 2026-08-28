# Needs a real, reachable Neo4j -- same caveat as test_decision_isolation.py.
#
# Regression/feature coverage for real source-document traceability: a fact
# used to only ever say WHICH knowledge base it came from (group_id), never
# the actual document/row it was extracted from -- exactly what the user
# meant by "traceability and explainability" needing "a connection to
# SOURCES, specific places the data was found". Graphiti already stores this
# on every RELATES_TO edge as `episodes` (a list of Episodic-node uuids), and
# every Episodic node carries the ingest-time `source_description` (e.g.
# "orders.csv (Order)" -- see app/ingestion/file_source.py's SourceRecord).
# GraphRepository._resolve_episode_sources resolves that property back to
# each fact as a "sources" list; this file checks it actually lands there
# across every fact-building path, not just the helper in isolation.
import asyncio
import uuid
from unittest.mock import Mock

import pytest

from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    # search_graphiti_facts short-circuits to [] when self.graphiti is falsy
    # (see its own early-return guard) -- a Mock() satisfies that without
    # ever needing a real Graphiti/LLM client, same convention as
    # test_decision_isolation.py's fixture.
    return GraphRepository(graphiti_instance=Mock())


def _node(repo, group_id, name):
    node_uuid = str(uuid.uuid4())
    repo.execute_cypher(
        "CREATE (n:Entity {uuid: $uuid, group_id: $group_id, name: $name, summary: $summary})",
        {"uuid": node_uuid, "group_id": group_id, "name": name, "summary": f"{name} summary"},
    )
    return node_uuid


def _episode(repo, group_id, source_description):
    ep_uuid = str(uuid.uuid4())
    repo.execute_cypher(
        "CREATE (e:Episodic {uuid: $uuid, group_id: $group_id, name: $uuid, source_description: $sd})",
        {"uuid": ep_uuid, "group_id": group_id, "sd": source_description},
    )
    return ep_uuid


def _edge(repo, source_uuid, target_uuid, fact, group_id, episodes, rel_type="RELATED_TO"):
    repo.execute_cypher(
        "MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
        "CREATE (a)-[:RELATES_TO {name: $rel_type, fact: $fact, group_id: $group_id, "
        "episodes: $episodes, valid_at: datetime('2026-01-01T00:00:00Z'), invalid_at: null, expired_at: null}]->(b)",
        {"a": source_uuid, "b": target_uuid, "fact": fact, "group_id": group_id, "episodes": episodes, "rel_type": rel_type},
    )


def test_resolve_episode_sources_maps_uuids_to_source_description(repo):
    group_id = f"test_fact_sources_helper_{uuid.uuid4().hex[:8]}"
    try:
        ep_a = _episode(repo, group_id, "orders.csv (Order)")
        ep_b = _episode(repo, group_id, "accounts.csv (Account)")
        resolved = repo._resolve_episode_sources([[ep_a], [ep_a, ep_b], [], ["not-a-real-uuid"]])
        assert resolved[0] == ["orders.csv (Order)"]
        assert resolved[1] == sorted(["orders.csv (Order)", "accounts.csv (Account)"])
        assert resolved[2] == []
        assert resolved[3] == []
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_direct_facts_carry_their_real_source_document(repo):
    group_id = f"test_fact_sources_direct_{uuid.uuid4().hex[:8]}"
    try:
        ep = _episode(repo, group_id, "orders.csv (Order)")
        anchor = _node(repo, group_id, "Fact Sources Order 1")
        supplier = _node(repo, group_id, "Fact Sources Supplier")
        _edge(repo, anchor, supplier, "Fact Sources Order 1 is sourced from Fact Sources Supplier.", group_id, [ep])

        facts = repo.direct_facts_for(anchor, None)
        assert len(facts) == 1
        assert facts[0]["sources"] == ["orders.csv (Order)"]
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_direct_facts_with_no_episodes_get_an_empty_sources_list_not_an_error(repo):
    group_id = f"test_fact_sources_empty_{uuid.uuid4().hex[:8]}"
    try:
        anchor = _node(repo, group_id, "Fact Sources No Episode Order")
        supplier = _node(repo, group_id, "Fact Sources No Episode Supplier")
        _edge(repo, anchor, supplier, "Fact Sources No Episode Order is sourced from Fact Sources No Episode Supplier.", group_id, [])

        facts = repo.direct_facts_for(anchor, None)
        assert facts[0]["sources"] == []
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_two_entity_causal_path_facts_each_carry_their_own_source(repo):
    group_id = f"test_fact_sources_causal_path_{uuid.uuid4().hex[:8]}"
    try:
        ep1 = _episode(repo, group_id, "quality_events.csv (QualityEvent)")
        ep2 = _episode(repo, group_id, "suppliers.csv (Supplier)")
        a = _node(repo, group_id, "Fact Sources Component")
        mid = _node(repo, group_id, "Fact Sources Supplier Two")
        b = _node(repo, group_id, "Fact Sources Quality Event")
        _edge(repo, a, mid, "Fact Sources Component is sourced from Fact Sources Supplier Two.", group_id, [ep2], rel_type="SOURCED_FROM")
        _edge(repo, mid, b, "Fact Sources Supplier Two caused Fact Sources Quality Event.", group_id, [ep1], rel_type="CAUSED_BY")

        _anchor, _second, facts = asyncio.run(
            repo.causal_chain_for_query("What's going on with Fact Sources Component?", [group_id], None)
        )
        by_fact = {f["fact"]: f["sources"] for f in facts}
        assert by_fact["Fact Sources Component is sourced from Fact Sources Supplier Two."] == ["suppliers.csv (Supplier)"]
        assert by_fact["Fact Sources Supplier Two caused Fact Sources Quality Event."] == ["quality_events.csv (QualityEvent)"]
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_plain_ask_two_entity_connection_facts_carry_sources_too(repo):
    group_id = f"test_fact_sources_plain_path_{uuid.uuid4().hex[:8]}"
    try:
        ep = _episode(repo, group_id, "org_chart.csv (Employee)")
        a = _node(repo, group_id, "Fact Sources Industry")
        b = _node(repo, group_id, "Fact Sources Employee")
        _edge(repo, a, b, "Fact Sources Industry employs Fact Sources Employee.", group_id, [ep], rel_type="RELATED_TO")

        facts = asyncio.run(repo.search_graphiti_facts(
            "How is Fact Sources Industry connected to Fact Sources Employee?", [group_id], None
        ))
        assert any(f.get("sources") == ["org_chart.csv (Employee)"] for f in facts)
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})
