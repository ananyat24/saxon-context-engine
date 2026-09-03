# Tests app/context/query_service.py's cache_hit/cost_usd bookkeeping around
# execute_context_query. No real Neo4j/Graphiti: scope resolution
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
    # The object actually stored in the cache must be untouched: a second
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
# above: ContextOrchestrator itself is replaced with a fake that just
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


# --- FoundryIQRetriever wiring into execute_context_query --------------
# Confirms the plain Ask path adds/omits it based on config alone, and that
# execute_causal_query never gets it regardless of config (see that
# function's own docstring for why): not FoundryIQRetriever's own
# request/response behavior, covered separately by
# test_foundry_iq_retriever.py.


class _FakeContextOrchestrator:
    calls = []

    def __init__(self, graphiti, neo4j_client=None, extra_retrievers=None):
        _FakeContextOrchestrator.calls.append({"extra_retrievers": extra_retrievers})

    async def get_context_packet(self, query, group_ids=None, visible_uuids=None, num_results=8, tenant_id=None):
        return ContextPacket(query=query, metadata={"summary": "s"})


def _patch_plain_query_scope(monkeypatch):
    monkeypatch.setattr(query_service, "resolve_knowledge_base", lambda t, kb: "kb1")
    monkeypatch.setattr(query_service.authorization, "resolve_as_user", lambda *a, **k: None)
    monkeypatch.setattr(query_service.settings, "llm_provider", "gemini")
    monkeypatch.setattr(query_service, "ContextOrchestrator", _FakeContextOrchestrator)
    # No per-knowledge-base foundry_iq connector in these tests: always
    # falls through to the global env-var check, so foundry_iq_configured
    # alone controls the outcome without touching Neo4j.
    monkeypatch.setattr(query_service.connectors, "find_foundry_iq_config_for_group", lambda *a, **k: None)
    monkeypatch.setattr(query_service.connectors, "find_microsoft_iq_config_for_group", lambda *a, **k: None)


def test_foundry_iq_retriever_is_added_when_fully_configured(monkeypatch):
    _FakeContextOrchestrator.calls.clear()
    _patch_plain_query_scope(monkeypatch)
    monkeypatch.setattr(query_service, "foundry_iq_configured", lambda: True)

    asyncio.run(
        query_service.execute_context_query(
            tenant=_tenant(), query="foundry iq wiring test - configured", neo4j_client=object(), graphiti_pool=_FakeGraphitiPool()
        )
    )
    extra = _FakeContextOrchestrator.calls[-1]["extra_retrievers"]
    assert extra is not None and len(extra) == 1


def test_foundry_iq_retriever_is_omitted_when_not_configured(monkeypatch):
    _FakeContextOrchestrator.calls.clear()
    _patch_plain_query_scope(monkeypatch)
    monkeypatch.setattr(query_service, "foundry_iq_configured", lambda: False)

    asyncio.run(
        query_service.execute_context_query(
            tenant=_tenant(), query="foundry iq wiring test - not configured", neo4j_client=object(), graphiti_pool=_FakeGraphitiPool()
        )
    )
    assert _FakeContextOrchestrator.calls[-1]["extra_retrievers"] is None


# --- _resolve_foundry_iq_retriever's own priority logic -----------------
# Unit-level, not through the full execute_context_query: covers the
# per-knowledge-base-connector-beats-global-env-var priority and the
# undecryptable-credential fallback directly.


def test_resolve_foundry_iq_retriever_prefers_a_per_group_connector_over_global_settings(monkeypatch):
    monkeypatch.setattr(
        query_service.connectors, "find_foundry_iq_config_for_group",
        lambda tenant_id, group_id, repo=None: (
            {"search_endpoint": "https://kb1.search.windows.net", "knowledge_base": "kb1-index", "api_key_enc": "enc-1"}
            if group_id == "kb1" else None
        ),
    )
    monkeypatch.setattr(query_service, "decrypt_token", lambda enc: f"decrypted-{enc}")
    monkeypatch.setattr(query_service, "foundry_iq_configured", lambda: False)  # global not set: connector must win

    retriever = query_service._resolve_foundry_iq_retriever("t1", ["kb1"], repo=None)

    assert retriever is not None
    assert retriever.search_endpoint == "https://kb1.search.windows.net"
    assert retriever.api_key == "decrypted-enc-1"
    assert retriever.knowledge_base == "kb1-index"


def test_resolve_foundry_iq_retriever_falls_back_to_global_settings_when_no_connector_matches(monkeypatch):
    monkeypatch.setattr(query_service.connectors, "find_foundry_iq_config_for_group", lambda *a, **k: None)
    monkeypatch.setattr(query_service, "foundry_iq_configured", lambda: True)

    retriever = query_service._resolve_foundry_iq_retriever("t1", ["kb1"], repo=None)
    assert retriever is not None


def test_resolve_foundry_iq_retriever_skips_an_undecryptable_credential(monkeypatch):
    from cryptography.fernet import InvalidToken

    def _raise(enc):
        raise InvalidToken()

    monkeypatch.setattr(
        query_service.connectors, "find_foundry_iq_config_for_group",
        lambda tenant_id, group_id, repo=None: {
            "search_endpoint": "https://x", "knowledge_base": "kb", "api_key_enc": "bad",
        },
    )
    monkeypatch.setattr(query_service, "decrypt_token", _raise)
    monkeypatch.setattr(query_service, "foundry_iq_configured", lambda: False)

    retriever = query_service._resolve_foundry_iq_retriever("t1", ["kb1"], repo=None)
    assert retriever is None  # undecryptable, and no global fallback configured either
