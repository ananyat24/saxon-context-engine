# Assembles the individual routers from health.py, entities.py, and context.py into
# one api_router that app/main.py mounts under /api/v1. Add a new endpoint file's
# router here to expose it.
from fastapi import APIRouter
from app.api.health import router as health_router
from app.api.entities import router as entities_router
from app.api.context import router as context_router

api_router = APIRouter()
api_router.include_router(health_router, prefix="/health", tags=["Health"])
api_router.include_router(entities_router, prefix="/entities", tags=["Entities"])
api_router.include_router(context_router, prefix="/context", tags=["Context"])

__all__ = ["api_router"]
