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
    # Backs the connector-scoped queries in app/api/graph.py (GET /graph/nodes
    # and /graph/relationships with ?connector_id=) -- see
    # app/ingestion/connector_sync.py's episode-tagging for what sets this.
    repo.execute_cypher(
        "CREATE INDEX episodic_connector_id IF NOT EXISTS FOR (e:Episodic) ON (e.connector_id)"
    )


_FIELDS = (
    "id, tenant_id, name, type, group_id, url, status, last_synced_at, "
    "last_error, content_hash, source_authority, oauth_file_ids"
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


def reassign_tenant(connector_id: str, new_tenant_id: str, repo: Optional[GraphRepository] = None) -> bool:
    """Moves an existing connector's *management* ownership to a different
    tenant -- for consolidating a connector created under a throwaway/
    temporary tenant (e.g. one stood up just to get a knowledge base's
    first ingestion done before the real tenant had that knowledge base in
    its own list) into the tenant that should actually own it going
    forward. Doesn't touch anything else: the connector's id, group_id,
    url/name, sync state, and -- critically -- the already-ingested graph
    data (Entity/RELATES_TO/Episodic nodes, scoped by group_id, not
    tenant_id) are all unaffected. Operator-only in practice (see
    app/api/admin.py's require_admin) -- there's no tenant-facing route for
    this, since a tenant reassigning its own connector to itself is
    meaningless and reassigning it to a DIFFERENT tenant is exactly the
    kind of cross-tenant action only an operator should be able to do.
    Returns False if no connector with that id exists."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        "MATCH (c:Connector {id: $id}) SET c.tenant_id = $new_tenant_id RETURN count(c) AS updated",
        {"id": connector_id, "new_tenant_id": new_tenant_id},
    )
    return bool(rows) and rows[0]["updated"] > 0


def create_connector(
    tenant_id: str,
    name: str,
    connector_type: str,
    group_id: str,
    url: str,
    repo: Optional[GraphRepository] = None,
    source_authority: int = 0,
) -> dict:
    """source_authority: an operator/tenant-set rank (higher = more
    authoritative), used only to break ties when two connectors' facts
    disagree about the same relationship at the same point in time (see
    app/context/orchestrator.py's authority tie-break) -- it never hides or
    filters a fact, every source's own facts stay visible regardless of
    rank. Defaults to 0 (no special standing) so an unset connector doesn't
    silently outrank or lose to anything."""
    repo = repo or GraphRepository()
    connector_id = str(uuid.uuid4())
    repo.execute_cypher(
        """
        CREATE (c:Connector {
            id: $id, tenant_id: $tenant_id, name: $name, type: $type, group_id: $group_id,
            url: $url, status: 'never_synced', last_synced_at: null, last_error: null,
            content_hash: null, source_authority: $source_authority, created_at: datetime()
        })
        """,
        {
            "id": connector_id,
            "tenant_id": tenant_id,
            "name": name,
            "type": connector_type,
            "group_id": group_id,
            "url": url,
            "source_authority": source_authority,
        },
    )
    return {
        "id": connector_id, "tenant_id": tenant_id, "name": name, "type": connector_type,
        "group_id": group_id, "url": url, "status": "never_synced", "last_synced_at": None,
        "last_error": None, "content_hash": None, "source_authority": source_authority,
    }


def create_oauth_pending_connector(
    tenant_id: str,
    name: str,
    group_id: str,
    oauth_refresh_token_enc: str,
    repo: Optional[GraphRepository] = None,
) -> dict:
    """Creates a "google_drive_oauth" connector immediately after the OAuth
    code exchange succeeds (see app/api/connectors.py's oauth/exchange
    route), before the user has actually picked which files to read --
    status 'authorized_needs_files' distinguishes this from every other
    connector's 'never_synced', so the frontend knows to resume the picker
    rather than offer "Sync now" on a connector with nothing to sync yet.
    oauth_refresh_token_enc must already be Fernet-encrypted (see
    app/graph/token_crypto.py) -- this function stores exactly what it's
    given, it doesn't encrypt on your behalf."""
    repo = repo or GraphRepository()
    connector_id = str(uuid.uuid4())
    repo.execute_cypher(
        """
        CREATE (c:Connector {
            id: $id, tenant_id: $tenant_id, name: $name, type: 'google_drive_oauth',
            group_id: $group_id, url: null, status: 'authorized_needs_files',
            last_synced_at: null, last_error: null, content_hash: null,
            source_authority: 0, oauth_file_ids: [],
            oauth_refresh_token_enc: $oauth_refresh_token_enc, created_at: datetime()
        })
        """,
        {
            "id": connector_id,
            "tenant_id": tenant_id,
            "name": name,
            "group_id": group_id,
            "oauth_refresh_token_enc": oauth_refresh_token_enc,
        },
    )
    return {
        "id": connector_id, "tenant_id": tenant_id, "name": name, "type": "google_drive_oauth",
        "group_id": group_id, "url": None, "status": "authorized_needs_files", "last_synced_at": None,
        "last_error": None, "content_hash": None, "source_authority": 0, "oauth_file_ids": [],
    }


def finalize_oauth_files(
    tenant_id: str,
    connector_id: str,
    file_ids: list[str],
    description: str,
    repo: Optional[GraphRepository] = None,
) -> bool:
    """Called once the user has picked files in the Google Picker (see
    app/api/connectors.py's oauth/files route) -- moves the connector out
    of 'authorized_needs_files' and into the normal 'never_synced' state
    every other connector starts in, so from here on it's indistinguishable
    from any other connector type to the sync path. Only ever moves a
    connector OUT of 'authorized_needs_files', never re-targets an
    already-active one -- picking files again means reconnecting (see that
    route's own docstring for why). Returns False if no matching pending
    connector was found (wrong tenant, wrong id, or already finalized),
    so the caller can 404/409 instead of silently no-op'ing."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        """
        MATCH (c:Connector {id: $id, tenant_id: $tenant_id, type: 'google_drive_oauth',
                             status: 'authorized_needs_files'})
        SET c.oauth_file_ids = $file_ids, c.url = $description, c.status = 'never_synced'
        RETURN count(c) AS updated
        """,
        {"id": connector_id, "tenant_id": tenant_id, "file_ids": file_ids, "description": description},
    )
    return bool(rows) and rows[0]["updated"] > 0


def create_foundry_iq_connector(
    tenant_id: str,
    name: str,
    group_id: str,
    search_endpoint: str,
    knowledge_base: str,
    api_key_enc: str,
    repo: Optional[GraphRepository] = None,
) -> dict:
    """Creates a "foundry_iq" connector -- see app/retrieval/foundry_iq_retriever.py's
    module docstring for why this is a live, query-time retriever config,
    not an ingestion connector: there's no fetch()/content_hash/sync
    lifecycle here, just three settings a query-time lookup
    (find_foundry_iq_config_for_group below) reads back per query. `url`
    stores search_endpoint, same "the address field is the address field"
    convention sharepoint/google_drive already use. api_key_enc must
    already be Fernet-encrypted (see app/graph/token_crypto.py) -- this
    function stores exactly what it's given, same contract as
    create_oauth_pending_connector above. status is set straight to
    'never_synced' (not a distinct pending state) since there's no
    multi-step setup flow like OAuth's picker step -- the connector is
    immediately usable the moment it's created; "Sync now" for this type
    runs a live connectivity check instead of an ingestion sync (see
    app/api/connectors.py's _enqueue_sync)."""
    repo = repo or GraphRepository()
    connector_id = str(uuid.uuid4())
    repo.execute_cypher(
        """
        CREATE (c:Connector {
            id: $id, tenant_id: $tenant_id, name: $name, type: 'foundry_iq',
            group_id: $group_id, url: $search_endpoint, status: 'never_synced',
            last_synced_at: null, last_error: null, content_hash: null,
            source_authority: 0, foundry_iq_knowledge_base: $knowledge_base,
            foundry_iq_api_key_enc: $api_key_enc, created_at: datetime()
        })
        """,
        {
            "id": connector_id,
            "tenant_id": tenant_id,
            "name": name,
            "group_id": group_id,
            "search_endpoint": search_endpoint,
            "knowledge_base": knowledge_base,
            "api_key_enc": api_key_enc,
        },
    )
    return {
        "id": connector_id, "tenant_id": tenant_id, "name": name, "type": "foundry_iq",
        "group_id": group_id, "url": search_endpoint, "status": "never_synced", "last_synced_at": None,
        "last_error": None, "content_hash": None, "source_authority": 0,
        "foundry_iq_knowledge_base": knowledge_base,
    }


def find_foundry_iq_config_for_group(
    tenant_id: str, group_id: str, repo: Optional[GraphRepository] = None
) -> Optional[dict]:
    """The per-query lookup execute_context_query uses to build a
    FoundryIQRetriever scoped to whichever knowledge base is actually being
    queried, instead of one global deployment-wide config -- a tenant
    configures this once, through the UI, per knowledge base, the same way
    every other connector is configured. Returns None (not an error) when
    no foundry_iq connector exists for this group_id -- the caller falls
    back to the deployment-wide FOUNDRY_IQ_* env vars, if any (see
    query_service.py). If more than one exists for the same group_id
    (nothing prevents that today), the most recently created one wins --
    same "first/most-recent match" tiebreak this codebase already uses
    elsewhere rather than erroring on an edge case nothing depends on."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        """
        MATCH (c:Connector {tenant_id: $tenant_id, group_id: $group_id, type: 'foundry_iq'})
        RETURN c.url AS search_endpoint, c.foundry_iq_knowledge_base AS knowledge_base,
               c.foundry_iq_api_key_enc AS api_key_enc
        ORDER BY c.created_at DESC
        LIMIT 1
        """,
        {"tenant_id": tenant_id, "group_id": group_id},
    )
    return rows[0] if rows else None


def get_foundry_iq_credential(
    tenant_id: str, connector_id: str, repo: Optional[GraphRepository] = None
) -> Optional[dict]:
    """The by-id counterpart to find_foundry_iq_config_for_group above --
    used by the "Sync now" connectivity check (app/api/connectors.py's
    _enqueue_sync), which already has this exact connector's own id and
    needs to test THIS connector's credential, not "whichever foundry_iq
    connector happens to be most recent for this group_id" (the group_id
    lookup's own documented tiebreak, right for query time, wrong here)."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        """
        MATCH (c:Connector {id: $id, tenant_id: $tenant_id, type: 'foundry_iq'})
        RETURN c.url AS search_endpoint, c.foundry_iq_knowledge_base AS knowledge_base,
               c.foundry_iq_api_key_enc AS api_key_enc
        """,
        {"id": connector_id, "tenant_id": tenant_id},
    )
    return rows[0] if rows else None


def get_oauth_refresh_token(tenant_id: str, connector_id: str, repo: Optional[GraphRepository] = None) -> Optional[str]:
    """Returns the connector's still-encrypted refresh token (see
    app/graph/token_crypto.py for decrypting it) -- deliberately a separate
    query from get_connector()/list_connectors() above rather than a field
    in _FIELDS, so a live credential never flows through the same code path
    that builds an HTTP API response, even by accident in a future change
    to _serialize()."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        "MATCH (c:Connector {id: $id, tenant_id: $tenant_id}) RETURN c.oauth_refresh_token_enc AS token",
        {"id": connector_id, "tenant_id": tenant_id},
    )
    if not rows:
        return None
    return rows[0]["token"]


def authority_by_group_id(tenant_id: str, repo: Optional[GraphRepository] = None) -> dict[str, int]:
    """Maps each of this tenant's own group_ids (knowledge bases) to the
    highest source_authority among the connectors feeding it -- more than
    one connector can write into the same knowledge base, so this takes the
    max rather than assuming a 1:1 connector-to-group_id mapping. A
    group_id with no connectors (or only ones with no authority set) is
    just absent from the returned map; callers should treat a missing
    entry as authority 0, same as an explicitly-unset connector."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        "MATCH (c:Connector {tenant_id: $tenant_id}) "
        "RETURN c.group_id AS group_id, max(coalesce(c.source_authority, 0)) AS authority",
        {"tenant_id": tenant_id},
    )
    return {row["group_id"]: row["authority"] for row in rows}


def purge_connector_data(connector_id: str, group_id: str, repo: Optional[GraphRepository] = None) -> dict:
    """Removes everything a *specific connector's own syncs* wrote -- not
    the connector row itself (see delete_connector for that) -- using the
    same Episodic.connector_id tag app/ingestion/connector_sync.py writes and
    app/api/graph.py's ?connector_id= filter reads. Exists for recovering
    from a bad sync (wrong data uploaded, a sync that partially completed
    before an error, content that shouldn't have landed) without wiping the
    whole knowledge base or leaving the connector permanently tainted --
    the connector keeps its config (url/name/group_id) and can be synced
    again immediately after this, clean.

    A RELATES_TO fact is only ever fully deleted when *every* episode that
    touched it belongs to this connector -- a fact another sync (this
    connector's own earlier run, or a different connector entirely) also
    contributed to keeps existing, just with this connector's episode
    uuid(s) stripped from its `episodes` list. An Entity is only deleted if
    it ends up with zero remaining RELATES_TO edges once that's done --
    never one still backing a fact from elsewhere. This mirrors
    app/context/orchestrator.py's own "never hide or filter a fact that's
    still real" posture, applied to deletion instead of ranking."""
    repo = repo or GraphRepository()
    edge_result = repo.execute_cypher(
        """
        MATCH (ep:Episodic {group_id: $group_id, connector_id: $connector_id})
        WITH collect(ep.uuid) AS connector_episode_uuids
        MATCH (a:Entity {group_id: $group_id})-[r:RELATES_TO]->(b:Entity {group_id: $group_id})
        WHERE ANY(e IN r.episodes WHERE e IN connector_episode_uuids)
        WITH r, connector_episode_uuids, [e IN r.episodes WHERE NOT e IN connector_episode_uuids] AS remaining
        WITH r, remaining, size(remaining) = 0 AS fully_owned
        FOREACH (_ IN CASE WHEN fully_owned THEN [1] ELSE [] END | DELETE r)
        FOREACH (_ IN CASE WHEN NOT fully_owned THEN [1] ELSE [] END | SET r.episodes = remaining)
        RETURN count(r) AS touched, sum(CASE WHEN fully_owned THEN 1 ELSE 0 END) AS deleted
        """,
        {"group_id": group_id, "connector_id": connector_id},
    )
    node_result = repo.execute_cypher(
        """
        MATCH (ep:Episodic {group_id: $group_id, connector_id: $connector_id})-[:MENTIONS]->(n:Entity)
        WHERE NOT (n)-[:RELATES_TO]-()
        WITH DISTINCT n
        DETACH DELETE n
        RETURN count(n) AS deleted
        """,
        {"group_id": group_id, "connector_id": connector_id},
    )
    episode_result = repo.execute_cypher(
        "MATCH (ep:Episodic {group_id: $group_id, connector_id: $connector_id}) "
        "DETACH DELETE ep RETURN count(ep) AS deleted",
        {"group_id": group_id, "connector_id": connector_id},
    )
    # Also resets the connector's own sync bookkeeping -- content_hash in
    # particular has to go back to null, or the next sync sees "identical
    # content to last time" (the hash was computed over the exact files
    # this purge just undid) and skips re-ingesting via
    # run_connector_sync's own dedup check, silently leaving the connector
    # empty even after a real "Sync now". record_sync_result() (above)
    # deliberately doesn't do this -- it only ever *sets* a hash on an
    # actual synced outcome, never clears one -- so this is a direct SET
    # rather than reusing it with a fabricated status.
    repo.execute_cypher(
        "MATCH (c:Connector {id: $connector_id}) "
        "SET c.status = 'never_synced', c.content_hash = null, c.last_error = null, c.last_synced_at = null",
        {"connector_id": connector_id},
    )
    return {
        "facts_deleted": (edge_result[0]["deleted"] if edge_result else 0) or 0,
        "facts_detached": (edge_result[0]["touched"] - (edge_result[0]["deleted"] or 0)) if edge_result else 0,
        "entities_deleted": node_result[0]["deleted"] if node_result else 0,
        "episodes_deleted": episode_result[0]["deleted"] if episode_result else 0,
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
