# Needs a real, reachable Neo4j: same caveat as test_entity_reconciliation.py.
# Creates and cleans up its own throwaway :Entity/:SAME_AS/:ProposedMerge
# data under randomly-suffixed group_ids/tenant_ids.
#
# Covers the Reconcile stage (app/graph/reconciliation.py): unlike Resolve's
# live, query-time name matching (entity_resolution.py), this runs once
# (per connector sync in production, called directly here) and persists its
# decision: a confident cross-connector name match becomes a :SAME_AS
# edge, an unconfident-but-similar one becomes a :ProposedMerge a human has
# to approve or reject.
import uuid

import pytest

from app.graph.graph_repository import GraphRepository
from app.graph.reconciliation import (
    approve_proposal,
    ensure_reconciliation_indexes,
    expand_same_as,
    list_proposals,
    reconcile_tenant,
    reject_proposal,
)


@pytest.fixture
def repo():
    from unittest.mock import Mock
    repo = GraphRepository(graphiti_instance=Mock())
    # Idempotent: the concurrent-reconcile test below specifically depends
    # on the pair_key uniqueness constraint this creates (see
    # _create_proposal's docstring).
    ensure_reconciliation_indexes(repo.execute_cypher)
    return repo


def _node(repo, group_id, name):
    node_uuid = str(uuid.uuid4())
    repo.execute_cypher(
        "CREATE (n:Entity {uuid: $uuid, group_id: $group_id, name: $name, summary: $summary})",
        {"uuid": node_uuid, "group_id": group_id, "name": name, "summary": f"{name} summary"},
    )
    return node_uuid


def test_exact_cross_group_match_creates_a_same_as_edge(repo):
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        a = _node(repo, group_a, "Reconcile Widgets Inc")
        b = _node(repo, group_b, "Reconcile Widgets Inc")

        result = reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])
        assert result["same_as_created"] == 1
        assert result["proposals_created"] == 0

        rows = repo.execute_cypher(
            "MATCH (x:Entity {uuid: $a})-[r:SAME_AS]-(y:Entity {uuid: $b}) RETURN r.confidence AS confidence",
            {"a": a, "b": b},
        )
        assert rows[0]["confidence"] == "exact"
    finally:
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})


def test_legal_suffix_variant_creates_a_normalized_same_as_edge(repo):
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        a = _node(repo, group_a, "Reconcile Gadgets LLC")
        b = _node(repo, group_b, "Reconcile Gadgets")

        reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])

        rows = repo.execute_cypher(
            "MATCH (x:Entity {uuid: $a})-[r:SAME_AS]-(y:Entity {uuid: $b}) RETURN r.confidence AS confidence",
            {"a": a, "b": b},
        )
        assert rows[0]["confidence"] == "normalized"
    finally:
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})


def test_close_but_not_normalized_equal_names_create_a_proposal_not_an_edge(repo):
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        a = _node(repo, group_a, "Reconcile Fuzzico Trading Co")
        b = _node(repo, group_b, "Reconcile Fuzzico Tradnig Co")  # typo

        result = reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])
        assert result["same_as_created"] == 0
        assert result["proposals_created"] == 1

        same_as_rows = repo.execute_cypher(
            "MATCH (x:Entity {uuid: $a})-[r:SAME_AS]-(y:Entity {uuid: $b}) RETURN r", {"a": a, "b": b}
        )
        assert same_as_rows == []

        proposals = list_proposals(repo.execute_cypher, tenant_id)
        assert len(proposals) == 1
        assert proposals[0]["status"] == "pending"
        assert {proposals[0]["entity_a_uuid"], proposals[0]["entity_b_uuid"]} == {a, b}
    finally:
        repo.execute_cypher(
            "MATCH (p:ProposedMerge {tenant_id: $t}) DETACH DELETE p", {"t": tenant_id}
        )
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})


def test_genuinely_different_names_create_nothing(repo):
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        _node(repo, group_a, "Reconcile Alpha Holdings")
        _node(repo, group_b, "Reconcile Zebra Logistics")

        result = reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])
        assert result == {"same_as_created": 0, "proposals_created": 0}
    finally:
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})


def test_same_group_id_duplicates_are_not_reconciled(repo):
    # Reconcile is a cross-CONNECTOR stage: two same-named nodes already in
    # the SAME group_id are either a real Graphiti extraction duplicate
    # (that's Graphiti's own dedup concern, see CLAUDE.md) or the same node,
    # not something this stage should be linking.
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    try:
        _node(repo, group_a, "Reconcile Same Group Inc")
        _node(repo, group_a, "Reconcile Same Group Inc")

        result = reconcile_tenant(repo.execute_cypher, tenant_id, [group_a])
        assert result == {"same_as_created": 0, "proposals_created": 0}
    finally:
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id = $g DETACH DELETE n", {"g": group_a})


def test_running_reconcile_twice_does_not_duplicate_edges_or_proposals(repo):
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        _node(repo, group_a, "Reconcile Idempotent Inc")
        _node(repo, group_b, "Reconcile Idempotent Inc")
        _node(repo, group_a, "Reconcile Fuzztown Trading Co")
        _node(repo, group_b, "Reconcile Fuzztown Tradnig Co")

        first = reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])
        second = reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])

        assert first["same_as_created"] == 1
        assert first["proposals_created"] == 1
        assert second == {"same_as_created": 0, "proposals_created": 0}

        same_as_count = repo.execute_cypher(
            "MATCH (:Entity {group_id: $a})-[r:SAME_AS]-(:Entity {group_id: $b}) RETURN count(r) AS c",
            {"a": group_a, "b": group_b},
        )[0]["c"]
        assert same_as_count == 1
        assert len(list_proposals(repo.execute_cypher, tenant_id)) == 1
    finally:
        repo.execute_cypher("MATCH (p:ProposedMerge {tenant_id: $t}) DETACH DELETE p", {"t": tenant_id})
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})


def test_approve_proposal_creates_same_as_and_marks_approved(repo):
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        a = _node(repo, group_a, "Reconcile Approve Trading Co")
        b = _node(repo, group_b, "Reconcile Approve Tradnig Co")
        reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])
        proposal_id = list_proposals(repo.execute_cypher, tenant_id)[0]["id"]

        assert approve_proposal(repo.execute_cypher, tenant_id, proposal_id) is True

        rows = repo.execute_cypher(
            "MATCH (x:Entity {uuid: $a})-[r:SAME_AS]-(y:Entity {uuid: $b}) RETURN r.confidence AS confidence",
            {"a": a, "b": b},
        )
        assert rows[0]["confidence"] == "fuzzy_approved"
        assert list_proposals(repo.execute_cypher, tenant_id, status="pending") == []
        # Already decided: approving (or rejecting) again is a no-op, not a
        # second edge.
        assert approve_proposal(repo.execute_cypher, tenant_id, proposal_id) is False
    finally:
        repo.execute_cypher("MATCH (p:ProposedMerge {tenant_id: $t}) DETACH DELETE p", {"t": tenant_id})
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})


def test_approve_proposal_fails_cleanly_if_an_entity_was_deleted_first(repo):
    # A real bug found in review: approve_proposal used to SET status =
    # 'approved' and only THEN try to create the :SAME_AS edge as a second,
    # separate query: if either entity had since been deleted (its
    # connector removed, its data re-synced away), that second query
    # silently matched nothing and created no edge, while the proposal was
    # already marked 'approved'. Now both are one statement: either the
    # whole thing commits together, or the proposal is untouched and stays
    # 'pending' so approving it again (once the data's back) can still work.
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        a = _node(repo, group_a, "Reconcile Deleted Trading Co")
        b = _node(repo, group_b, "Reconcile Deleted Tradnig Co")
        reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])
        proposal_id = list_proposals(repo.execute_cypher, tenant_id)[0]["id"]

        repo.execute_cypher("MATCH (n:Entity {uuid: $uuid}) DETACH DELETE n", {"uuid": a})

        assert approve_proposal(repo.execute_cypher, tenant_id, proposal_id) is False
        pending = list_proposals(repo.execute_cypher, tenant_id, status="pending")
        assert len(pending) == 1 and pending[0]["id"] == proposal_id

        same_as_rows = repo.execute_cypher(
            "MATCH (:Entity {uuid: $b})-[r:SAME_AS]-() RETURN r", {"b": b}
        )
        assert same_as_rows == []
    finally:
        repo.execute_cypher("MATCH (p:ProposedMerge {tenant_id: $t}) DETACH DELETE p", {"t": tenant_id})
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})


def test_concurrent_reconcile_runs_never_duplicate_a_same_as_edge_or_proposal(repo):
    # The race this session's review flagged: two different connectors of
    # the SAME tenant syncing close together each run reconcile_tenant
    # independently: the in-memory already_paired snapshot each call
    # starts with can't see what the OTHER concurrent call is doing, so the
    # real guard has to be at the database level (MERGE, not CREATE): see
    # _create_same_as's and _create_proposal's docstrings. Real threads
    # against the real driver, not sequential calls that only look
    # concurrent.
    import threading
    from app.graph.neo4j_client import Neo4jClient

    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        _node(repo, group_a, "Reconcile Race Exact Co")
        _node(repo, group_b, "Reconcile Race Exact Co")
        _node(repo, group_a, "Reconcile Race Fuzzy Trading Co")
        _node(repo, group_b, "Reconcile Race Fuzzy Tradnig Co")

        def run_once():
            thread_repo = GraphRepository(neo4j_client=Neo4jClient())
            reconcile_tenant(thread_repo.execute_cypher, tenant_id, [group_a, group_b])

        threads = [threading.Thread(target=run_once) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        same_as_count = repo.execute_cypher(
            "MATCH (:Entity {group_id: $a})-[r:SAME_AS]-(:Entity {group_id: $b}) RETURN count(r) AS c",
            {"a": group_a, "b": group_b},
        )[0]["c"]
        assert same_as_count == 1
        assert len(list_proposals(repo.execute_cypher, tenant_id)) == 1
    finally:
        repo.execute_cypher("MATCH (p:ProposedMerge {tenant_id: $t}) DETACH DELETE p", {"t": tenant_id})
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})


def test_reject_proposal_never_creates_a_same_as_edge(repo):
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        a = _node(repo, group_a, "Reconcile Reject Trading Co")
        b = _node(repo, group_b, "Reconcile Reject Tradnig Co")
        reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])
        proposal_id = list_proposals(repo.execute_cypher, tenant_id)[0]["id"]

        assert reject_proposal(repo.execute_cypher, tenant_id, proposal_id) is True

        rows = repo.execute_cypher(
            "MATCH (x:Entity {uuid: $a})-[r:SAME_AS]-(y:Entity {uuid: $b}) RETURN r", {"a": a, "b": b}
        )
        assert rows == []
        assert list_proposals(repo.execute_cypher, tenant_id, status="rejected")[0]["id"] == proposal_id
    finally:
        repo.execute_cypher("MATCH (p:ProposedMerge {tenant_id: $t}) DETACH DELETE p", {"t": tenant_id})
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})


def test_expand_same_as_pulls_in_the_linked_node(repo):
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        a = _node(repo, group_a, "Reconcile Expand Trading Co")
        b = _node(repo, group_b, "Reconcile Expand Tradnig Co")
        reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])
        proposal_id = list_proposals(repo.execute_cypher, tenant_id)[0]["id"]
        approve_proposal(repo.execute_cypher, tenant_id, proposal_id)

        rows = repo._match_entities_by_name("Reconcile Expand Trading Co", [group_a, group_b])
        uuids = {r["uuid"] for r in rows}
        assert a in uuids
        assert b in uuids
    finally:
        repo.execute_cypher("MATCH (p:ProposedMerge {tenant_id: $t}) DETACH DELETE p", {"t": tenant_id})
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})


def test_expand_same_as_does_not_leak_a_node_outside_the_allowed_group_ids(repo):
    # The security-relevant case: SAME_AS links across a whole tenant, but a
    # single query can be scoped narrower (a document set, or
    # authorization.visible_uuids): expand_same_as must never hand back a
    # node outside what THIS caller asked to see, even if it's genuinely
    # linked in the graph.
    tenant_id = f"test_reconcile_tenant_{uuid.uuid4().hex[:8]}"
    group_a = f"{tenant_id}_a"
    group_b = f"{tenant_id}_b"
    try:
        a = _node(repo, group_a, "Reconcile Scope Trading Co")
        b = _node(repo, group_b, "Reconcile Scope Tradnig Co")
        reconcile_tenant(repo.execute_cypher, tenant_id, [group_a, group_b])
        proposal_id = list_proposals(repo.execute_cypher, tenant_id)[0]["id"]
        approve_proposal(repo.execute_cypher, tenant_id, proposal_id)

        rows = [{"uuid": a, "name": "Reconcile Scope Trading Co", "summary": "", "group_id": group_a}]
        expanded = expand_same_as(repo.execute_cypher, rows, allowed_group_ids=[group_a])
        assert {r["uuid"] for r in expanded} == {a}

        expanded_both = expand_same_as(repo.execute_cypher, rows, allowed_group_ids=[group_a, group_b])
        assert {r["uuid"] for r in expanded_both} == {a, b}
    finally:
        repo.execute_cypher("MATCH (p:ProposedMerge {tenant_id: $t}) DETACH DELETE p", {"t": tenant_id})
        repo.execute_cypher("MATCH (n:Entity) WHERE n.group_id IN $g DETACH DELETE n", {"g": [group_a, group_b]})
