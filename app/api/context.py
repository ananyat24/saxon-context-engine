# POST /api/v1/context/query -- the main "ask a question, get assembled context back"
# endpoint. Delegates all the actual work to ContextOrchestrator; this file is just
# the HTTP request/response wiring around it.
from typing import List, Optional
from fastapi import APIRouter, Request
from pydantic import BaseModel
from app.context.orchestrator import ContextOrchestrator

router = APIRouter()


class SearchQueryRequest(BaseModel):
    query: str
    # Graphiti "group ids" scope a search to a subset of ingested data (e.g. one
    # customer's records). Leave unset to search across everything.
    group_ids: Optional[List[str]] = None


@router.post("/query")
async def query_context(req: SearchQueryRequest, request: Request):
    # The Graphiti client is created once at app startup (see app/main.py's
    # lifespan handler) and shared across requests rather than reconnected each time.
    orchestrator = ContextOrchestrator(request.app.state.graphiti)
    return await orchestrator.get_context_packet(req.query, group_ids=req.group_ids)
