# GET endpoints for browsing a tenant's own graph data directly -- mainly for
# the UI (see frontend/), so it has something real to render beyond the
# ontology's static type list. Every route here requires a tenant's API key
# and scopes its Cypher query to that tenant's group_id; none of them accept
# a group_id from the caller, for the same reason /context/query doesn't (see
# app/security.py) -- a client-supplied group_id would make tenant isolation
# advisory instead of enforced.
from fastapi import APIRouter, Depends

from app.config import TenantConfig
from app.graph.graph_repository import GraphRepository
from app.security import require_tenant

router = APIRouter()


@router.get("/summary")
def get_summary(tenant: TenantConfig = Depends(require_tenant)):
    """Node/relationship counts and which entity types are actually present
    for this tenant -- a quick "is there anything in here, and what kind of
    thing is it" view."""
    repo = GraphRepository()
    node_count = repo.execute_cypher(
        "MATCH (n:Entity {group_id: $group_id}) RETURN count(n) AS c",
        {"group_id": tenant.group_id},
    )[0]["c"]
    rel_count = repo.execute_cypher(
        "MATCH (:Entity {group_id: $group_id})-[r:RELATES_TO]->(:Entity {group_id: $group_id}) RETURN count(r) AS c",
        {"group_id": tenant.group_id},
    )[0]["c"]
    labels = repo.execute_cypher(
        "MATCH (n:Entity {group_id: $group_id}) RETURN DISTINCT labels(n) AS labels",
        {"group_id": tenant.group_id},
    )
    entity_types = sorted({label for row in labels for label in row["labels"] if label != "Entity"})

    return {
        "group_id": tenant.group_id,
        "node_count": node_count,
        "relationship_count": rel_count,
        "entity_types_present": entity_types,
    }


@router.get("/nodes")
def get_nodes(limit: int = 50, tenant: TenantConfig = Depends(require_tenant)):
    """This tenant's nodes, most recently created first."""
    limit = max(1, min(limit, 200))  # keep an unbounded ?limit= from becoming a full graph dump
    repo = GraphRepository()
    rows = repo.execute_cypher(
        """
        MATCH (n:Entity {group_id: $group_id})
        RETURN n.uuid AS id, n.name AS name, labels(n) AS labels, n.summary AS summary
        ORDER BY n.created_at DESC
        LIMIT $limit
        """,
        {"group_id": tenant.group_id, "limit": limit},
    )
    return rows


@router.get("/relationships")
def get_relationships(limit: int = 50, tenant: TenantConfig = Depends(require_tenant)):
    """This tenant's relationships, most recently created first."""
    limit = max(1, min(limit, 200))
    repo = GraphRepository()
    rows = repo.execute_cypher(
        """
        MATCH (a:Entity {group_id: $group_id})-[r:RELATES_TO]->(b:Entity {group_id: $group_id})
        RETURN a.name AS source, r.name AS type, b.name AS target, r.fact AS fact
        ORDER BY r.created_at DESC
        LIMIT $limit
        """,
        {"group_id": tenant.group_id, "limit": limit},
    )
    return rows
