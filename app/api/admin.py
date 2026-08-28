# Operator-only tenant management -- the live-without-a-redeploy path
# alongside `python scripts/manage_tenants.py` / config/tenants.json (see
# app/config.py's tenant_api_keys docstring and app/graph/tenants.py for
# why). Every route here requires ADMIN_API_KEY (app/security.py's
# require_admin), a single operator secret, never a tenant's own key.
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import KnowledgeBase, settings
from app.graph import tenants
from app.graph.graph_repository import GraphRepository
from app.graph.spend_limiter import get_limiter
from app.security import require_admin

router = APIRouter(dependencies=[Depends(require_admin)])


@router.get("/spend")
def get_spend():
    """The real, persisted running totals for both spend-limiter buckets
    (see app/graph/spend_limiter.py) -- moved behind ADMIN_API_KEY rather
    than shown per-query in the main UI, which every tenant's user could
    see regardless of whether they should know this app's own operating
    cost. Per-query cost is still in the raw API response
    (metadata.cost_usd) for anyone building their own tooling against it --
    this route is just what the demo UI's own admin-only footer reads."""
    limiter = get_limiter()
    return {
        "query": {"spent_usd": round(limiter.spent("query"), 6), "budget_usd": settings.azure_openai_query_budget_usd},
        "ingestion": {
            "spent_usd": round(limiter.spent("ingestion"), 6),
            "budget_usd": settings.azure_openai_ingestion_budget_usd,
        },
    }


class CreateTenantRequest(BaseModel):
    tenant_id: str = Field(min_length=1, max_length=200)
    gemini_api_key: str = Field(min_length=1)
    knowledge_bases: list[KnowledgeBase] = Field(min_length=1)


@router.get("/tenants")
def list_tenants(request: Request):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    rows = tenants.list_tenants(repo=repo)
    return [
        {
            "tenant_id": r["tenant_id"],
            "knowledge_bases": [
                {"id": kb_id, "label": kb_label}
                for kb_id, kb_label in zip(r["kb_ids"] or [], r["kb_labels"] or [])
            ],
            "api_key_last4": r["api_key_last4"],
            "created_at": GraphRepository._to_native(r["created_at"]),
        }
        for r in rows
    ]


@router.post("/tenants", status_code=status.HTTP_201_CREATED)
def create_tenant(req: CreateTenantRequest, request: Request):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    try:
        raw_key, summary = tenants.create_tenant(
            req.tenant_id, req.gemini_api_key, req.knowledge_bases, repo=repo
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    # The only place this key is ever returned -- only its hash is stored
    # (see app/graph/tenants.py), so there's no way to recover it later.
    return {**summary, "api_key": raw_key}


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tenant(tenant_id: str, request: Request):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    deleted = tenants.delete_tenant(tenant_id, repo=repo)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
