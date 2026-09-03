# The Reconcile stage's human review surface. See app/graph/reconciliation.py's
# module docstring for the full picture: a :ProposedMerge is a fuzzy name
# match found across two of a tenant's connectors that wasn't confident
# enough to auto-merge on its own. It sits as `pending` until a person here
# approves it (writing the :SAME_AS edge) or rejects it (marked decided,
# never proposed again on a later sync).
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.config import TenantConfig
from app.graph import reconciliation
from app.graph.graph_repository import GraphRepository
from app.security import require_tenant

router = APIRouter()


class ProposalResponse(BaseModel):
    id: str
    entity_a_uuid: str
    entity_a_name: str
    entity_a_group_id: str
    entity_b_uuid: str
    entity_b_name: str
    entity_b_group_id: str
    similarity: float
    status: str
    created_at: str


@router.get("", response_model=list[ProposalResponse])
def list_proposals(request: Request, status_filter: str | None = None, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    rows = reconciliation.list_proposals(repo.execute_cypher, tenant.tenant_id, status=status_filter)
    return [{**row, "created_at": str(row["created_at"])} for row in rows]


@router.post("/{proposal_id}/approve")
def approve_proposal(proposal_id: str, request: Request, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    ok = reconciliation.approve_proposal(repo.execute_cypher, tenant.tenant_id, proposal_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or already-decided proposal.")
    return {"status": "approved"}


@router.post("/{proposal_id}/reject")
def reject_proposal(proposal_id: str, request: Request, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    ok = reconciliation.reject_proposal(repo.execute_cypher, tenant.tenant_id, proposal_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown or already-decided proposal.")
    return {"status": "rejected"}
