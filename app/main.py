# Entry point for the HTTP API. FastAPI turns this `app` object into a running web
# server when launched with e.g. `uvicorn app.main:app --reload`. The actual route
# definitions live under app/api/ and are wired in via api_router -- this file just
# creates the app and mounts them.
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.api import api_router
from app.graph.tenant_graphiti_pool import TenantGraphitiPool


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Each tenant has their own Gemini API key (see app/config.py's TenantConfig),
    # so there's no longer one process-wide Graphiti client to build here -- instead,
    # a pool that builds one client per tenant, lazily, the first time that tenant
    # makes a request, and reuses it after that. `app.state` is FastAPI's built-in
    # place for this kind of shared, per-process object; routes reach it via
    # `request.app.state.graphiti_pool`.
    app.state.graphiti_pool = TenantGraphitiPool()
    try:
        yield
    finally:
        await app.state.graphiti_pool.close_all()


# Title/description here are what a client sees in the Swagger UI at /docs --
# the only customer-facing surface this API currently has. Keep the underlying
# tech stack (Neo4j, Graphiti) out of it; that's an implementation detail
# documented for engineers in README.md, not something to expose to end users.
app = FastAPI(
    title="Saxon AI Context Engine",
    description="Ontology-guided temporal context engine for enterprise AI.",
    version="0.1.0",
    lifespan=lifespan,
)

# Every route under app/api/ becomes reachable at /api/v1/<route>, e.g. the health
# check in app/api/health.py ends up at /api/v1/health.
app.include_router(api_router, prefix="/api/v1")
app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/ui", response_class=FileResponse)
def ui():
    return FileResponse("frontend/index.html")

@app.get("/")
def root():
    return {"message": "Welcome to the Saxon AI Context Engine API. Visit /docs for OpenAPI specs."}
