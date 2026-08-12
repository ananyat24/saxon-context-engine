# Entry point for the HTTP API. FastAPI turns this `app` object into a running web
# server when launched with e.g. `uvicorn app.main:app --reload`. The actual route
# definitions live under app/api/ and are wired in via api_router -- this file just
# creates the app and mounts them.
from contextlib import asynccontextmanager

from fastapi import FastAPI
from app.api import api_router
from app.graph.graphiti_adapter import build_graphiti


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build one Graphiti client (and the Neo4j connection pool + LLM clients it
    # holds) when the server starts, and reuse it for every request, rather than
    # constructing a fresh one per request as earlier versions of this endpoint did.
    # `app.state` is FastAPI's built-in place for this kind of shared, per-process
    # object; routes reach it via `request.app.state.graphiti`.
    app.state.graphiti = build_graphiti()
    try:
        yield
    finally:
        await app.state.graphiti.close()


app = FastAPI(
    title="AIssist Context Engine API",
    description="Ontology-guided temporal context engine powered by Graphiti and Neo4j.",
    version="0.1.0",
    lifespan=lifespan,
)

# Every route under app/api/ becomes reachable at /api/v1/<route>, e.g. the health
# check in app/api/health.py ends up at /api/v1/health.
app.include_router(api_router, prefix="/api/v1")


@app.get("/")
def root():
    return {"message": "Welcome to AIssist Context Engine API. Visit /docs for OpenAPI specs."}
