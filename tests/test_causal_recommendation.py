# Pure-logic tests for ContextOrchestrator.get_causal_context_packet -- a
# fake GraphRepository (swapped onto orchestrator._repo) and a fake Graphiti
# llm_client stand in for real Neo4j/LLM calls, same "stub the collaborator"
# convention as test_orchestrator_observability.py.
#
# Covers the two things the causal-reasoning pivot explicitly required (see
# CLAUDE.md): the recommendation lives in its own metadata.recommendation
# field, never blended into metadata.summary; and a produced recommendation
# is logged as an auditable :Decision node (app/graph/decisions.py), not
# just returned and forgotten.
import asyncio

from app.context.orchestrator import ContextOrchestrator


class _FakeLLMClient:
    def __init__(self, response):
        self._response = response

    async def generate_response(self, messages, response_model=None, max_tokens=None):
        return self._response


class _FakeGraphiti:
    def __init__(self, llm_response):
        self.llm_client = _FakeLLMClient(llm_response)


class _FakeCausalRepo:
    def __init__(self, anchor, facts, direct_facts=None, second_entity=None):
        self._anchor = anchor
        self._facts = facts
        self._second_entity = second_entity
        # The chain-empty fallback's direct_facts_for lookup -- defaults to
        # empty so existing tests that don't care about it keep behaving
        # exactly as before (empty chain + no direct facts = truly nothing).
        self.direct_facts = direct_facts if direct_facts is not None else []

    async def causal_chain_for_query(self, query, group_ids, visible_uuids):
        return self._anchor, self._second_entity, self._facts

    def direct_facts_for(self, uuid, visible_uuids):
        return self.direct_facts

    # Delegates to the real implementation -- it's pure logic (no Neo4j/LLM
    # call), so there's no reason to fake it, and doing so keeps this fake
    # honest about what actually decides "real causal chain" vs "fact-only
    # fallback" in get_causal_context_packet.
    @staticmethod
    def is_entirely_causal(facts):
        from app.graph.graph_repository import GraphRepository

        return GraphRepository.is_entirely_causal(facts)


_LLM_RESPONSE = {
    "what_happened": "The order was delayed.",
    "why": "Its component ran out of stock at the supplier.",
    "impact": "The customer's shipment will miss its date.",
    "recommendation": "Expedite an alternate supplier for the component.",
}


def _orchestrator(anchor, facts, llm_response=_LLM_RESPONSE, second_entity=None):
    orchestrator = ContextOrchestrator(graphiti_instance=_FakeGraphiti(llm_response))
    orchestrator._repo = _FakeCausalRepo(anchor, facts, second_entity=second_entity)
    return orchestrator


def test_recommendation_is_a_separate_field_never_blended_into_summary(monkeypatch):
    monkeypatch.setattr("app.context.orchestrator.record_decision", lambda repo, **kw: "decision-1")
    anchor = {"uuid": "anchor-1", "name": "Test Order 500"}
    facts = [{"fact": "Test Order 500 depends on Test Widget.", "is_valid": True, "relationship_type": "DEPENDS_ON"}]
    orchestrator = _orchestrator(anchor, facts)

    packet = asyncio.run(
        orchestrator.get_causal_context_packet("Why is Test Order 500 at risk?", group_ids=["kb1"], tenant_id="t1")
    )

    assert packet.metadata["summary"] == "Test Order 500 depends on Test Widget."
    assert packet.metadata["recommendation"] == _LLM_RESPONSE
    assert "Expedite an alternate supplier" not in packet.metadata["summary"]
    assert packet.metadata["retrieval_path"] == "causal_chain"


def test_recommendation_is_recorded_as_a_decision_when_tenant_id_given(monkeypatch):
    recorded = {}

    def fake_record_decision(repo, **kwargs):
        recorded.update(kwargs)
        return "decision-uuid-1"

    monkeypatch.setattr("app.context.orchestrator.record_decision", fake_record_decision)
    anchor = {"uuid": "anchor-1", "name": "Test Order 501"}
    facts = [{"fact": "Test Order 501 depends on Test Widget.", "is_valid": True, "relationship_type": "DEPENDS_ON"}]
    orchestrator = _orchestrator(anchor, facts)

    packet = asyncio.run(
        orchestrator.get_causal_context_packet("Why is Test Order 501 at risk?", group_ids=["kb1"], tenant_id="t1")
    )

    assert packet.metadata["decision_id"] == "decision-uuid-1"
    assert recorded["anchor_uuid"] == "anchor-1"
    assert recorded["group_id"] == "kb1"


def test_no_decision_recorded_without_tenant_id(monkeypatch):
    called = []
    monkeypatch.setattr("app.context.orchestrator.record_decision", lambda repo, **kw: called.append(kw) or "x")
    anchor = {"uuid": "anchor-1", "name": "Test Order 502"}
    facts = [{"fact": "Test Order 502 depends on Test Widget.", "is_valid": True, "relationship_type": "DEPENDS_ON"}]
    orchestrator = _orchestrator(anchor, facts)

    packet = asyncio.run(orchestrator.get_causal_context_packet("Why is Test Order 502 at risk?", group_ids=["kb1"]))

    assert called == []
    assert packet.metadata["decision_id"] is None


def test_unresolved_anchor_returns_none_recommendation_and_no_decision(monkeypatch):
    called = []
    monkeypatch.setattr("app.context.orchestrator.record_decision", lambda repo, **kw: called.append(kw) or "x")
    orchestrator = _orchestrator(None, [])

    packet = asyncio.run(orchestrator.get_causal_context_packet("Why is Unknown Entity at risk?", group_ids=["kb1"], tenant_id="t1"))

    assert packet.metadata["recommendation"] is None
    assert packet.metadata["decision_id"] is None
    assert called == []
    assert packet.metadata["retrieval_path"] == "none"


def test_empty_chain_skips_synthesis_and_decision(monkeypatch):
    called = []
    monkeypatch.setattr("app.context.orchestrator.record_decision", lambda repo, **kw: called.append(kw) or "x")
    anchor = {"uuid": "anchor-1", "name": "Isolated Order"}
    orchestrator = _orchestrator(anchor, [])

    packet = asyncio.run(orchestrator.get_causal_context_packet("Why is Isolated Order at risk?", group_ids=["kb1"], tenant_id="t1"))

    assert packet.metadata["recommendation"] is None
    assert called == []
    assert packet.metadata["retrieval_path"] == "causal_chain_empty"


# --- Chain-empty fallback to the anchor's own direct facts -----------------
# When there's no causal-typed chain, "Explain why" used to just say nothing
# was found even for a question a direct, non-causal fact (e.g. a LOCATED_AT
# edge) could actually answer -- like "where is X located?". This falls back
# to that entity's own direct facts, answered the same fact-only way the
# plain Ask path would: no recommendation, no :Decision, and an irrelevant
# direct fact simply doesn't make it into the synthesized answer (the same
# relevance-to-the-question behavior _synthesize_answer already has on the
# plain path -- not re-tested here, see test_orchestrator_observability.py).


def test_empty_causal_chain_falls_back_to_a_single_direct_fact(monkeypatch):
    called = []
    monkeypatch.setattr("app.context.orchestrator.record_decision", lambda repo, **kw: called.append(kw) or "x")
    anchor = {"uuid": "anchor-1", "name": "Fallback Widget"}
    direct_facts = [{"fact": "Fallback Widget is located in Warehouse 4.", "is_valid": True}]
    orchestrator = _orchestrator(anchor, [])
    orchestrator._repo.direct_facts = direct_facts

    packet = asyncio.run(
        orchestrator.get_causal_context_packet("Where is Fallback Widget located?", group_ids=["kb1"], tenant_id="t1")
    )

    assert packet.metadata["summary"] == "Fallback Widget is located in Warehouse 4."
    assert packet.metadata["recommendation"] is None
    assert packet.metadata["decision_id"] is None
    assert called == []
    assert packet.metadata["retrieval_path"] == "causal_fallback_direct_facts"
    assert packet.metadata["facts"] == direct_facts


def test_empty_causal_chain_falls_back_to_synthesizing_several_direct_facts(monkeypatch):
    called = []
    monkeypatch.setattr("app.context.orchestrator.record_decision", lambda repo, **kw: called.append(kw) or "x")
    anchor = {"uuid": "anchor-1", "name": "Multi Fact Widget"}
    direct_facts = [
        {"fact": "Multi Fact Widget is located in Warehouse 4.", "is_valid": True},
        {"fact": "Multi Fact Widget was requested by customer ACME.", "is_valid": True},
    ]
    orchestrator = _orchestrator(anchor, [], llm_response={"answer": "Multi Fact Widget is located in Warehouse 4."})
    orchestrator._repo.direct_facts = direct_facts

    packet = asyncio.run(
        orchestrator.get_causal_context_packet("Where is Multi Fact Widget located?", group_ids=["kb1"], tenant_id="t1")
    )

    assert packet.metadata["summary"] == "Multi Fact Widget is located in Warehouse 4."
    assert packet.metadata["recommendation"] is None
    assert called == []
    assert packet.metadata["retrieval_path"] == "causal_fallback_direct_facts"


def test_empty_causal_chain_with_no_direct_facts_either_still_reports_not_found(monkeypatch):
    called = []
    monkeypatch.setattr("app.context.orchestrator.record_decision", lambda repo, **kw: called.append(kw) or "x")
    anchor = {"uuid": "anchor-1", "name": "Truly Isolated Widget"}
    orchestrator = _orchestrator(anchor, [])  # direct_facts defaults to []

    packet = asyncio.run(
        orchestrator.get_causal_context_packet("Why is Truly Isolated Widget at risk?", group_ids=["kb1"], tenant_id="t1")
    )

    assert packet.metadata["recommendation"] is None
    assert called == []
    assert packet.metadata["retrieval_path"] == "causal_chain_empty"
    assert packet.metadata["facts"] == []


def test_synthesis_failure_still_returns_the_chain_without_recommendation():
    anchor = {"uuid": "anchor-1", "name": "Fragile Order"}
    facts = [{"fact": "Fragile Order depends on something.", "is_valid": True, "relationship_type": "DEPENDS_ON"}]
    # A response that doesn't fit _CausalRecommendation's schema simulates a
    # malformed/failed LLM call -- should degrade gracefully, not crash the query.
    orchestrator = _orchestrator(anchor, facts, llm_response={"not": "the right shape"})

    packet = asyncio.run(orchestrator.get_causal_context_packet("Why is Fragile Order at risk?", group_ids=["kb1"]))

    assert packet.metadata["recommendation"] is None
    assert packet.metadata["summary"] == "Fragile Order depends on something."


# --- Two-entity "how is X connected to Y" routing ---------------------------
# Regression: this case previously didn't exist in get_causal_context_packet
# at all -- causal_chain_for_query only ever returned one anchor, so a
# two-entity query silently got that single entity's own unrelated facts
# treated as "the explanation." Found for real against production data: "How
# is Industrial Automation connected to Diego Alvarez?" returned "Vantus
# Robotics operates in the Industrial Automation industry" -- a fact that
# doesn't even mention Diego Alvarez. See CLAUDE.md and test_causal_chain.py
# for the GraphRepository-level fix this exercises at the orchestrator level.


def test_two_entity_path_entirely_causal_gets_a_real_recommendation(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        "app.context.orchestrator.record_decision",
        lambda repo, **kw: recorded.update(kw) or "decision-two-ent",
    )
    anchor = {"uuid": "component-1", "name": "Test Component"}
    second_entity = {"uuid": "qe-1", "name": "Test QualityEvent"}
    facts = [
        {"fact": "Test Component is sourced from Test Supplier.", "is_valid": True, "relationship_type": "SOURCED_FROM"},
        {"fact": "Test QualityEvent affects Test Supplier.", "is_valid": True, "relationship_type": "AFFECTS"},
    ]
    orchestrator = _orchestrator(anchor, facts, second_entity=second_entity)

    packet = asyncio.run(
        orchestrator.get_causal_context_packet(
            "How is Test Component connected to Test QualityEvent?", group_ids=["kb1"], tenant_id="t1"
        )
    )

    assert packet.metadata["recommendation"] == _LLM_RESPONSE
    assert packet.metadata["decision_id"] == "decision-two-ent"
    assert packet.metadata["retrieval_path"] == "causal_chain"
    assert recorded["anchor_uuid"] == "component-1"


def test_two_entity_path_not_entirely_causal_is_fact_only_with_full_evidence(monkeypatch):
    called = []
    monkeypatch.setattr("app.context.orchestrator.record_decision", lambda repo, **kw: called.append(kw) or "x")
    anchor = {"uuid": "industry-1", "name": "Industrial Automation"}
    second_entity = {"uuid": "person-1", "name": "Diego Alvarez"}
    facts = [
        {
            "fact": "Brightpeak Automation operates in Industrial Automation.",
            "is_valid": True, "relationship_type": "IS_A",
        },
        {
            "fact": "Brightpeak Automation has Diego Alvarez as its account manager.",
            "is_valid": True, "relationship_type": "MANAGED_BY",
        },
    ]
    orchestrator = _orchestrator(
        anchor, facts, second_entity=second_entity,
        llm_response={"answer": "Industrial Automation connects to Diego Alvarez through Brightpeak Automation."},
    )

    packet = asyncio.run(
        orchestrator.get_causal_context_packet(
            "How is Industrial Automation connected to Diego Alvarez?", group_ids=["kb1"], tenant_id="t1"
        )
    )

    # The actual point of this fix: BOTH hops of the real connecting path
    # come back as evidence, not one arbitrary entity's own unrelated fact.
    assert packet.metadata["facts"] == facts
    assert packet.metadata["summary"] == "Industrial Automation connects to Diego Alvarez through Brightpeak Automation."
    assert packet.metadata["recommendation"] is None
    assert packet.metadata["decision_id"] is None
    assert called == []  # no :Decision -- this was never treated as a causal inference
    assert packet.metadata["retrieval_path"] == "causal_path_between_entities"


def test_two_entity_no_path_reports_no_connection_found_not_an_unrelated_fact(monkeypatch):
    called = []
    monkeypatch.setattr("app.context.orchestrator.record_decision", lambda repo, **kw: called.append(kw) or "x")
    anchor = {"uuid": "a-1", "name": "Isolated Thing One"}
    second_entity = {"uuid": "b-1", "name": "Isolated Thing Two"}
    orchestrator = _orchestrator(anchor, [], second_entity=second_entity)

    packet = asyncio.run(
        orchestrator.get_causal_context_packet(
            "How is Isolated Thing One connected to Isolated Thing Two?", group_ids=["kb1"], tenant_id="t1"
        )
    )

    assert packet.metadata["recommendation"] is None
    assert called == []
    assert packet.metadata["retrieval_path"] == "causal_chain_empty"
    assert packet.metadata["facts"] == []
    assert "Isolated Thing One" in packet.metadata["summary"]
    assert "Isolated Thing Two" in packet.metadata["summary"]
