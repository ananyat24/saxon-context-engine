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
from app.api.admin import router as admin_router
from app.api.webhooks import router as webhooks_router
from app.api.odata import router as odata_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["Health"])
api_router.include_router(entities_router, prefix="/entities", tags=["Entities"])
api_router.include_router(context_router, prefix="/context", tags=["Context"])
# Every route in graph.py requires a tenant API key and scopes its query to
# that tenant's group_id -- see app/api/graph.py's module docstring.
api_router.include_router(graph_router, prefix="/graph", tags=["Graph"])
api_router.include_router(document_sets_router, prefix="/document-sets", tags=["Document Sets"])
api_router.include_router(connectors_router, prefix="/connectors", tags=["Connectors"])
# Operator-only (ADMIN_API_KEY, not a tenant's own key) -- see app/api/admin.py.
api_router.include_router(admin_router, prefix="/admin", tags=["Admin"])
# Unauthenticated (Microsoft Graph calls this directly, with no API key) --
# see app/api/webhooks.py's module docstring for the actual trust boundary.
api_router.include_router(webhooks_router, prefix="/webhooks", tags=["Webhooks"])
# The Context Layer's BI surface -- a read-only OData v4 feed Power BI's
# built-in "OData Feed" connector can point at directly. Same X-API-Key
# tenant auth as every other route here; see app/api/odata.py.
api_router.include_router(odata_router, prefix="/odata", tags=["OData / BI"])

__all__ = ["api_router"]
