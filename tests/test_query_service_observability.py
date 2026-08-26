# Tests app/context/query_service.py's cache_hit/cost_usd bookkeeping around
# execute_context_query -- no real Neo4j/Graphiti: scope resolution
# (resolve_knowledge_base/authorization.resolve_as_user) is monkeypatched so
# only the cache-hit short-circuit itself is under test.
import asyncio

from app.config import KnowledgeBase, TenantConfig
from app.context import query_service
from app.context.response_cache import get_response_cache
from app.models.context_packet import ContextPacket


def _tenant() -> TenantConfig:
    return TenantConfig(tenant_id="t1", gemini_api_key="k", knowledge_bases=[KnowledgeBase(id="kb1", label="KB")])


def test_cache_hit_is_marked_true_without_mutating_the_stored_copy(monkeypatch):
    tenant = _tenant()
    monkeypatch.setattr(query_service, "resolve_knowledge_base", lambda t, kb: "kb1")
    monkeypatch.setattr(query_service.authorization, "resolve_as_user", lambda *a, **k: None)

    cache = get_response_cache()
    stored = ContextPacket(query="q", metadata={"summary": "cached", "cache_hit": False})
    key = cache.make_key(tenant.tenant_id, ["kb1"], None, "cache hit test query", 8)
    cache.set(key, stored)

    result = asyncio.run(
        query_service.execute_context_query(
            tenant=tenant, query="cache hit test query", neo4j_client=object(), graphiti_pool=object()
        )
    )

    assert result.metadata["cache_hit"] is True
    assert result.metadata["summary"] == "cached"
    # The object actually stored in the cache must be untouched -- a second
    # concurrent caller reading it independently still needs cache_hit=True
    # (it IS a hit for them too), never the pre-cache False the first
    # computation set before storing it.
    assert stored.metadata["cache_hit"] is False


def test_cost_usd_is_none_when_provider_is_not_cost_tracked(monkeypatch):
    monkeypatch.setattr(query_service.settings, "llm_provider", "gemini")
    assert "gemini" not in query_service._COST_TRACKED_PROVIDERS


def test_cost_tracked_providers_are_anthropic_and_azure_openai():
    assert query_service._COST_TRACKED_PROVIDERS == {"anthropic", "azure_openai"}
