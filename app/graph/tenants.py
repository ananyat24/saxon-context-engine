# Tenants added live, without a redeploy -- see app/api/admin.py.
#
# The original way to onboard a tenant (`python scripts/manage_tenants.py
# add`, writing config/tenants.json, or the TENANT_API_KEYS env var -- see
# app/config.py) is static: it's only read once, at process startup, so
# adding a client meant re-running the *entire* deploy script (a full
# container rebuild) just to add one API key. Stored as :Tenant nodes in
# Neo4j instead, same rationale as :Connector/:DocumentSet -- this is
# operator-created data that has to survive a redeploy, and (the actual
# point here) be visible to every already-running request-handling process
# the moment it's created, not just after a restart.
#
# require_tenant (app/security.py) and the MCP server's own authentication
# (app/mcp/server.py) both check the static config first, then fall back to
# looking a tenant up here -- so an existing statically-configured tenant's
# behavior is completely unchanged, and only a *new* tenant (created via the
# admin API) takes this path.
#
# The API key itself is stored only as a SHA-256 hash, not plaintext --
# unlike config/tenants.json (a local file, gitignored, but still plaintext
# on disk), a Neo4j data breach here wouldn't hand over every tenant's live
# key. The tenant's own Gemini key is still stored in plaintext, same as
# config/tenants.json today -- it has to be, to actually call Gemini with it.
import hashlib
import secrets
from typing import Optional

from app.config import KnowledgeBase, TenantConfig
from app.graph.graph_repository import GraphRepository


def ensure_tenant_indexes(repo: Optional[GraphRepository] = None) -> None:
    """Idempotent, safe to call on every startup -- same pattern as
    app/graph/authorization.py's ensure_authorization_indexes."""
    repo = repo or GraphRepository()
    repo.execute_cypher("CREATE INDEX tenant_api_key_hash IF NOT EXISTS FOR (t:Tenant) ON (t.api_key_hash)")
    repo.execute_cypher(
        "CREATE CONSTRAINT tenant_tenant_id_unique IF NOT EXISTS FOR (t:Tenant) REQUIRE t.tenant_id IS UNIQUE"
    )


def _hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def _row_to_tenant_config(row: dict) -> TenantConfig:
    knowledge_bases = [
        KnowledgeBase(id=kb_id, label=kb_label)
        for kb_id, kb_label in zip(row["kb_ids"] or [], row["kb_labels"] or [])
    ]
    return TenantConfig(tenant_id=row["tenant_id"], gemini_api_key=row["gemini_api_key"], knowledge_bases=knowledge_bases)


def find_tenant_by_api_key(api_key: str, repo: Optional[GraphRepository] = None) -> Optional[TenantConfig]:
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        "MATCH (t:Tenant {api_key_hash: $hash}) "
        "RETURN t.tenant_id AS tenant_id, t.gemini_api_key AS gemini_api_key, "
        "t.kb_ids AS kb_ids, t.kb_labels AS kb_labels",
        {"hash": _hash_api_key(api_key)},
    )
    if not rows:
        return None
    return _row_to_tenant_config(rows[0])


def list_tenant_configs(repo: Optional[GraphRepository] = None) -> list[TenantConfig]:
    """Every Neo4j-backed tenant as a full TenantConfig (Gemini key
    included) -- for internal, in-process use only (the background
    connector scheduler needs to sync *every* tenant's connectors, not just
    the statically-configured ones). Never expose this via an API response;
    use list_tenants() (masked) for anything client- or operator-facing."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        "MATCH (t:Tenant) RETURN t.tenant_id AS tenant_id, t.gemini_api_key AS gemini_api_key, "
        "t.kb_ids AS kb_ids, t.kb_labels AS kb_labels"
    )
    return [_row_to_tenant_config(r) for r in rows]


def list_tenants(repo: Optional[GraphRepository] = None) -> list[dict]:
    """Never returns the API key itself, even hashed -- only its last 4
    characters, enough for an operator to recognize which key is which
    without the listing becoming a second way to exfiltrate a live key."""
    repo = repo or GraphRepository()
    return repo.execute_cypher(
        "MATCH (t:Tenant) "
        "RETURN t.tenant_id AS tenant_id, t.kb_ids AS kb_ids, t.kb_labels AS kb_labels, "
        "t.api_key_last4 AS api_key_last4, t.created_at AS created_at "
        "ORDER BY t.created_at DESC"
    )


def create_tenant(
    tenant_id: str,
    gemini_api_key: str,
    knowledge_bases: list[KnowledgeBase],
    repo: Optional[GraphRepository] = None,
) -> tuple[str, dict]:
    """Generates a fresh API key, stores only its hash, and returns the raw
    key exactly once -- the caller (the admin API route) is the only place
    it's ever shown; there's no way to recover it afterward, same as most
    providers' own API key creation flow. Raises ValueError if tenant_id is
    already taken (the uniqueness constraint from ensure_tenant_indexes)."""
    repo = repo or GraphRepository()
    raw_key = secrets.token_urlsafe(32)
    kb_ids = [kb.id for kb in knowledge_bases]
    kb_labels = [kb.label for kb in knowledge_bases]
    try:
        repo.execute_cypher(
            """
            CREATE (t:Tenant {
                tenant_id: $tenant_id, api_key_hash: $api_key_hash, api_key_last4: $api_key_last4,
                gemini_api_key: $gemini_api_key, kb_ids: $kb_ids, kb_labels: $kb_labels,
                created_at: datetime()
            })
            """,
            {
                "tenant_id": tenant_id,
                "api_key_hash": _hash_api_key(raw_key),
                "api_key_last4": raw_key[-4:],
                "gemini_api_key": gemini_api_key,
                "kb_ids": kb_ids,
                "kb_labels": kb_labels,
            },
        )
    except Exception as e:
        if "already exists" in str(e) or "ConstraintValidationFailed" in str(e):
            raise ValueError(f"Tenant '{tenant_id}' already exists.") from e
        raise
    summary = {
        "tenant_id": tenant_id,
        "knowledge_bases": [kb.model_dump() for kb in knowledge_bases],
        "api_key_last4": raw_key[-4:],
    }
    return raw_key, summary


def delete_tenant(tenant_id: str, repo: Optional[GraphRepository] = None) -> bool:
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        "MATCH (t:Tenant {tenant_id: $tenant_id}) WITH t DETACH DELETE t RETURN count(t) AS deleted",
        {"tenant_id": tenant_id},
    )
    return bool(rows) and rows[0]["deleted"] > 0
