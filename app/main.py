# Entry point for the HTTP API. FastAPI turns this `app` object into a running web
# server when launched with e.g. `uvicorn app.main:app --reload`. The actual route
# definitions live under app/api/ and are wired in via api_router -- this file just
# creates the app and mounts them.
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from mcp.server.transport_security import TransportSecuritySettings
from app.api import api_router
from app.config import settings
from app.graph import authorization, connectors, document_sets, tenants
from app.graph.connector_scheduler import start_connector_scheduler
from app.graph.decisions import ensure_decision_indexes
from app.graph.graph_repository import GraphRepository
from app.graph.ingestion_queue import IngestionQueue
from app.graph.neo4j_client import Neo4jClient
from app.graph.tenant_graphiti_pool import TenantGraphitiPool
from app.mcp import server as mcp_server_module

# The MCP server's ASGI app (app/mcp/server.py) is built once at import time.
# Its own route is registered at exactly "/mcp"; its routes are appended
# directly onto this app's router below rather than via app.mount("/mcp", ...),
# which would register the route at "/mcp/mcp" (double prefix) or otherwise
# 307-redirect POST /mcp -> /mcp/, which not every MCP client follows.
# transport_security allow-lists this deployment's own hostname (see
# settings.mcp_allowed_hosts) -- the SDK's DNS-rebinding protection 421s any
# request whose Host header isn't explicitly listed, before auth even runs.
# allowed_origins is deliberately left empty: MCP clients (Claude Desktop/
# Code, server-side agents) call this directly, not from a browser, so they
# send no Origin header at all -- the SDK already treats an absent Origin as
# passing. Populating this with bare host:port strings wouldn't match a real
# "scheme://host" Origin value anyway.
mcp_asgi_app = mcp_server_module.mcp_server.streamable_http_app(
    streamable_http_path="/mcp",
    transport_security=TransportSecuritySettings(allowed_hosts=settings.mcp_allowed_hosts_list()),
)


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
    tenants.ensure_tenant_indexes(repo=repo)
    ensure_decision_indexes(repo=repo)
    # Periodically syncs every tenant's connectors in the background -- see
    # app/graph/connector_scheduler.py. Returns None (and starts nothing) if
    # disabled via settings.connector_sync_enabled.
    app.state.connector_scheduler = start_connector_scheduler(app.state.neo4j_client)
    # Backs the manual "Sync now" route (app/api/connectors.py) -- accepts a
    # sync job and returns immediately instead of blocking the HTTP request
    # on fetch+extraction. See app/graph/ingestion_queue.py.
    app.state.ingestion_queue = IngestionQueue()
    app.state.ingestion_queue.start()
    # The MCP server (app/mcp/server.py) is mounted as its own Starlette
    # sub-app below, so it doesn't share this FastAPI app's `state` --
    # configure() hands its tools the same neo4j_client/graphiti_pool
    # directly instead. Its session manager also needs its own async
    # context entered for the life of the process (the streamable-http
    # transport's stateful session bookkeeping depends on it), hence the
    # AsyncExitStack rather than just a second `yield`.
    mcp_server_module.configure(app.state.neo4j_client, app.state.graphiti_pool)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(mcp_server_module.mcp_server.session_manager.run())
        try:
            yield
        finally:
            if app.state.connector_scheduler is not None:
                app.state.connector_scheduler.shutdown(wait=False)
            await app.state.ingestion_queue.stop()
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
# MCP (v3.5): any MCP-capable agent (Claude Desktop/Code, Copilot...) can
# point at https://<this-deployment>/mcp with the tenant's own X-API-Key
# header and query their consolidated graph -- see app/mcp/server.py.
app.router.routes.extend(mcp_asgi_app.routes)

@app.get("/ui", response_class=FileResponse)
def ui():
    return FileResponse("frontend/index.html")

@app.get("/")
def root():
    return {"message": "Welcome to the Saxon AI Context Engine API. Visit /docs for OpenAPI specs."}
