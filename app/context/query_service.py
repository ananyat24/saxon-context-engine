# The one real implementation behind "ask a question, get assembled context
# back" -- resolves scope (a single knowledge base, a document set, or an
# as_user-restricted view), checks the response cache, runs retrieval +
# synthesis, and caches the result. Both the HTTP route
# (app/api/context.py) and the MCP server (app/mcp/server.py) call this
# directly rather than each re-implementing scope resolution, so the two
# surfaces can never drift on what a given (tenant, query, scope) actually
# returns.
from typing import Optional

from fastapi import HTTPException, status

from app.config import TenantConfig, settings
from app.context.orchestrator import ContextOrchestrator
from app.context.response_cache import get_response_cache
from app.graph import authorization, document_sets
from app.graph.graph_repository import GraphRepository
from app.graph.neo4j_client import Neo4jClient
from app.graph.spend_limiter import SpendLimitExceeded, get_limiter
from app.graph.tenant_graphiti_pool import TenantGraphitiPool
from app.models.context_packet import ContextPacket
from app.security import resolve_knowledge_base

# Only anthropic/azure_openai calls are ever recorded against the spend
# limiter (see app/graph/graphiti_adapter.py) -- a Gemini-provider tenant's
# queries genuinely aren't cost-tracked yet, so cost_usd is reported as None
# for them rather than a misleading 0.0 that would read as "this was free."
_COST_TRACKED_PROVIDERS = {"anthropic", "azure_openai"}


async def execute_context_query(
    *,
    tenant: TenantConfig,
    query: str,
    neo4j_client: Neo4jClient,
    graphiti_pool: TenantGraphitiPool,
    knowledge_base: Optional[str] = None,
    document_set: Optional[str] = None,
    as_user: Optional[str] = None,
    result_limit: Optional[int] = None,
) -> ContextPacket:
    """Raises HTTPException (400 unknown scope, 402 spend limit exceeded) --
    both callers are expected to either let it propagate (FastAPI turns it
    into the matching HTTP response on its own) or catch and translate it."""
    repo = GraphRepository(neo4j_client=neo4j_client)
    num_results = max(1, min(result_limit, 20)) if result_limit else 8

    if document_set:
        if as_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="as_user isn't supported together with document_set yet -- use knowledge_base instead.",
            )
        group_ids = document_sets.resolve_document_set(tenant.tenant_id, document_set, repo=repo)
        visible_uuids = None
    else:
        group_id = resolve_knowledge_base(tenant, knowledge_base)
        group_ids = [group_id]
        user_id = authorization.resolve_as_user(group_id, as_user, repo=repo)
        visible_uuids = authorization.get_visible_entity_uuids(group_id, user_id, repo=repo) if user_id is not None else None

    cache = get_response_cache()
    cache_key = cache.make_key(tenant.tenant_id, group_ids, as_user, query, num_results)
    cached = cache.get(cache_key)
    if cached is not None:
        # A copy, not a mutation of the cached ContextPacket itself -- other
        # callers may be holding/about to hit the same cache entry, and they
        # each need their own accurate cache_hit value, not whatever the last
        # caller happened to set it to.
        return cached.model_copy(update={"metadata": {**cached.metadata, "cache_hit": True}})

    graphiti = await graphiti_pool.get_or_create(tenant)
    orchestrator = ContextOrchestrator(graphiti, neo4j_client=neo4j_client)
    limiter = get_limiter()
    spent_before = limiter.spent("query")
    try:
        packet = await orchestrator.get_context_packet(
            query, group_ids=group_ids, visible_uuids=visible_uuids, num_results=num_results
        )
    except SpendLimitExceeded as e:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(e))
    packet.metadata["cache_hit"] = False
    packet.metadata["cost_usd"] = (
        round(limiter.spent("query") - spent_before, 6) if settings.llm_provider in _COST_TRACKED_PROVIDERS else None
    )
    cache.set(cache_key, packet)
    return packet
