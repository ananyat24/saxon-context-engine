# POST /api/v1/context/query -- the main "ask a question, get assembled context back"
# endpoint. Delegates all the actual work to ContextOrchestrator; this file is just
# the HTTP request/response wiring around it.
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from app.context.orchestrator import ContextOrchestrator
from app.security import require_tenant

router = APIRouter()


class SearchQueryRequest(BaseModel):
    query: str
    # Note: no group_id/group_ids field here on purpose. Which tenant's data a
    # request can see is determined by its API key (via require_tenant below),
    # not by anything the caller states in the request body -- see app/security.py.


@router.post("/query")
async def query_context(req: SearchQueryRequest, request: Request, group_id: str = Depends(require_tenant)):
    # The Graphiti client is created once at app startup (see app/main.py's
    # lifespan handler) and shared across requests rather than reconnected each time.
    orchestrator = ContextOrchestrator(request.app.state.graphiti)
    return await orchestrator.get_context_packet(req.query, group_ids=[group_id])
