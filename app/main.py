# Entry point for the HTTP API. FastAPI turns this `app` object into a running web
# server when launched with e.g. `uvicorn app.main:app --reload`. The actual route
# definitions live under app/api/ and are wired in via api_router -- this file just
# creates the app and mounts them.
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.api import api_router
from app.graph import authorization, connectors, document_sets
from app.graph.graph_repository import GraphRepository
from app.graph.neo4j_client import Neo4jClient
from app.graph.tenant_graphiti_pool import TenantGraphitiPool


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One Neo4j driver (its own internal connection pool) for the life of the
    # process, shared by every request -- see app/graph/neo4j_client.py.
    # GraphRepository.execute_cypher() otherwise opens a brand-new driver (a
    # fresh TCP + Bolt handshake) on every single Cypher call with no client
    # given, which is fine for a one-off script but doesn't hold up once a
    # single request can fire off several Cypher calls (RBAC visibility,
    # entity resolution, entity edges) under real concurrent load.
    app.state.neo4j_client = Neo4jClient()
    # Each tenant has their own Gemini API key (see app/config.py's TenantConfig),
    # so there's no longer one process-wide Graphiti client to build here -- instead,
    # a pool that builds one client per tenant, lazily, the first time that tenant
    # makes a request, and reuses it after that. `app.state` is FastAPI's built-in
    # place for this kind of shared, per-process object; routes reach it via
    # `request.app.state.graphiti_pool`.
    app.state.graphiti_pool = TenantGraphitiPool()
    # Idempotent -- role-based visibility (app/graph/authorization.py) depends
    # on this index existing for its scaling claim to hold, so it's created on
    # every startup rather than assumed to already be there.
    repo = GraphRepository(neo4j_client=app.state.neo4j_client)
    authorization.ensure_authorization_indexes(repo=repo)
    document_sets.ensure_document_set_indexes(repo=repo)
    connectors.ensure_connector_indexes(repo=repo)
    try:
        yield
    finally:
        await app.state.graphiti_pool.close_all()
        app.state.neo4j_client.close()


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
