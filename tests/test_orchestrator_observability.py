# Tests ContextOrchestrator.get_context_packet()'s new observability fields
# (retrieval_path: see app/context/orchestrator.py) via a stub retriever,
# no real Graphiti/Neo4j. cost_usd/cache_hit are added one layer up in
# app/context/query_service.py, not here: see test_query_service_observability.py.
import asyncio

from app.context.orchestrator import ContextOrchestrator


class _StubRetriever:
    def __init__(self, facts: list[dict]):
        self._facts = facts

    async def retrieve(self, query, group_ids=None, visible_uuids=None, num_results=8):
        return self._facts


def _orchestrator_with_facts(facts: list[dict]) -> ContextOrchestrator:
    # ContextOrchestrator's constructor only stores graphiti_instance on the
    # GraphRetriever it builds, never calls it, so a plain object() is a
    # safe stand-in as long as the stub retriever below replaces it entirely.
    orchestrator = ContextOrchestrator(graphiti_instance=object())
    orchestrator.retrievers = [_StubRetriever(facts)]
    return orchestrator


def _fact(text: str, **overrides) -> dict:
    base = {
        "fact": text,
        "source_node_uuid": "s1",
        "target_node_uuid": "t1",
        "valid_at": None,
        "invalid_at": None,
        "expired_at": None,
        "is_valid": True,
    }
    base.update(overrides)
    return base


def test_retrieval_path_is_entity_resolution_when_no_fact_is_semantic_search():
    orchestrator = _orchestrator_with_facts([_fact("Acme Corp is an active account.")])
    packet = asyncio.run(orchestrator.get_context_packet("who is Acme Corp"))
    assert packet.metadata["retrieval_path"] == "entity_resolution"


def test_retrieval_path_is_semantic_search_when_a_fact_carries_that_kind():
    orchestrator = _orchestrator_with_facts([_fact("Acme Corp is an active account.", kind="semantic_search")])
    packet = asyncio.run(orchestrator.get_context_packet("tell me about accounts"))
    assert packet.metadata["retrieval_path"] == "semantic_search"


def test_retrieval_path_is_none_when_nothing_matched():
    orchestrator = _orchestrator_with_facts([])
    packet = asyncio.run(orchestrator.get_context_packet("who is Nobody Inc"))
    assert packet.metadata["retrieval_path"] == "none"
    assert packet.metadata["summary"] == "No matching graph context found."
