# Needs a real, reachable Neo4j -- same caveat as test_graph.py. Creates and
# cleans up its own throwaway :Entity nodes under randomly-suffixed group_ids,
# so this never touches real ingested data and is safe to run repeatedly.
#
# Covers the deterministic cross-connector reconciliation added to
# _match_entities_by_name/_resolve_named_entities/search_graphiti_facts (see
# CLAUDE.md's v2.5 status note): the same exact entity name appearing in two
# different connectors' group_ids should resolve to one pooled set of facts,
# not just whichever node happened to be picked first.
import asyncio
import uuid
from unittest.mock import Mock

import pytest

from app.graph.graph_repository import GraphRepository, _normalize_entity_name


@pytest.fixture
def repo():
    return GraphRepository(graphiti_instance=Mock())


def _make_node(repo, group_id, name):
    node_uuid = str(uuid.uuid4())
    repo.execute_cypher(
        "CREATE (n:Entity {uuid: $uuid, group_id: $group_id, name: $name, summary: $summary})",
        {"uuid": node_uuid, "group_id": group_id, "name": name, "summary": f"{name} summary"},
    )
    return node_uuid


def _make_edge(repo, source_uuid, target_uuid, fact):
    repo.execute_cypher(
        "MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
        "CREATE (a)-[:RELATES_TO {fact: $fact, valid_at: datetime('2026-01-01T00:00:00Z'), "
        "invalid_at: null, expired_at: null}]->(b)",
        {"a": source_uuid, "b": target_uuid, "fact": fact},
    )


def test_same_name_across_two_groups_pools_facts_from_both(repo):
    group_a = f"test_reconcile_a_{uuid.uuid4().hex[:8]}"
    group_b = f"test_reconcile_b_{uuid.uuid4().hex[:8]}"
    try:
        entity_a = _make_node(repo, group_a, "Acme Reconciliation Test Co")
        crm_owner = _make_node(repo, group_a, "Some CRM Owner")
        _make_edge(repo, crm_owner, entity_a, "Some CRM Owner owns Acme Reconciliation Test Co.")

        entity_b = _make_node(repo, group_b, "Acme Reconciliation Test Co")
        email_sender = _make_node(repo, group_b, "Some Email Sender")
        _make_edge(repo, email_sender, entity_b, "Some Email Sender flagged Acme Reconciliation Test Co as at risk.")

        facts = asyncio.run(
            repo.search_graphiti_facts(
                "What is going on with Acme Reconciliation Test Co?",
                group_ids=[group_a, group_b],
                visible_uuids=None,
            )
        )
        fact_texts = {f["fact"] for f in facts}
        assert "Some CRM Owner owns Acme Reconciliation Test Co." in fact_texts
        assert "Some Email Sender flagged Acme Reconciliation Test Co as at risk." in fact_texts
    finally:
        repo.execute_cypher(
            "MATCH (n:Entity) WHERE n.group_id IN $groups DETACH DELETE n",
            {"groups": [group_a, group_b]},
        )


def test_two_distinct_entities_still_resolve_a_connection_path(repo):
    # Regression check: reconciliation must not confuse "one name matched
    # across two connectors" with "two differently-named entities" -- a
    # genuine two-entity connection query should still work.
    group_id = f"test_reconcile_path_{uuid.uuid4().hex[:8]}"
    try:
        node_x = _make_node(repo, group_id, "Reconciliation Path Widgets Inc")
        node_y = _make_node(repo, group_id, "Reconciliation Path Gadgets Inc")
        _make_edge(repo, node_x, node_y, "Reconciliation Path Widgets Inc supplies Reconciliation Path Gadgets Inc.")

        facts = asyncio.run(
            repo.search_graphiti_facts(
                "How is Reconciliation Path Widgets Inc connected to Reconciliation Path Gadgets Inc?",
                group_ids=[group_id],
                visible_uuids=None,
            )
        )
        assert any("supplies" in f["fact"] for f in facts)
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


# --- _normalize_entity_name (pure function, no Neo4j needed) ---------------


def test_normalize_strips_a_trailing_legal_suffix():
    assert _normalize_entity_name("Fenwick & Cole Legal, Inc.") == "fenwick and cole legal"


def test_normalize_treats_ampersand_and_and_the_same():
    assert _normalize_entity_name("Fenwick & Cole") == _normalize_entity_name("Fenwick and Cole")


def test_normalize_collapses_whitespace_and_case():
    assert _normalize_entity_name("  Acme   CORP  ") == _normalize_entity_name("acme corp")


def test_normalize_only_strips_a_trailing_suffix_not_one_mid_name():
    # "Co" inside "Coca Cola Co" shouldn't be stripped from the middle --
    # only a trailing legal-suffix word changes what's compared.
    assert _normalize_entity_name("Coca Cola Co") == "coca cola"
    assert "coca" in _normalize_entity_name("Coca Cola Co")


# --- Normalized reconciliation across connectors (needs real Neo4j) --------


def test_legal_suffix_variant_reconciles_across_groups(repo):
    # The exact same real-world entity, named with a legal suffix in one
    # connector's data and without it in another -- e.g. a CRM export vs. a
    # hand-written document -- should still pool as one entity.
    group_a = f"test_reconcile_suffix_a_{uuid.uuid4().hex[:8]}"
    group_b = f"test_reconcile_suffix_b_{uuid.uuid4().hex[:8]}"
    try:
        entity_a = _make_node(repo, group_a, "Fenwick & Cole Legal, Inc.")
        crm_owner = _make_node(repo, group_a, "Some CRM Rep")
        _make_edge(repo, crm_owner, entity_a, "Some CRM Rep owns the Fenwick & Cole Legal, Inc. account.")

        entity_b = _make_node(repo, group_b, "Fenwick and Cole Legal")
        email_sender = _make_node(repo, group_b, "Some Email Contact")
        _make_edge(repo, email_sender, entity_b, "Some Email Contact emailed Fenwick and Cole Legal about renewal.")

        facts = asyncio.run(
            repo.search_graphiti_facts(
                "What is going on with Fenwick and Cole Legal?",
                group_ids=[group_a, group_b],
                visible_uuids=None,
            )
        )
        fact_texts = {f["fact"] for f in facts}
        assert "Some CRM Rep owns the Fenwick & Cole Legal, Inc. account." in fact_texts
        assert "Some Email Contact emailed Fenwick and Cole Legal about renewal." in fact_texts
    finally:
        repo.execute_cypher(
            "MATCH (n:Entity) WHERE n.group_id IN $groups DETACH DELETE n",
            {"groups": [group_a, group_b]},
        )


def test_normalization_does_not_merge_genuinely_different_entities(repo):
    group_id = f"test_reconcile_no_false_merge_{uuid.uuid4().hex[:8]}"
    try:
        _make_node(repo, group_id, "Rhodes Furniture Inc")
        _make_node(repo, group_id, "Rhodes Furnishings Inc")

        rows = repo._match_entities_by_name("Rhodes Furniture", [group_id])
        names = {r["name"] for r in rows}
        assert "Rhodes Furnishings Inc" not in names
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_partial_name_match_does_not_merge_across_groups(repo):
    # A CONTAINS-only (non-exact) match must stay single-row, even across
    # multiple candidate group_ids -- merging on a loose partial match would
    # risk conflating two genuinely different entities.
    group_a = f"test_reconcile_partial_a_{uuid.uuid4().hex[:8]}"
    group_b = f"test_reconcile_partial_b_{uuid.uuid4().hex[:8]}"
    try:
        _make_node(repo, group_a, "Reconciliation Partial Match Holdings LLC")
        _make_node(repo, group_b, "A Totally Different Reconciliation Partial Entity")

        rows = repo._match_entities_by_name("Reconciliation Partial Match Holdings", [group_a, group_b])
        assert len(rows) == 1
        assert rows[0]["name"] == "Reconciliation Partial Match Holdings LLC"
    finally:
        repo.execute_cypher(
            "MATCH (n:Entity) WHERE n.group_id IN $groups DETACH DELETE n",
            {"groups": [group_a, group_b]},
        )
