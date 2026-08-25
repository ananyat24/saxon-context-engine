# Document Sets: named groupings of one or more of a tenant's own knowledge
# bases ("connectors" in this feature's terms -- see app/config.py's
# KnowledgeBase), so a query can be scoped to several connectors at once
# under one label instead of picking exactly one knowledge_base each time.
#
# Stored as :DocumentSet nodes directly in Neo4j, the same way :User/
# :REPORTS_TO authorization data is (see app/graph/authorization.py's module
# docstring for why): this is app-owned structured data a client creates and
# deletes live through the UI, not something an LLM extraction pipeline
# should be inventing from text. It also has to actually persist across
# requests and redeploys -- config/tenants.json is only read once at process
# startup, and Azure Container Apps' filesystem doesn't survive a redeploy
# anyway, so a JSON file on disk isn't a real option for something meant to
# be created/deleted live.
#
# Scoped by tenant_id, not group_id -- a set can name several group_ids at
# once, so it isn't itself a single data partition the way group_id is.
import uuid
from typing import Optional

from fastapi import HTTPException, status

from app.graph.graph_repository import GraphRepository


def ensure_document_set_indexes(repo: Optional[GraphRepository] = None) -> None:
    """Idempotent, safe to call on every startup -- same pattern as
    authorization.ensure_authorization_indexes."""
    repo = repo or GraphRepository()
    repo.execute_cypher(
        "CREATE INDEX document_set_tenant_id IF NOT EXISTS FOR (d:DocumentSet) ON (d.tenant_id)"
    )


def list_document_sets(tenant_id: str, repo: Optional[GraphRepository] = None) -> list[dict]:
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        """
        MATCH (d:DocumentSet {tenant_id: $tenant_id})
        RETURN d.id AS id, d.name AS name, d.connector_ids AS connector_ids, d.is_public AS is_public
        ORDER BY d.created_at DESC
        """,
        {"tenant_id": tenant_id},
    )
    return rows


def get_document_set(tenant_id: str, document_set_id: str, repo: Optional[GraphRepository] = None) -> Optional[dict]:
    """Looked up by id AND tenant_id together, the same boundary
    resolve_knowledge_base enforces for a single knowledge base -- a caller
    can only ever reach a document set that belongs to their own tenant,
    never one they merely guessed the id of."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        """
        MATCH (d:DocumentSet {id: $id, tenant_id: $tenant_id})
        RETURN d.id AS id, d.name AS name, d.connector_ids AS connector_ids, d.is_public AS is_public
        """,
        {"id": document_set_id, "tenant_id": tenant_id},
    )
    return rows[0] if rows else None


def create_document_set(
    tenant_id: str,
    name: str,
    connector_ids: list[str],
    is_public: bool,
    repo: Optional[GraphRepository] = None,
) -> dict:
    repo = repo or GraphRepository()
    doc_set_id = str(uuid.uuid4())
    repo.execute_cypher(
        """
        CREATE (d:DocumentSet {
            id: $id, tenant_id: $tenant_id, name: $name, connector_ids: $connector_ids,
            is_public: $is_public, created_at: datetime()
        })
        """,
        {
            "id": doc_set_id,
            "tenant_id": tenant_id,
            "name": name,
            "connector_ids": connector_ids,
            "is_public": is_public,
        },
    )
    return {"id": doc_set_id, "name": name, "connector_ids": connector_ids, "is_public": is_public}


def delete_document_set(tenant_id: str, document_set_id: str, repo: Optional[GraphRepository] = None) -> bool:
    """Returns whether anything was actually deleted, so the route can 404 on
    an id that doesn't belong to this tenant instead of silently no-opping."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        "MATCH (d:DocumentSet {id: $id, tenant_id: $tenant_id}) WITH d DETACH DELETE d RETURN count(d) AS deleted",
        {"id": document_set_id, "tenant_id": tenant_id},
    )
    return bool(rows) and rows[0]["deleted"] > 0


def resolve_document_set(tenant_id: str, document_set_id: str, repo: Optional[GraphRepository] = None) -> list[str]:
    """Turns a document_set id into the connector (knowledge_base) ids it
    names -- the same boundary resolve_knowledge_base enforces for a single
    id: the set must belong to *this* tenant or the request is rejected."""
    doc_set = get_document_set(tenant_id, document_set_id, repo=repo)
    if doc_set is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown document set '{document_set_id}' for this tenant.",
        )
    return doc_set["connector_ids"]
