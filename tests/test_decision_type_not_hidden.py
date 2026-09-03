# Regression coverage for a real bug found by testing against real ingested
# data: ontology/core.yaml's `Decision` entity type is a legitimate general-
# purpose business entity ("a decision, approval, rejection, or
# recommendation"): a real client dataset can and does contain its own
# genuine Decision records (a CSV literally named "decisions.csv" auto-infers
# that exact type name via app/ingestion/database_source.py's _infer_spec).
# Every "hide Saxon's own generated recommendation audit trail" exclusion in
# this codebase used to filter on the ontology label `:Decision` directly --
# which silently hid a client's own real Decision entities too, permanently,
# system-wide. Fixed by tagging only Saxon's own generated nodes with a
# second, narrower label (:SaxonRecommendation: see app/graph/decisions.py)
# and filtering on that instead everywhere. This file proves the actual fix:
# a plain :Entity:Decision node with NO :SaxonRecommendation label (what a
# real client's own decisions.csv row looks like once ingested) is fully
# retrievable, unlike before.
#
# Needs a real, reachable Neo4j: same pattern as test_decision_isolation.py.
import asyncio
import uuid

from unittest.mock import Mock

from app.graph.decisions import ensure_decision_indexes
from app.graph.entity_resolution import match_entities_by_name
from app.graph.graph_repository import GraphRepository


def _fake_repo():
    return GraphRepository(graphiti_instance=Mock())


def _real_business_decision(repo, group_id, decision_id, approver, amount):
    """A node shaped exactly like what _infer_spec + Graphiti extraction
    would produce from a real decisions.csv row: :Entity:Decision, no
    :SaxonRecommendation label, a real decision_id/approver/amount."""
    node_uuid = str(uuid.uuid4())
    repo.execute_cypher(
        "CREATE (n:Entity:Decision {uuid: $uuid, group_id: $group_id, name: $name, "
        "decision_status: 'Approved'})",
        {"uuid": node_uuid, "group_id": group_id, "name": decision_id},
    )
    return node_uuid


def test_a_real_business_decision_entity_resolves_by_name(monkeypatch=None):
    repo = _fake_repo()
    group_id = f"test_real_decision_{uuid.uuid4().hex[:8]}"
    try:
        decision_uuid = _real_business_decision(repo, group_id, "DEC-2026-014", "Daniel Reyes", 38400)

        rows = match_entities_by_name(repo.execute_cypher, "DEC-2026-014", [group_id])

        assert len(rows) == 1
        assert rows[0]["uuid"] == decision_uuid
        assert rows[0]["name"] == "DEC-2026-014"
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_a_real_business_decisions_own_facts_are_retrievable():
    repo = _fake_repo()
    group_id = f"test_real_decision_facts_{uuid.uuid4().hex[:8]}"
    try:
        decision_uuid = _real_business_decision(repo, group_id, "DEC-2026-099", "Grace Okafor", 12000)
        other_uuid = str(uuid.uuid4())
        repo.execute_cypher(
            "CREATE (n:Entity {uuid: $uuid, group_id: $group_id, name: 'Related Order'})",
            {"uuid": other_uuid, "group_id": group_id},
        )
        repo.execute_cypher(
            "MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
            "CREATE (a)-[:RELATES_TO {name: 'RELATED_TO', fact: 'DEC-2026-099 approved the budget for Related Order.', "
            "group_id: $group_id, valid_at: datetime('2026-01-01T00:00:00Z'), invalid_at: null, expired_at: null}]->(b)",
            {"a": decision_uuid, "b": other_uuid, "group_id": group_id},
        )

        facts = repo.direct_facts_for(decision_uuid, None)

        assert len(facts) == 1
        assert facts[0]["fact"] == "DEC-2026-099 approved the budget for Related Order."
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_a_stale_pre_fix_saxon_decision_gets_backfilled_and_stays_hidden():
    # Real bug found live, right after deploying the label fix: a
    # :Decision node this app itself generated *before* the
    # :SaxonRecommendation label existed had no way to get the new label
    # retroactively, so it was indistinguishable from a real client
    # Decision entity and got resolved as if it were one, on a repeat of
    # the exact query that had generated it. ensure_decision_indexes' new
    # backfill step (app/graph/decisions.py) is what's supposed to fix
    # this on every startup, keyed on source_system rather than the label
    # itself (which is exactly what's missing on a stale node).
    repo = _fake_repo()
    group_id = f"test_stale_decision_{uuid.uuid4().hex[:8]}"
    try:
        # Shaped exactly like a Decision record_decision() created BEFORE
        # the :SaxonRecommendation label was added: :Entity:Decision,
        # source_system set (that was never gated on the label), no
        # :SaxonRecommendation.
        stale_uuid = str(uuid.uuid4())
        repo.execute_cypher(
            "CREATE (d:Entity:Decision {uuid: $uuid, group_id: $group_id, "
            "name: 'Recommendation for: Why is Order SO-1 at risk?', "
            "source_system: 'saxon.causal_engine'})",
            {"uuid": stale_uuid, "group_id": group_id},
        )

        rows_before = match_entities_by_name(repo.execute_cypher, "SO-1", [group_id])
        assert any(r["uuid"] == stale_uuid for r in rows_before), "test setup: must reproduce the bug first"

        ensure_decision_indexes(repo=repo)

        rows_after = match_entities_by_name(repo.execute_cypher, "SO-1", [group_id])
        assert not any(r["uuid"] == stale_uuid for r in rows_after)

        labels = repo.execute_cypher("MATCH (d {uuid: $uuid}) RETURN labels(d) AS labels", {"uuid": stale_uuid})[0]
        assert "SaxonRecommendation" in labels["labels"]
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_backfill_never_tags_a_real_business_decision_with_no_source_system():
    repo = _fake_repo()
    group_id = f"test_real_decision_untouched_{uuid.uuid4().hex[:8]}"
    try:
        real_uuid = _real_business_decision(repo, group_id, "DEC-2026-555", "Renee Kapoor", 9000)
        ensure_decision_indexes(repo=repo)
        labels = repo.execute_cypher("MATCH (d {uuid: $uuid}) RETURN labels(d) AS labels", {"uuid": real_uuid})[0]
        assert "SaxonRecommendation" not in labels["labels"]
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})


def test_a_real_business_decision_is_reachable_via_causal_chain():
    repo = _fake_repo()
    group_id = f"test_real_decision_causal_{uuid.uuid4().hex[:8]}"
    try:
        decision_uuid = _real_business_decision(repo, group_id, "DEC-2026-777", "Owen Whitfield", 5000)
        anchor, _second, facts = asyncio.run(
            repo.causal_chain_for_query("What is DEC-2026-777?", [group_id], None)
        )
        assert anchor is not None
        assert anchor["uuid"] == decision_uuid
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})
