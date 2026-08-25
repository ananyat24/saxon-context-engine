# POST /api/v1/context/query -- the main "ask a question, get assembled context back"
# endpoint. Delegates all the actual work to ContextOrchestrator; this file is just
# the HTTP request/response wiring around it.
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from app.config import TenantConfig
from app.context.orchestrator import ContextOrchestrator
from app.graph import authorization, document_sets
from app.graph.graph_repository import GraphRepository
from app.graph.spend_limiter import SpendLimitExceeded
from app.security import require_tenant, resolve_knowledge_base

router = APIRouter()


class SearchQueryRequest(BaseModel):
    # Capped so a client can't send an arbitrarily large string straight into
    # a paid LLM call -- every query here costs real tokens on the tenant's
    # own Gemini key, and there's no other rate limiting in front of this yet.
    query: str = Field(min_length=1, max_length=2000)
    # Which of the tenant's own knowledge bases to search; defaults to that
    # tenant's first one if omitted. Note there's no API-key field here --
    # which tenant a request belongs to, and therefore whose Gemini key it
    # uses, is determined by the X-API-Key header (via require_tenant below),
    # not by anything the caller states in the request body. See app/security.py.
    knowledge_base: Optional[str] = None
    # Scopes the search across every connector (knowledge base) named by this
    # document set instead of a single one -- see app/graph/document_sets.py.
    # Mutually exclusive with knowledge_base; when given, knowledge_base is
    # ignored.
    document_set: Optional[str] = None
    # Answer only from what this person can see in the org hierarchy, rather
    # than the whole knowledge base -- see app/graph/authorization.py. Not
    # supported together with document_set yet (role-based visibility is
    # scoped to one knowledge base's org chart at a time).
    as_user: Optional[str] = None
    # How many facts the semantic-search fallback returns at most (default 8 --
    # see GraphRepository.search_graphiti_facts). A resolved named entity's own
    # facts are never capped by this. Clamped server-side so a client can't
    # turn a broad question into an arbitrarily large, arbitrarily expensive
    # synthesis call -- meant for a client-side "see more results" follow-up
    # (the initial response's metadata.result_limit_hit says when one's worth
    # offering), not as a default every request should set.
    result_limit: Optional[int] = None


@router.post("/query")
async def query_context(req: SearchQueryRequest, request: Request, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    num_results = max(1, min(req.result_limit, 20)) if req.result_limit else 8

    if req.document_set:
        if req.as_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="as_user isn't supported together with document_set yet -- use knowledge_base instead.",
            )
        group_ids = document_sets.resolve_document_set(tenant.tenant_id, req.document_set, repo=repo)
        visible_uuids = None
    else:
        group_id = resolve_knowledge_base(tenant, req.knowledge_base)
        group_ids = [group_id]
        user_id = authorization.resolve_as_user(group_id, req.as_user, repo=repo)
        visible_uuids = authorization.get_visible_entity_uuids(group_id, user_id, repo=repo) if user_id is not None else None

    # Each tenant gets their own cached Graphiti client, built with their own
    # Gemini key on first use and reused after that -- see
    # app/graph/tenant_graphiti_pool.py.
    graphiti = await request.app.state.graphiti_pool.get_or_create(tenant)
    orchestrator = ContextOrchestrator(graphiti, neo4j_client=request.app.state.neo4j_client)
    try:
        return await orchestrator.get_context_packet(
            req.query, group_ids=group_ids, visible_uuids=visible_uuids, num_results=num_results
        )
    except SpendLimitExceeded as e:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(e))
