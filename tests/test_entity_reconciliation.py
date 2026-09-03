# Needs a real, reachable Neo4j: same caveat as test_graph.py. Creates and
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
from unittest.mock import AsyncMock, Mock

import pytest

from app.graph.entity_resolution import _normalize_entity_name
from app.graph.graph_repository import GraphRepository


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
    # across two connectors" with "two differently-named entities": a
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
    # connector's data and without it in another (e.g. a CRM export vs. a
    # hand-written document) should still pool as one entity.
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


# --- Lowercase/casual entity names still resolve precisely instead of
# falling through to unconstrained semantic search: found for real in
# production: "what do we know about diego" pulled in several unrelated
# orders/shipments/quality events as "context" alongside the two real Diego
# Alvarez facts, while the identical question typed as "Diego Alvarez"
# resolved cleanly. See _extract_lowercase_word_candidates's docstring.


def test_lowercase_query_still_resolves_precisely_not_via_semantic_search(repo):
    group_id = f"test_reconcile_lowercase_{uuid.uuid4().hex[:8]}"
    try:
        anchor = _make_node(repo, group_id, "Diego Reconciliation Alvarez")
        other = _make_node(repo, group_id, "Brightpeak Reconciliation Automation")
        _make_edge(repo, anchor, other, "Diego Reconciliation Alvarez is the account manager for Brightpeak Reconciliation Automation.")
        # An unrelated entity in the same group_id: must NOT show up in the
        # answer just because a broad semantic search would have padded it
        # in (the exact behavior this fix replaces).
        unrelated_a = _make_node(repo, group_id, "Reconciliation Order SO-1")
        unrelated_b = _make_node(repo, group_id, "Reconciliation Plant 1")
        _make_edge(repo, unrelated_a, unrelated_b, "Reconciliation Order SO-1 is being produced at Reconciliation Plant 1.")

        facts = asyncio.run(
            repo.search_graphiti_facts(
                "what do we know about diego reconciliation alvarez",
                group_ids=[group_id],
                visible_uuids=None,
            )
        )
        fact_texts = {f["fact"] for f in facts}
        assert "Diego Reconciliation Alvarez is the account manager for Brightpeak Reconciliation Automation." in fact_texts
        assert not any(f.get("kind") == "semantic_search" for f in facts)
        assert "Reconciliation Order SO-1 is being produced at Reconciliation Plant 1." not in fact_texts
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_possessive_query_still_resolves_the_real_entity(repo):
    # Real bug found by testing against real ingested data: "Ferrotek's"
    # (a natural possessive reference in a question) failed to resolve to
    # "Ferrotek Components" at all, and because a sentence-initial
    # auxiliary word ("Has") glued onto it into one spurious two-word
    # proper-noun candidate ("Has Ferrotek's"), the failure hard-short-
    # circuited the whole query into a false "not found" rather than
    # falling through to search. Fixed in both _extract_candidate_entities
    # (strips a leading auxiliary/stopword) and match_entities_by_name/
    # _normalize_entity_name (strips the possessive itself).
    group_id = f"test_reconcile_possessive_{uuid.uuid4().hex[:8]}"
    try:
        anchor = _make_node(repo, group_id, "Ferrotek Reconciliation Components")
        other = _make_node(repo, group_id, "Reconciliation CX-17 Power Relay")
        _make_edge(
            repo, anchor, other,
            "Ferrotek Reconciliation Components produces Reconciliation CX-17 Power Relay.",
        )

        facts = asyncio.run(
            repo.search_graphiti_facts(
                "Has Ferrotek Reconciliation Components's certification been restored?",
                group_ids=[group_id],
                visible_uuids=None,
            )
        )
        fact_texts = {f["fact"] for f in facts}
        assert "Ferrotek Reconciliation Components produces Reconciliation CX-17 Power Relay." in fact_texts
        # Resolved via real entity resolution, not padded-out semantic search.
        assert not any(f.get("kind") == "semantic_search" for f in facts)
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


# --- Lowercase-word fallback must not hijack onto an action-log entity -----
# Real bug found live: "Who approved the expedited fix, and what did it
# cost?" has no proper-noun/id candidate at all, so every remaining word
# ("approved", "expedited", "fix", "cost") went through the lenient
# lowercase-word fallback, and "expedited" happened to CONTAINS-match a
# :Task node Graphiti had auto-named "expedited qualification lot" (from an
# activity-log row), silently anchoring the whole causal walk on an
# unrelated part of the story instead of falling through to search (which
# could have found the real approval facts). A generic English word
# coincidentally appearing inside an auto-generated, sentence-shaped
# Task/Event/Activity/... name is expected and common; it was never a
# reliable signal the query is actually about that logged action.


def _make_task_node(repo, group_id, name):
    node_uuid = str(uuid.uuid4())
    repo.execute_cypher(
        "CREATE (n:Entity:Task {uuid: $uuid, group_id: $group_id, name: $name, summary: $summary})",
        {"uuid": node_uuid, "group_id": group_id, "name": name, "summary": f"{name} summary"},
    )
    return node_uuid


def test_lowercase_word_does_not_hijack_onto_an_action_log_task_node(repo):
    group_id = f"test_reconcile_action_hijack_{uuid.uuid4().hex[:8]}"
    repo.graphiti.search = AsyncMock(return_value=[])
    try:
        real_entity = _make_node(repo, group_id, "Reconciliation Decision DEC-9001")
        task_node = _make_task_node(repo, group_id, "expedited reconciliation shipment task")
        _make_edge(repo, real_entity, task_node, "Reconciliation Decision DEC-9001 approved a $9,000 expedited reconciliation shipment task.")

        facts = asyncio.run(
            repo.search_graphiti_facts(
                "who approved the expedited fix, and what did it cost",
                group_ids=[group_id],
                visible_uuids=None,
            )
        )
        # The Task node's own name must NOT have grounded this query: with
        # the bug, its own edge fact (above) would come back as if it were
        # the answer. Fixed, nothing resolves via name matching and this
        # falls through to (stubbed) semantic search instead.
        assert facts == []
        repo.graphiti.search.assert_awaited_once()
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_lowercase_word_fallback_still_matches_a_real_non_task_entity(repo):
    # Regression guard: the existing "diego"-style lowercase fallback (a
    # casually-typed person/company name) must keep working: this fix only
    # narrows matching for Task/Event/Activity/... typed nodes, nothing else.
    group_id = f"test_reconcile_lowercase_still_works_{uuid.uuid4().hex[:8]}"
    try:
        anchor = _make_node(repo, group_id, "Expedited Reconciliation Logistics")
        other = _make_node(repo, group_id, "Reconciliation Freight Partners")
        _make_edge(repo, anchor, other, "Expedited Reconciliation Logistics partners with Reconciliation Freight Partners.")

        facts = asyncio.run(
            repo.search_graphiti_facts(
                "what do we know about expedited reconciliation logistics",
                group_ids=[group_id],
                visible_uuids=None,
            )
        )
        fact_texts = {f["fact"] for f in facts}
        assert "Expedited Reconciliation Logistics partners with Reconciliation Freight Partners." in fact_texts
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_proper_noun_candidate_can_still_match_a_task_node(repo):
    # The restriction only applies to the lenient lowercase-word fallback --
    # a real, specific multi-word proper-noun candidate must still be able
    # to resolve to a Task/Event-typed node (e.g. a query literally naming
    # an activity-log entry by its own generated name).
    group_id = f"test_reconcile_proper_noun_task_{uuid.uuid4().hex[:8]}"
    try:
        task_node = _make_task_node(repo, group_id, "Reconciliation Quarterly Compliance Audit")
        other = _make_node(repo, group_id, "Reconciliation Compliance Team")
        _make_edge(repo, task_node, other, "Reconciliation Quarterly Compliance Audit was performed by Reconciliation Compliance Team.")

        facts = asyncio.run(
            repo.search_graphiti_facts(
                "What happened with the Reconciliation Quarterly Compliance Audit?",
                group_ids=[group_id],
                visible_uuids=None,
            )
        )
        fact_texts = {f["fact"] for f in facts}
        assert "Reconciliation Quarterly Compliance Audit was performed by Reconciliation Compliance Team." in fact_texts
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_generic_lowercase_query_with_no_real_entity_still_falls_through_to_search(repo):
    # No proper noun, no matching lowercase word either: this must NOT
    # short-circuit to a false "not found" (the lenient candidates never set
    # saw_unresolved), it should just fall through to normal search like any
    # other open-ended question. graphiti.search itself is stubbed (this repo
    # fixture's graphiti_instance is a plain Mock, not a real Graphiti/LLM
    # client) purely to prove that code path was actually reached, not to
    # test search's own behavior.
    group_id = f"test_reconcile_generic_{uuid.uuid4().hex[:8]}"
    repo.graphiti.search = AsyncMock(return_value=[])
    try:
        facts = asyncio.run(
            repo.search_graphiti_facts(
                "what has changed recently",
                group_ids=[group_id],
                visible_uuids=None,
            )
        )
        assert facts == []
        repo.graphiti.search.assert_awaited_once()
    finally:
        repo.execute_cypher("MATCH (n:Entity {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_partial_name_match_does_not_merge_across_groups(repo):
    # A CONTAINS-only (non-exact) match must stay single-row, even across
    # multiple candidate group_ids: merging on a loose partial match would
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
