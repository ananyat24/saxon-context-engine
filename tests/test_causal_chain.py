# Needs a real, reachable Neo4j -- same caveat as test_entity_reconciliation.py.
# Creates and cleans up its own throwaway :Entity nodes under a randomly-
# suffixed group_id.
#
# Covers GraphRepository.causal_chain_for_query/_causal_chain_facts_from --
# the causal-reasoning retriever added for the Context Graph/Layer/Engine
# pivot (see CLAUDE.md). Built to walk the pivot's own illustrative example:
# an at-risk Order -> its Product -> a Component -> the Supplier -> an open
# QualityEvent.
import asyncio
import uuid
from unittest.mock import Mock

import pytest

from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    return GraphRepository(graphiti_instance=Mock())


def _node(repo, group_id, name):
    node_uuid = str(uuid.uuid4())
    repo.execute_cypher(
        "CREATE (n:Entity {uuid: $uuid, group_id: $group_id, name: $name, summary: $summary})",
        {"uuid": node_uuid, "group_id": group_id, "name": name, "summary": f"{name} summary"},
    )
    return node_uuid


def _causal_edge(repo, source_uuid, target_uuid, rel_type, fact, group_id):
    repo.execute_cypher(
        "MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
        "CREATE (a)-[:RELATES_TO {name: $rel_type, fact: $fact, group_id: $group_id, "
        "valid_at: datetime('2026-01-01T00:00:00Z'), invalid_at: null, expired_at: null}]->(b)",
        {"a": source_uuid, "b": target_uuid, "rel_type": rel_type, "fact": fact, "group_id": group_id},
    )


def _non_causal_edge(repo, source_uuid, target_uuid, fact, group_id):
    repo.execute_cypher(
        "MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
        "CREATE (a)-[:RELATES_TO {name: 'RELATED_TO', fact: $fact, group_id: $group_id, "
        "valid_at: datetime('2026-01-01T00:00:00Z'), invalid_at: null, expired_at: null}]->(b)",
        {"a": source_uuid, "b": target_uuid, "fact": fact, "group_id": group_id},
    )


def test_walks_a_multi_hop_causal_chain_from_the_resolved_entity(repo):
    group_id = f"test_causal_{uuid.uuid4().hex[:8]}"
    try:
        order = _node(repo, group_id, "Causal Test Order 9001")
        product = _node(repo, group_id, "Causal Test Widget")
        component = _node(repo, group_id, "Causal Test Bearing")
        supplier = _node(repo, group_id, "Causal Test Supplier Co")

        _causal_edge(repo, order, product, "DEPENDS_ON", "Causal Test Order 9001 depends on Causal Test Widget.", group_id)
        _causal_edge(repo, product, component, "DEPENDS_ON", "Causal Test Widget depends on Causal Test Bearing.", group_id)
        _causal_edge(repo, component, supplier, "SOURCED_FROM", "Causal Test Bearing is sourced from Causal Test Supplier Co.", group_id)

        anchor, facts = asyncio.run(
            repo.causal_chain_for_query("What's going on with Causal Test Order 9001?", [group_id], None)
        )
        assert anchor["name"] == "Causal Test Order 9001"
        fact_texts = {f["fact"] for f in facts}
        assert "Causal Test Order 9001 depends on Causal Test Widget." in fact_texts
        assert "Causal Test Widget depends on Causal Test Bearing." in fact_texts
        assert "Causal Test Bearing is sourced from Causal Test Supplier Co." in fact_texts
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_non_causal_relationship_types_are_not_walked(repo):
    group_id = f"test_causal_noncausal_{uuid.uuid4().hex[:8]}"
    try:
        order = _node(repo, group_id, "Causal Noncausal Order")
        unrelated = _node(repo, group_id, "Causal Noncausal Sibling Order")
        _non_causal_edge(repo, order, unrelated, "Causal Noncausal Order is related to Causal Noncausal Sibling Order.", group_id)

        anchor, facts = asyncio.run(
            repo.causal_chain_for_query("What happened with Causal Noncausal Order?", [group_id], None)
        )
        assert anchor is not None
        assert facts == []
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_causal_walk_never_crosses_into_another_groups_node(repo):
    # Regression: the multi-hop MATCH originally constrained only the anchor
    # node's uuid and the relationship types, with no group_id check on any
    # node along the path -- unlike every other relationship traversal in
    # this codebase (see app/graph/authorization.py, app/api/odata.py's
    # list_facts_odata). In practice a RELATES_TO edge shouldn't naturally
    # span two knowledge bases, but that was an unenforced assumption on the
    # one multi-hop traversal in the codebase, and a causal answer also gets
    # written into a permanent, auditable :Decision node -- so this proves
    # the walk actually stops at a group_id boundary rather than relying on
    # the assumption holding.
    group_id = f"test_causal_cross_{uuid.uuid4().hex[:8]}"
    other_group_id = f"test_causal_cross_other_{uuid.uuid4().hex[:8]}"
    try:
        order = _node(repo, group_id, "Causal Cross Order")
        other_tenants_node = _node(repo, other_group_id, "Causal Cross Other Tenant Secret")
        _causal_edge(
            repo, order, other_tenants_node,
            "DEPENDS_ON", "Causal Cross Order depends on Causal Cross Other Tenant Secret.", group_id,
        )

        anchor, facts = asyncio.run(
            repo.causal_chain_for_query("What happened with Causal Cross Order?", [group_id], None)
        )
        assert anchor is not None
        assert facts == []
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": other_group_id})


def test_unresolved_query_returns_no_anchor_and_no_facts(repo):
    anchor, facts = asyncio.run(
        repo.causal_chain_for_query("What happened with Totally Unknown Causal Entity?", ["nonexistent-group"], None)
    )
    assert anchor is None
    assert facts == []


def test_visibility_filter_excludes_a_hop_through_a_hidden_node(repo):
    group_id = f"test_causal_visibility_{uuid.uuid4().hex[:8]}"
    try:
        order = _node(repo, group_id, "Causal Visibility Order")
        hidden_product = _node(repo, group_id, "Causal Visibility Hidden Product")
        _causal_edge(repo, order, hidden_product, "DEPENDS_ON", "Causal Visibility Order depends on the hidden product.", group_id)

        visible_uuids = {order}  # the target end is deliberately excluded
        anchor, facts = asyncio.run(
            repo.causal_chain_for_query("What happened with Causal Visibility Order?", [group_id], visible_uuids)
        )
        assert anchor is not None
        assert facts == []
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})
