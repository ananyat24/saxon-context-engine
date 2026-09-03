# The one real implementation behind "ask a question, get assembled context
# back": resolves scope (a single knowledge base, a document set, or an
# as_user-restricted view), checks the response cache, runs retrieval +
# synthesis, and caches the result. Both the HTTP route
# (app/api/context.py) and the MCP server (app/mcp/server.py) call this
# directly rather than each re-implementing scope resolution, so the two
# surfaces can never drift on what a given (tenant, query, scope) actually
# returns.
import logging
from typing import Optional

from cryptography.fernet import InvalidToken
from fastapi import HTTPException, status

from app.config import TenantConfig, settings
from app.context.orchestrator import ContextOrchestrator
from app.context.response_cache import get_response_cache
from app.graph import authorization, connectors, document_sets
from app.graph.graph_repository import GraphRepository
from app.graph.neo4j_client import Neo4jClient
from app.graph.spend_limiter import SpendLimitExceeded, get_limiter
from app.graph.tenant_graphiti_pool import TenantGraphitiPool
from app.graph.token_crypto import decrypt_token
from app.models.context_packet import ContextPacket
from app.retrieval.fabric_iq_ontology_retriever import FabricIQOntologyRetriever
from app.retrieval.foundry_iq_retriever import FoundryIQRetriever, foundry_iq_configured
from app.retrieval.work_iq_retriever import WorkIQRetriever
from app.security import resolve_knowledge_base

logger = logging.getLogger(__name__)


def _resolve_foundry_iq_retriever(
    tenant_id: str, group_ids: list[str], repo: GraphRepository
) -> Optional[FoundryIQRetriever]:
    """A per-knowledge-base "foundry_iq" connector (configured through the
    UI, see app/api/connectors.py's _create_foundry_iq_connector) takes
    priority over the deployment-wide FOUNDRY_IQ_* env vars, so different
    knowledge bases (different clients, on a multi-tenant deployment) can
    each point at their own Foundry IQ knowledge base rather than sharing
    one operator-configured default. Checks each of this query's group_ids
    in order and uses the first match; falls back to the global env-var
    config (foundry_iq_configured()) if none of them has one configured;
    returns None if neither does, so the caller adds nothing to
    extra_retrievers rather than a retriever that would fail every call."""
    for group_id in group_ids:
        config = connectors.find_foundry_iq_config_for_group(tenant_id, group_id, repo=repo)
        if config is None:
            continue
        try:
            api_key = decrypt_token(config["api_key_enc"])
        except InvalidToken:
            logger.warning(f"Foundry IQ connector for group '{group_id}' has an undecryptable credential; skipping it.")
            continue
        return FoundryIQRetriever(
            search_endpoint=config["search_endpoint"], api_key=api_key, knowledge_base=config["knowledge_base"],
        )
    return FoundryIQRetriever() if foundry_iq_configured() else None


def _resolve_microsoft_iq_retrievers(tenant_id: str, group_ids: list[str], repo: GraphRepository) -> list:
    """The "fabric_iq_ontology"/"work_iq" counterpart to
    _resolve_foundry_iq_retriever above. There's no global-env-var fallback
    here (unlike Foundry IQ, these have no service-credential config at all,
    see app/config.py's microsoft_oauth_* settings' own comment on why),
    so a knowledge base only ever gets one of these if a connector was
    actually connected for it. Both, not just the first match, can apply
    to the same group_ids: a tenant may have connected Fabric IQ
    Ontology and Work IQ separately, and both are worth querying."""
    retrievers: list = []
    for group_id in group_ids:
        for connector_type in ("fabric_iq_ontology", "work_iq"):
            config = connectors.find_microsoft_iq_config_for_group(tenant_id, group_id, connector_type, repo=repo)
            if config is None or not config.get("oauth_refresh_token_enc"):
                continue
            try:
                refresh_token = decrypt_token(config["oauth_refresh_token_enc"])
            except InvalidToken:
                logger.warning(f"{connector_type} connector for group '{group_id}' has an undecryptable credential; skipping it.")
                continue
            if connector_type == "fabric_iq_ontology":
                retrievers.append(FabricIQOntologyRetriever(
                    tenant_id=settings.microsoft_oauth_tenant_id, workspace_id=config["fabric_iq_workspace_id"],
                    ontology_id=config["fabric_iq_ontology_id"], refresh_token=refresh_token,
                    scope=settings.fabric_iq_ontology_scope,
                ))
            else:
                retrievers.append(WorkIQRetriever(refresh_token=refresh_token, scope=settings.work_iq_scope))
    return retrievers


# Only anthropic/azure_openai calls are ever recorded against the spend
# limiter (see app/graph/graphiti_adapter.py). A Gemini-provider tenant's
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
    """Raises HTTPException (400 unknown scope, 402 spend limit exceeded).
    Both callers are expected to either let it propagate (FastAPI turns it
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
        # A copy, not a mutation of the cached ContextPacket itself: other
        # callers may be holding/about to hit the same cache entry, and they
        # each need their own accurate cache_hit value, not whatever the last
        # caller happened to set it to.
        return cached.model_copy(update={"metadata": {**cached.metadata, "cache_hit": True}})

    graphiti = await graphiti_pool.get_or_create(tenant)
    # Only added when a foundry_iq connector exists for one of these
    # group_ids, or the deployment-wide env vars are set (see
    # _resolve_foundry_iq_retriever above), so an unconfigured tenant's
    # queries run exactly as they did before this integration existed, not
    # against a retriever that would fail every call. Plain Ask only,
    # deliberately not threaded into execute_causal_query below; see this
    # module's own note there.
    foundry_iq_retriever = _resolve_foundry_iq_retriever(tenant.tenant_id, group_ids, repo)
    extra_retrievers = ([foundry_iq_retriever] if foundry_iq_retriever else []) + _resolve_microsoft_iq_retrievers(
        tenant.tenant_id, group_ids, repo
    )
    extra_retrievers = extra_retrievers or None
    orchestrator = ContextOrchestrator(graphiti, neo4j_client=neo4j_client, extra_retrievers=extra_retrievers)
    limiter = get_limiter()
    spent_before = limiter.spent("query")
    try:
        packet = await orchestrator.get_context_packet(
            query, group_ids=group_ids, visible_uuids=visible_uuids, num_results=num_results,
            tenant_id=tenant.tenant_id,
        )
    except SpendLimitExceeded as e:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(e))
    packet.metadata["cache_hit"] = False
    packet.metadata["cost_usd"] = (
        round(limiter.spent("query") - spent_before, 6) if settings.llm_provider in _COST_TRACKED_PROVIDERS else None
    )
    cache.set(cache_key, packet)
    return packet


async def execute_causal_query(
    *,
    tenant: TenantConfig,
    query: str,
    neo4j_client: Neo4jClient,
    graphiti_pool: TenantGraphitiPool,
    knowledge_base: Optional[str] = None,
    as_user: Optional[str] = None,
) -> ContextPacket:
    """The causal-reasoning counterpart to execute_context_query above: the
    same scope resolution as that function's single-knowledge-base branch,
    including as_user's org-hierarchy-scoped visibility (document_set still
    isn't supported here, since a causal chain needs one clear knowledge
    base to write its Decision node into; that part of the docstring
    still holds). Deliberately does NOT go through the response cache: a
    causal query has a real side effect (recording a Decision node, see
    app/graph/decisions.py) every time it runs, and caching the response
    would silently suppress that side effect on a cache hit while still
    looking like a fresh answer to the caller.

    Tracks cost_usd the same way execute_context_query does (only
    anthropic/azure_openai are ever recorded against the spend limiter,
    see _COST_TRACKED_PROVIDERS above), returned in metadata.cost_usd rather
    than silently left out, so a causal query's cost is visible the same way
    a plain query's is.

    Deliberately does NOT get FoundryIQRetriever (see execute_context_query
    above): a causal answer can synthesize a recommendation and writes a
    permanent, auditable :Decision node from whatever facts fed it. An
    external knowledge base result with no group_id, no bi-temporal
    validity, and a separate (currently unmapped) permission model is a
    real, undecided question for that path, not a safe default to wire in
    silently. Revisit once FoundryIQRetriever's own as_user-mapping gap
    (see its retrieve() docstring) is actually resolved.
    """
    repo = GraphRepository(neo4j_client=neo4j_client)
    group_id = resolve_knowledge_base(tenant, knowledge_base)
    user_id = authorization.resolve_as_user(group_id, as_user, repo=repo)
    visible_uuids = authorization.get_visible_entity_uuids(group_id, user_id, repo=repo) if user_id is not None else None

    graphiti = await graphiti_pool.get_or_create(tenant)
    orchestrator = ContextOrchestrator(graphiti, neo4j_client=neo4j_client)
    limiter = get_limiter()
    spent_before = limiter.spent("query")
    try:
        packet = await orchestrator.get_causal_context_packet(
            query, group_ids=[group_id], visible_uuids=visible_uuids, tenant_id=tenant.tenant_id
        )
    except SpendLimitExceeded as e:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(e))
    packet.metadata["cost_usd"] = (
        round(limiter.spent("query") - spent_before, 6) if settings.llm_provider in _COST_TRACKED_PROVIDERS else None
    )
    return packet
