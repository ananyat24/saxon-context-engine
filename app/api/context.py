# POST /api/v1/context/query -- the main "ask a question, get assembled context back"
# endpoint. Delegates all the actual work to execute_context_query
# (app/context/query_service.py, shared with the MCP server in app/mcp/server.py);
# this file is just the HTTP request/response wiring around it.
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from app.config import TenantConfig
from app.context.query_service import execute_context_query
from app.security import require_tenant

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
    return await execute_context_query(
        tenant=tenant,
        query=req.query,
        neo4j_client=request.app.state.neo4j_client,
        graphiti_pool=request.app.state.graphiti_pool,
        knowledge_base=req.knowledge_base,
        document_set=req.document_set,
        as_user=req.as_user,
        result_limit=req.result_limit,
    )
