# Assembles the individual routers from health.py, entities.py, and context.py into
# one api_router that app/main.py mounts under /api/v1. Add a new endpoint file's
# router here to expose it.
from fastapi import APIRouter
from app.api.health import router as health_router
from app.api.entities import router as entities_router
from app.api.context import router as context_router
from app.api.graph import router as graph_router
from app.api.document_sets import router as document_sets_router
from app.api.connectors import router as connectors_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["Health"])
api_router.include_router(entities_router, prefix="/entities", tags=["Entities"])
api_router.include_router(context_router, prefix="/context", tags=["Context"])
# Every route in graph.py requires a tenant API key and scopes its query to
# that tenant's group_id -- see app/api/graph.py's module docstring.
api_router.include_router(graph_router, prefix="/graph", tags=["Graph"])
api_router.include_router(document_sets_router, prefix="/document-sets", tags=["Document Sets"])
api_router.include_router(connectors_router, prefix="/connectors", tags=["Connectors"])

__all__ = ["api_router"]
