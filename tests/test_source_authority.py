# Covers app/graph/connectors.py's source_authority field/authority_by_group_id
# (real Neo4j) and ContextOrchestrator._apply_authority_tie_break (pure-logic,
# stubbed retriever -- same convention as test_orchestrator_observability.py).
# See CLAUDE.md's pivot notes: source_authority only ever breaks a tie between
# two connectors' disagreeing facts, it never hides or filters anything.
import asyncio
import uuid

import pytest

from app.context.orchestrator import ContextOrchestrator
from app.graph import connectors
from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    return GraphRepository()


def test_create_connector_stores_source_authority(repo):
    tenant_id = f"test_authority_tenant_{uuid.uuid4().hex[:8]}"
    try:
        created = connectors.create_connector(
            tenant_id, "ERP Feed", "web", "kb1", "https://erp.example.com", repo=repo, source_authority=10
        )
        assert created["source_authority"] == 10
        fetched = connectors.get_connector(tenant_id, created["id"], repo=repo)
        assert fetched["source_authority"] == 10
    finally:
        repo.execute_cypher("MATCH (c:Connector {tenant_id: $t}) DETACH DELETE c", {"t": tenant_id})


def test_create_connector_defaults_source_authority_to_zero(repo):
    tenant_id = f"test_authority_default_{uuid.uuid4().hex[:8]}"
    try:
        created = connectors.create_connector(tenant_id, "Some Doc Store", "web", "kb1", "https://example.com", repo=repo)
        assert created["source_authority"] == 0
    finally:
        repo.execute_cypher("MATCH (c:Connector {tenant_id: $t}) DETACH DELETE c", {"t": tenant_id})


def test_authority_by_group_id_takes_the_max_across_connectors_in_the_same_group(repo):
    tenant_id = f"test_authority_max_{uuid.uuid4().hex[:8]}"
    try:
        connectors.create_connector(tenant_id, "Low", "web", "kb1", "https://a.example.com", repo=repo, source_authority=2)
        connectors.create_connector(tenant_id, "High", "web", "kb1", "https://b.example.com", repo=repo, source_authority=9)
        connectors.create_connector(tenant_id, "Other group", "web", "kb2", "https://c.example.com", repo=repo, source_authority=5)

        authority = connectors.authority_by_group_id(tenant_id, repo=repo)
        assert authority["kb1"] == 9
        assert authority["kb2"] == 5
    finally:
        repo.execute_cypher("MATCH (c:Connector {tenant_id: $t}) DETACH DELETE c", {"t": tenant_id})


class _StubRetriever:
    def __init__(self, facts):
        self._facts = facts

    async def retrieve(self, query, group_ids=None, visible_uuids=None, num_results=8):
        return self._facts


def _orchestrator_with_facts(facts):
    orchestrator = ContextOrchestrator(graphiti_instance=object())
    orchestrator.retrievers = [_StubRetriever(facts)]
    return orchestrator


def _fact(text, group_id, **overrides):
    base = {
        "fact": text,
        "source_node_uuid": "s1",
        "target_node_uuid": "t1",
        "valid_at": None,
        "invalid_at": None,
        "expired_at": None,
        "group_id": group_id,
        "is_valid": True,
    }
    base.update(overrides)
    return base


def test_higher_authority_source_wins_a_disagreement(repo, monkeypatch):
    tenant_id = f"test_tiebreak_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(
        "app.graph.connectors.authority_by_group_id",
        lambda t, repo=None: {"erp_group": 10, "doc_group": 1} if t == tenant_id else {},
    )
    facts = [
        _fact("Order 9001 is shipped.", "doc_group"),
        _fact("Order 9001 is delayed.", "erp_group"),
    ]
    orchestrator = _orchestrator_with_facts(facts)
    packet = asyncio.run(orchestrator.get_context_packet("What is the status of Order 9001?", group_ids=["erp_group", "doc_group"], tenant_id=tenant_id))

    assert "Order 9001 is delayed." in packet.metadata["summary"] or packet.metadata["summary"] == "Order 9001 is delayed."
    # Both facts are still present in the returned evidence -- authority
    # only decides which feeds the answer, never hides the other source.
    fact_texts = {f["fact"] for f in packet.metadata["facts"]}
    assert "Order 9001 is shipped." in fact_texts
    assert "Order 9001 is delayed." in fact_texts
    winning = next(f for f in packet.metadata["facts"] if f["fact"] == "Order 9001 is delayed.")
    losing = next(f for f in packet.metadata["facts"] if f["fact"] == "Order 9001 is shipped.")
    assert winning["is_authoritative"] is True
    assert losing["is_authoritative"] is False


def test_no_tie_break_when_facts_agree(monkeypatch):
    monkeypatch.setattr("app.graph.connectors.authority_by_group_id", lambda t, repo=None: {"a": 1, "b": 9})
    facts = [
        _fact("Order 9002 is delayed.", "a"),
        _fact("Order 9002 is delayed.", "b"),
    ]
    orchestrator = _orchestrator_with_facts(facts)
    packet = asyncio.run(orchestrator.get_context_packet("Status of Order 9002?", group_ids=["a", "b"], tenant_id="t"))
    assert all(f["is_authoritative"] for f in packet.metadata["facts"])


def test_tie_break_skipped_without_tenant_id(monkeypatch):
    called = []
    monkeypatch.setattr("app.graph.connectors.authority_by_group_id", lambda t, repo=None: called.append(t) or {})
    facts = [_fact("Order 9003 is delayed.", "a")]
    orchestrator = _orchestrator_with_facts(facts)
    asyncio.run(orchestrator.get_context_packet("Status of Order 9003?", group_ids=["a"]))
    assert called == []


# --- Regression: two real, non-contradicting facts about the same node pair
# used to be wrongly treated as a "disagreement" and one silently dropped
# from the answer -- found against real northwind data ("Order 10252 was
# shipped to the city of Charleroi" / "...to the country of Belgium", both
# LOCATED_AT edges from the same connector). Neither is what source_authority
# is for: it ranks one connector's data over another's, so two facts from the
# *same* connector are never a disagreement to arbitrate, whatever their
# relationship type or node pair.


def test_same_connector_facts_about_the_same_pair_are_never_tie_broken(monkeypatch):
    monkeypatch.setattr("app.graph.connectors.authority_by_group_id", lambda t, repo=None: {"northwind": 0})
    facts = [
        _fact("Order 10252 was shipped to the city of Charleroi", "northwind", relationship_type="LOCATED_AT"),
        _fact("Order 10252 was shipped to the country of Belgium", "northwind", relationship_type="LOCATED_AT"),
    ]
    orchestrator = _orchestrator_with_facts(facts)
    packet = asyncio.run(
        orchestrator.get_context_packet("Where was Order 10252 shipped?", group_ids=["northwind"], tenant_id="t")
    )
    assert all(f["is_authoritative"] for f in packet.metadata["facts"])


def test_same_pair_different_relationship_type_is_never_tie_broken(monkeypatch):
    # Two genuinely different relationships between the same two nodes (e.g.
    # an Account MANAGES and is also ASSOCIATED_WITH the same Contact) --
    # even across two different connectors, this isn't a disagreement about
    # one fact, it's two different true facts, so relationship_type has to
    # be part of the grouping key alongside source/target.
    monkeypatch.setattr("app.graph.connectors.authority_by_group_id", lambda t, repo=None: {"a": 1, "b": 9})
    facts = [
        _fact("Jordan manages the Acme account.", "a", relationship_type="MANAGES"),
        _fact("Jordan is associated with the Acme account.", "b", relationship_type="ASSOCIATED_WITH"),
    ]
    orchestrator = _orchestrator_with_facts(facts)
    packet = asyncio.run(orchestrator.get_context_packet("Tell me about Jordan and Acme.", group_ids=["a", "b"], tenant_id="t"))
    assert all(f["is_authoritative"] for f in packet.metadata["facts"])


def test_multi_hop_path_facts_are_never_tie_broken(monkeypatch):
    # The "how is X connected to Y" path-lookup branch (see
    # GraphRepository._relationship_path_facts / search_graphiti_facts)
    # returns every hop with source_node_uuid/target_node_uuid both "" --
    # without excluding blank-uuid facts from grouping, every hop of a
    # multi-hop path collides on that shared blank key and looks like one
    # giant disagreement, collapsing a real multi-fact connection down to
    # a single hop in the answer.
    monkeypatch.setattr("app.graph.connectors.authority_by_group_id", lambda t, repo=None: {"a": 1, "b": 9})
    facts = [
        _fact("Order 10248 was placed by VINET.", "a", source_node_uuid="", target_node_uuid=""),
        _fact("VINET is based in Reims.", "b", source_node_uuid="", target_node_uuid=""),
    ]
    orchestrator = _orchestrator_with_facts(facts)
    packet = asyncio.run(orchestrator.get_context_packet("How is Order 10248 connected to Reims?", group_ids=["a", "b"], tenant_id="t"))
    assert all(f["is_authoritative"] for f in packet.metadata["facts"])
