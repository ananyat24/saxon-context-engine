# Connectors: a configured link to an external data source (a web page, plus
# demo/mock-data structured "database", "documents", and "email" types --
# see app/api/connectors.py's _CONNECTOR_FACTORIES; a real SharePoint/Google
# Drive/CRM API is meant to slot in later as new `type` values without
# changing this storage layer or the API shape). Each
# connector feeds one of the tenant's existing knowledge bases -- syncing it
# fetches the source's content and runs it through the same
# IngestionPipeline every other source in this codebase already uses (see
# app/api/connectors.py), so nothing about extraction/graph-writing is
# connector-specific.
#
# Stored as :Connector nodes in Neo4j, same rationale as :DocumentSet (see
# app/graph/document_sets.py's module docstring): this is app-owned data a
# client creates/deletes live through the UI, and it has to survive a
# redeploy, which a local JSON file can't.
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.graph.graph_repository import GraphRepository


def ensure_connector_indexes(repo: Optional[GraphRepository] = None) -> None:
    """Idempotent, safe to call on every startup -- same pattern as
    authorization.ensure_authorization_indexes."""
    repo = repo or GraphRepository()
    repo.execute_cypher(
        "CREATE INDEX connector_tenant_id IF NOT EXISTS FOR (c:Connector) ON (c.tenant_id)"
    )


_FIELDS = (
    "id, tenant_id, name, type, group_id, url, status, last_synced_at, "
    "last_error, content_hash"
)
# Real-time push (see app/ingestion/graph_subscriptions.py, app/api/webhooks.py)
# -- kept as separate optional fields rather than folded into _FIELDS above
# since every existing connector predates them (null on any row created
# before this), but still returned by list/get below: app/api/connectors.py's
# _serialize() needs push_subscription_id to report whether push is enabled.
_PUSH_FIELDS = "push_subscription_id, push_client_state, push_expires_at"
_ALL_FIELDS = f"{_FIELDS}, {_PUSH_FIELDS}"


def list_connectors(tenant_id: str, repo: Optional[GraphRepository] = None) -> list[dict]:
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        f"""
        MATCH (c:Connector {{tenant_id: $tenant_id}})
        RETURN {_as_return(_ALL_FIELDS)}
        ORDER BY c.created_at DESC
        """,
        {"tenant_id": tenant_id},
    )
    for row in rows:
        row["last_synced_at"] = GraphRepository._to_native(row["last_synced_at"])
        row["push_expires_at"] = GraphRepository._to_native(row["push_expires_at"])
    return rows


def get_connector(tenant_id: str, connector_id: str, repo: Optional[GraphRepository] = None) -> Optional[dict]:
    """Looked up by id AND tenant_id together -- same boundary every other
    per-tenant lookup in this codebase enforces (see resolve_knowledge_base):
    a caller can only ever reach a connector belonging to their own tenant."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        f"""
        MATCH (c:Connector {{id: $id, tenant_id: $tenant_id}})
        RETURN {_as_return(_ALL_FIELDS)}
        """,
        {"id": connector_id, "tenant_id": tenant_id},
    )
    if not rows:
        return None
    rows[0]["last_synced_at"] = GraphRepository._to_native(rows[0]["last_synced_at"])
    rows[0]["push_expires_at"] = GraphRepository._to_native(rows[0]["push_expires_at"])
    return rows[0]


def create_connector(
    tenant_id: str,
    name: str,
    connector_type: str,
    group_id: str,
    url: str,
    repo: Optional[GraphRepository] = None,
) -> dict:
    repo = repo or GraphRepository()
    connector_id = str(uuid.uuid4())
    repo.execute_cypher(
        """
        CREATE (c:Connector {
            id: $id, tenant_id: $tenant_id, name: $name, type: $type, group_id: $group_id,
            url: $url, status: 'never_synced', last_synced_at: null, last_error: null,
            content_hash: null, created_at: datetime()
        })
        """,
        {
            "id": connector_id,
            "tenant_id": tenant_id,
            "name": name,
            "type": connector_type,
            "group_id": group_id,
            "url": url,
        },
    )
    return {
        "id": connector_id, "tenant_id": tenant_id, "name": name, "type": connector_type,
        "group_id": group_id, "url": url, "status": "never_synced", "last_synced_at": None,
        "last_error": None, "content_hash": None,
    }


def delete_connector(tenant_id: str, connector_id: str, repo: Optional[GraphRepository] = None) -> bool:
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        "MATCH (c:Connector {id: $id, tenant_id: $tenant_id}) WITH c DETACH DELETE c RETURN count(c) AS deleted",
        {"id": connector_id, "tenant_id": tenant_id},
    )
    return bool(rows) and rows[0]["deleted"] > 0


def mark_sync_queued(tenant_id: str, connector_id: str, repo: Optional[GraphRepository] = None) -> None:
    """Called the moment a sync is accepted onto the ingestion queue (see
    app/graph/ingestion_queue.py), before the queued job actually runs --
    so a client polling GET /connectors sees "queued" immediately instead
    of the stale status from whatever the connector's last sync attempt
    was, for however long it takes a worker to pick the job up. Doesn't
    touch last_synced_at/last_error -- those still describe the last
    *completed* attempt until this one finishes and record_sync_result()
    below overwrites them."""
    repo = repo or GraphRepository()
    repo.execute_cypher(
        "MATCH (c:Connector {id: $id, tenant_id: $tenant_id}) SET c.status = 'queued'",
        {"id": connector_id, "tenant_id": tenant_id},
    )


def record_sync_result(
    tenant_id: str,
    connector_id: str,
    *,
    status: str,
    last_error: Optional[str] = None,
    content_hash: Optional[str] = None,
    repo: Optional[GraphRepository] = None,
) -> None:
    """Called once a sync attempt finishes, success or not -- overwrites
    whatever mark_sync_queued() above set. `status` is one of "synced" (new
    content ingested), "unchanged" (fetched fine, but matched content_hash
    from last time so nothing was re-ingested -- see
    app/ingestion/web_source.py's content_hash), or "error". content_hash is
    only updated on an actual "synced" outcome, so an "error" or "unchanged"
    run doesn't clobber the fingerprint a real sync last recorded."""
    repo = repo or GraphRepository()
    set_clauses = ["c.status = $status", "c.last_synced_at = $last_synced_at", "c.last_error = $last_error"]
    params = {
        "id": connector_id,
        "tenant_id": tenant_id,
        "status": status,
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
        "last_error": last_error,
    }
    if content_hash is not None:
        set_clauses.append("c.content_hash = $content_hash")
        params["content_hash"] = content_hash
    repo.execute_cypher(
        f"MATCH (c:Connector {{id: $id, tenant_id: $tenant_id}}) SET {', '.join(set_clauses)}",
        params,
    )


def set_push_subscription(
    tenant_id: str,
    connector_id: str,
    *,
    subscription_id: str,
    client_state: str,
    expires_at,
    repo: Optional[GraphRepository] = None,
) -> None:
    """Records a newly-created (or renewed) Microsoft Graph subscription --
    see app/ingestion/graph_subscriptions.py. Called after a successful
    create_mail_subscription()/renew_subscription() call, never on its own;
    a failed subscription attempt just leaves these fields unset/stale and
    the connector keeps working via polling."""
    repo = repo or GraphRepository()
    repo.execute_cypher(
        "MATCH (c:Connector {id: $id, tenant_id: $tenant_id}) "
        "SET c.push_subscription_id = $subscription_id, c.push_client_state = $client_state, "
        "c.push_expires_at = $expires_at",
        {
            "id": connector_id,
            "tenant_id": tenant_id,
            "subscription_id": subscription_id,
            "client_state": client_state,
            "expires_at": expires_at.isoformat(),
        },
    )


def clear_push_subscription(tenant_id: str, connector_id: str, repo: Optional[GraphRepository] = None) -> None:
    """Called when a subscription is deleted, or renewal fails permanently
    (see app/graph/connector_scheduler.py) -- the connector falls back to
    polling-only, same as it worked before push was ever set up."""
    repo = repo or GraphRepository()
    repo.execute_cypher(
        "MATCH (c:Connector {id: $id, tenant_id: $tenant_id}) "
        "SET c.push_subscription_id = null, c.push_client_state = null, c.push_expires_at = null",
        {"id": connector_id, "tenant_id": tenant_id},
    )


def get_connector_by_subscription_id(subscription_id: str, repo: Optional[GraphRepository] = None) -> Optional[dict]:
    """Maps an inbound Graph notification's subscriptionId back to the
    connector it belongs to -- app/api/webhooks.py's only way to know which
    connector to sync, since the notification itself carries no tenant/API
    key. subscription_id is Graph-assigned and globally unique, so this
    intentionally isn't scoped by tenant_id the way every other lookup in
    this module is."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        f"MATCH (c:Connector {{push_subscription_id: $subscription_id}}) "
        f"RETURN {_as_return(_FIELDS)}, {_as_return(_PUSH_FIELDS)}",
        {"subscription_id": subscription_id},
    )
    if not rows:
        return None
    row = rows[0]
    row["last_synced_at"] = GraphRepository._to_native(row["last_synced_at"])
    row["push_expires_at"] = GraphRepository._to_native(row["push_expires_at"])
    return row


def list_connectors_with_push_subscriptions(repo: Optional[GraphRepository] = None) -> list[dict]:
    """Every connector (any tenant) with an active push subscription -- what
    the scheduler's renewal check iterates, since a subscription nearing
    expiry has to be renewed regardless of which tenant owns its connector."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        f"MATCH (c:Connector) WHERE c.push_subscription_id IS NOT NULL "
        f"RETURN {_as_return(_FIELDS)}, {_as_return(_PUSH_FIELDS)}"
    )
    for row in rows:
        row["last_synced_at"] = GraphRepository._to_native(row["last_synced_at"])
        row["push_expires_at"] = GraphRepository._to_native(row["push_expires_at"])
    return rows


def _as_return(fields: str) -> str:
    return ", ".join(f"c.{f.strip()} AS {f.strip()}" for f in fields.split(","))
