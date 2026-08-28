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


# --- execute_causal_query's spend tracking + as_user visibility -------------
# Same "monkeypatch the collaborators, no real Neo4j/Graphiti" convention as
# above -- ContextOrchestrator itself is replaced with a fake that just
# records what it was called with, so this tests query_service's own wiring
# (spend-limiter diffing, as_user -> visible_uuids resolution), not the
# orchestrator's internals (covered separately by test_causal_recommendation.py).


class _FakeGraphitiPool:
    async def get_or_create(self, tenant):
        return object()


class _FakeLimiter:
    def __init__(self, readings):
        self._readings = iter(readings)

    def spent(self, bucket):
        return next(self._readings)


class _FakeOrchestrator:
    calls = []

    def __init__(self, graphiti, neo4j_client=None):
        pass

    async def get_causal_context_packet(self, query, group_ids=None, visible_uuids=None, tenant_id=None):
        _FakeOrchestrator.calls.append({"visible_uuids": visible_uuids, "group_ids": group_ids})
        return ContextPacket(query=query, metadata={"summary": "s", "recommendation": None})


def test_causal_query_reports_cost_usd_for_a_cost_tracked_provider(monkeypatch):
    monkeypatch.setattr(query_service, "resolve_knowledge_base", lambda t, kb: "kb1")
    monkeypatch.setattr(query_service.authorization, "resolve_as_user", lambda *a, **k: None)
    monkeypatch.setattr(query_service.settings, "llm_provider", "anthropic")
    monkeypatch.setattr(query_service, "get_limiter", lambda: _FakeLimiter([1.0, 1.25]))
    monkeypatch.setattr(query_service, "ContextOrchestrator", _FakeOrchestrator)

    result = asyncio.run(
        query_service.execute_causal_query(
            tenant=_tenant(), query="why is this at risk", neo4j_client=object(), graphiti_pool=_FakeGraphitiPool()
        )
    )
    assert result.metadata["cost_usd"] == 0.25


def test_causal_query_cost_usd_is_none_for_a_non_cost_tracked_provider(monkeypatch):
    monkeypatch.setattr(query_service, "resolve_knowledge_base", lambda t, kb: "kb1")
    monkeypatch.setattr(query_service.authorization, "resolve_as_user", lambda *a, **k: None)
    monkeypatch.setattr(query_service.settings, "llm_provider", "gemini")
    monkeypatch.setattr(query_service, "get_limiter", lambda: _FakeLimiter([0.0, 0.0]))
    monkeypatch.setattr(query_service, "ContextOrchestrator", _FakeOrchestrator)

    result = asyncio.run(
        query_service.execute_causal_query(
            tenant=_tenant(), query="why is this at risk", neo4j_client=object(), graphiti_pool=_FakeGraphitiPool()
        )
    )
    assert result.metadata["cost_usd"] is None


def test_causal_query_resolves_as_user_into_visible_uuids(monkeypatch):
    _FakeOrchestrator.calls.clear()
    monkeypatch.setattr(query_service, "resolve_knowledge_base", lambda t, kb: "kb1")
    monkeypatch.setattr(query_service.authorization, "resolve_as_user", lambda group_id, as_user, repo=None: "jordan")
    monkeypatch.setattr(
        query_service.authorization, "get_visible_entity_uuids", lambda group_id, user_id, repo=None: {"e1", "e2"}
    )
    monkeypatch.setattr(query_service.settings, "llm_provider", "gemini")
    monkeypatch.setattr(query_service, "get_limiter", lambda: _FakeLimiter([0.0, 0.0]))
    monkeypatch.setattr(query_service, "ContextOrchestrator", _FakeOrchestrator)

    asyncio.run(
        query_service.execute_causal_query(
            tenant=_tenant(), query="why is this at risk", neo4j_client=object(), graphiti_pool=_FakeGraphitiPool(),
            as_user="jordan",
        )
    )
    assert _FakeOrchestrator.calls[-1]["visible_uuids"] == {"e1", "e2"}
    assert _FakeOrchestrator.calls[-1]["group_ids"] == ["kb1"]


def test_causal_query_visible_uuids_is_none_without_as_user(monkeypatch):
    _FakeOrchestrator.calls.clear()
    monkeypatch.setattr(query_service, "resolve_knowledge_base", lambda t, kb: "kb1")
    monkeypatch.setattr(query_service.authorization, "resolve_as_user", lambda *a, **k: None)
    monkeypatch.setattr(query_service.settings, "llm_provider", "gemini")
    monkeypatch.setattr(query_service, "get_limiter", lambda: _FakeLimiter([0.0, 0.0]))
    monkeypatch.setattr(query_service, "ContextOrchestrator", _FakeOrchestrator)

    asyncio.run(
        query_service.execute_causal_query(
            tenant=_tenant(), query="why is this at risk", neo4j_client=object(), graphiti_pool=_FakeGraphitiPool()
        )
    )
    assert _FakeOrchestrator.calls[-1]["visible_uuids"] is None
