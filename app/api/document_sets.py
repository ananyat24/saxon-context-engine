# Document Sets: lets a tenant group several of their own knowledge bases
# ("connectors") into one named, filterable bundle -- see
# app/graph/document_sets.py for the storage layer and app/api/context.py's
# document_set field for how one scopes a query across every connector it
# names.
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import TenantConfig
from app.graph import document_sets
from app.graph.graph_repository import GraphRepository
from app.security import require_tenant

router = APIRouter()


class CreateDocumentSetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    connector_ids: list[str] = Field(min_length=1)
    # Purely a label surfaced in the UI for now, like the reference product's
    # own "Public" column -- this app has one shared API key per tenant, not
    # per-operator accounts within a tenant, so there's no separate identity
    # to withhold a private set from yet.
    is_public: bool = True


def _serialize(doc_set: dict, tenant: TenantConfig) -> dict:
    """Adds each connector's human-readable label on top of the stored
    connector_ids, so the client doesn't need a second round trip or its own
    copy of the tenant's knowledge-base labels just to render a table."""
    labels_by_id = {kb.id: kb.label for kb in tenant.knowledge_bases}
    return {
        "id": doc_set["id"],
        "name": doc_set["name"],
        "connectors": [
            {"id": cid, "label": labels_by_id.get(cid, cid)} for cid in doc_set["connector_ids"]
        ],
        "is_public": doc_set["is_public"],
        # A document set is a live filter over connectors that are already
        # ingested, not a separate index it has to build -- there's no real
        # "indexing"/"stale" state to report here; it's current the moment
        # it's created.
        "status": "Up to date",
    }


@router.get("")
def list_document_sets(request: Request, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    sets = document_sets.list_document_sets(tenant.tenant_id, repo=repo)
    return [_serialize(s, tenant) for s in sets]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_document_set(
    req: CreateDocumentSetRequest, request: Request, tenant: TenantConfig = Depends(require_tenant)
):
    unknown = set(req.connector_ids) - tenant.knowledge_base_ids()
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown connector(s) for this tenant: {', '.join(sorted(unknown))}",
        )
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    created = document_sets.create_document_set(
        tenant.tenant_id, req.name.strip(), req.connector_ids, req.is_public, repo=repo
    )
    return _serialize(created, tenant)


@router.delete("/{document_set_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document_set(document_set_id: str, request: Request, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    deleted = document_sets.delete_document_set(tenant.tenant_id, document_set_id, repo=repo)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document set not found.")
