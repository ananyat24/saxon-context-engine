# POST /api/v1/context/query -- the main "ask a question, get assembled context back"
# endpoint. Delegates all the actual work to ContextOrchestrator; this file is just
# the HTTP request/response wiring around it.
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from app.config import TenantConfig
from app.context.orchestrator import ContextOrchestrator
from app.security import require_tenant

router = APIRouter()


class SearchQueryRequest(BaseModel):
    query: str
    # Note: no group_id/API-key field here on purpose. Which tenant a request
    # belongs to -- and therefore whose data and whose Gemini key it uses -- is
    # determined by the X-API-Key header (via require_tenant below), not by
    # anything the caller states in the request body. See app/security.py.


@router.post("/query")
async def query_context(req: SearchQueryRequest, request: Request, tenant: TenantConfig = Depends(require_tenant)):
    # Each tenant gets their own cached Graphiti client, built with their own
    # Gemini key on first use and reused after that -- see
    # app/graph/tenant_graphiti_pool.py.
    graphiti = await request.app.state.graphiti_pool.get_or_create(tenant)
    orchestrator = ContextOrchestrator(graphiti)
    return await orchestrator.get_context_packet(req.query, group_ids=[tenant.group_id])
