from typing import List, Optional
from fastapi import APIRouter
from pydantic import BaseModel
from app.graph.graphiti_adapter import build_graphiti
from app.context.orchestrator import ContextOrchestrator

router = APIRouter()


class SearchQueryRequest(BaseModel):
    query: str
    group_ids: Optional[List[str]] = None


@router.post("/query")
async def query_context(req: SearchQueryRequest):
    graphiti = build_graphiti()
    try:
        orchestrator = ContextOrchestrator(graphiti)
        packet = await orchestrator.get_context_packet(req.query, group_ids=req.group_ids)
        return packet
    finally:
        await graphiti.close()
