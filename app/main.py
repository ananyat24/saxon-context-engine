from fastapi import FastAPI
from app.api import api_router

app = FastAPI(
    title="AIssist Context Engine API",
    description="Ontology-guided temporal context engine powered by Graphiti and Neo4j.",
    version="0.1.0",
)

app.include_router(api_router, prefix="/api/v1")


@app.get("/")
def root():
    return {"message": "Welcome to AIssist Context Engine API. Visit /docs for OpenAPI specs."}
