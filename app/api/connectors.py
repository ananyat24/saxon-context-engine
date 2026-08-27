# Connectors: configure a link to an external data source, then sync it to
# pull its content into one of the tenant's own knowledge bases -- see
# app/graph/connectors.py for the storage layer and _CONNECTOR_FACTORIES
# below for the connector types implemented so far.
#
# This route is the manual "Sync now" trigger; app/graph/connector_scheduler.py
# runs the same sync automatically on an interval, via the shared
# run_connector_sync() in app/ingestion/connector_sync.py -- this route is a
# thin HTTP wrapper around that (look up the connector, call it, translate
# the result into an HTTP response), not a second implementation of the
# fetch -> dedup-check -> ingest sequence.
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import TenantConfig, settings
from app.graph import connectors
from app.graph.graph_repository import GraphRepository
from app.ingestion.connector_base import ConnectorFetchError, SourceConnector
from app.ingestion.connector_sync import run_connector_sync
from app.ingestion.database_source import DatabaseConnector
from app.ingestion.document_source import DocumentConnector
from app.ingestion.email_source import EmailConnector
from app.ingestion.gmail_source import GmailConnector
from app.ingestion.google_drive_source import GoogleDriveConnector
from app.ingestion.outlook_mail_source import OutlookMailConnector
from app.ingestion.sharepoint_source import SharePointConnector
from app.ingestion.web_source import WebConnector
from app.security import require_tenant

router = APIRouter()

# The one place a connector `type` maps to its SourceConnector implementation.
# Adding a new type (another real live source, once credentials exist) is:
# implement SourceConnector (see app/ingestion/connector_base.py) and add one
# entry here -- no changes needed to the sync route below, IngestionPipeline,
# or ontology handling. "database"/"email" read bundled mock data rather than
# a live source (see each module's docstring) -- "database" stands in for a
# CRM/DB until one's wired up, and "email" is the from-scratch fallback for a
# mailbox that isn't Gmail or Microsoft 365. Every other type below is a real
# live source: "web" (any URL), "google_drive"/"sharepoint" (live document
# stores), and "gmail"/"outlook_mail" (live mailboxes).
_CONNECTOR_FACTORIES: dict[str, Callable[[dict], SourceConnector]] = {
    "web": lambda connector: WebConnector(connector["url"]),
    "database": lambda connector: DatabaseConnector(),
    "documents": lambda connector: DocumentConnector(),
    "email": lambda connector: EmailConnector(),
    "google_drive": lambda connector: GoogleDriveConnector(connector["url"]),
    "sharepoint": lambda connector: SharePointConnector(connector["url"]),
    "gmail": lambda connector: GmailConnector(connector["url"]),
    "outlook_mail": lambda connector: OutlookMailConnector(connector["url"]),
}

# Which types read from a tenant-supplied address (a URL/site link/mailbox)
# vs. a fixed bundled sample with nothing to collect (see _CONNECTOR_FACTORIES
# above) or an operator-wide credential with no per-connector address at all.
_TYPES_REQUIRING_URL = {"web", "google_drive", "sharepoint", "gmail", "outlook_mail"}

# Real live connector types need an operator-wide credential configured
# before a tenant can even create one -- checked here so that's a clear 400
# at creation time, not a confusing failure the first time someone clicks
# "Sync now". Each check function takes no arguments and returns whether
# that type's required setting(s) are present. gmail/outlook_mail reuse the
# same operator credentials as google_drive/sharepoint respectively (see
# app/ingestion/gmail_source.py and app/ingestion/outlook_mail_source.py for
# why) -- the extra Graph/Workspace permission each needs on top of that
# shared credential can't be checked from here, only at sync time.
_OPERATOR_CONFIG_CHECKS: dict[str, Callable[[], bool]] = {
    "google_drive": lambda: bool(settings.google_drive_service_account_json),
    "gmail": lambda: bool(settings.google_drive_service_account_json),
    "sharepoint": lambda: bool(
        settings.sharepoint_tenant_id and settings.sharepoint_client_id and settings.sharepoint_client_secret
    ),
    "outlook_mail": lambda: bool(
        settings.sharepoint_tenant_id and settings.sharepoint_client_id and settings.sharepoint_client_secret
    ),
}


class CreateConnectorRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = "web"
    # Which of the tenant's own knowledge bases a sync writes into -- must be
    # one the tenant already has (see app/security.py's resolve_knowledge_base
    # for the same boundary applied elsewhere), not an arbitrary new value.
    group_id: str
    url: Optional[str] = Field(default=None, max_length=2000)


# A connector past this many sync intervals since its last success is
# flagged "stale" rather than "ok" -- generous enough to absorb a transient
# failure or two without crying wolf, but still catches "the background
# scheduler stopped running for this connector" (see
# app/graph/connector_scheduler.py), which nothing else surfaces.
_STALE_AFTER_INTERVAL_MULTIPLE = 3


def _connector_health(c: dict) -> str:
    """"error" | "never_synced" | "queued" | "stale" | "ok" -- a coarse
    freshness signal computed server-side so the staleness threshold lives
    in one place rather than being duplicated in the frontend."""
    if c["status"] == "error":
        return "error"
    if c["status"] == "queued":
        # A sync was just accepted onto the ingestion queue (see
        # app/graph/ingestion_queue.py) and hasn't run yet -- not stale
        # (it's about to get fresher, not gone quiet), not "ok" either
        # (nothing new has actually landed since it was triggered).
        return "queued"
    if not c["last_synced_at"]:
        return "never_synced"
    last_synced_at = c["last_synced_at"]
    if isinstance(last_synced_at, str):
        last_synced_at = datetime.fromisoformat(last_synced_at.replace("Z", "+00:00"))
    max_age = timedelta(minutes=settings.connector_sync_interval_minutes * _STALE_AFTER_INTERVAL_MULTIPLE)
    if datetime.now(timezone.utc) - last_synced_at > max_age:
        return "stale"
    return "ok"


def _serialize(c: dict) -> dict:
    return {
        "id": c["id"],
        "name": c["name"],
        "type": c["type"],
        "group_id": c["group_id"],
        "url": c["url"],
        "status": c["status"],
        "last_synced_at": c["last_synced_at"],
        "last_error": c["last_error"],
        "health": _connector_health(c),
        "push_enabled": bool(c.get("push_subscription_id")),
    }


# Connector types real-time push (app/ingestion/graph_subscriptions.py) is
# wired up for -- see that module's docstring for why sharepoint isn't
# included yet. Attempting this for a type not in here is simply skipped.
_PUSH_CAPABLE_TYPES = {"outlook_mail"}


async def _try_enable_push(tenant: TenantConfig, connector: dict, mailbox: str, repo: GraphRepository) -> None:
    """Best-effort: a failure here (missing PUBLIC_BASE_URL, Graph
    rejecting the subscription, a network error) leaves the connector
    exactly as usable as it was before push existed -- polled on the
    normal interval -- so it's caught and logged, never raised to the
    caller. Only called for a type in _PUSH_CAPABLE_TYPES."""
    if not settings.public_base_url:
        return
    from app.ingestion.graph_subscriptions import create_mail_subscription, new_client_state

    client_state = new_client_state()
    notification_url = f"{settings.public_base_url}/api/v1/webhooks/graph"
    try:
        subscription_id, expires_at = await create_mail_subscription(mailbox, notification_url, client_state)
    except ConnectorFetchError as e:
        logging.getLogger(__name__).warning(
            f"Could not enable push for connector '{connector['id']}' ({tenant.tenant_id}): {e}"
        )
        return
    connectors.set_push_subscription(
        tenant.tenant_id, connector["id"], subscription_id=subscription_id, client_state=client_state,
        expires_at=expires_at, repo=repo,
    )


@router.get("")
def list_connectors(request: Request, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    return [_serialize(c) for c in connectors.list_connectors(tenant.tenant_id, repo=repo)]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_connector(
    req: CreateConnectorRequest, request: Request, tenant: TenantConfig = Depends(require_tenant)
):
    if req.type not in _CONNECTOR_FACTORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported connector type '{req.type}'. Supported: {', '.join(sorted(_CONNECTOR_FACTORIES))}.",
        )
    if req.group_id not in tenant.knowledge_base_ids():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown knowledge base '{req.group_id}' for this tenant.",
        )

    config_check = _OPERATOR_CONFIG_CHECKS.get(req.type)
    if config_check is not None and not config_check():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{req.type}' isn't configured on this server yet -- ask your operator to set it up.",
        )

    if req.type in _TYPES_REQUIRING_URL:
        url = (req.url or "").strip()
        if not url:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This connector type needs a URL.")
        try:
            _CONNECTOR_FACTORIES[req.type]({"url": url})  # validates the URL/id shape up front
        except ConnectorFetchError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    else:
        # Ignore any submitted url for a bundled-mock-data type -- store the
        # connector's own fixed description instead, both so the table shows
        # something meaningful and so a tenant-supplied value never has any
        # path to influence what gets read from disk.
        url = _CONNECTOR_FACTORIES[req.type]({}).source_description()

    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    created = connectors.create_connector(
        tenant.tenant_id, req.name.strip(), req.type, req.group_id, url, repo=repo
    )
    if req.type in _PUSH_CAPABLE_TYPES:
        await _try_enable_push(tenant, created, url, repo)
        created = connectors.get_connector(tenant.tenant_id, created["id"], repo=repo)
    return _serialize(created)


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connector(connector_id: str, request: Request, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    connector = connectors.get_connector(tenant.tenant_id, connector_id, repo=repo)
    if connector is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")
    if connector.get("push_subscription_id"):
        from app.ingestion.graph_subscriptions import delete_subscription

        await delete_subscription(connector["push_subscription_id"])
    connectors.delete_connector(tenant.tenant_id, connector_id, repo=repo)


@router.post("/{connector_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_connector(connector_id: str, request: Request, tenant: TenantConfig = Depends(require_tenant)):
    """Accepts the sync onto the in-process ingestion queue (see
    app/graph/ingestion_queue.py) and returns immediately -- the actual
    fetch + extraction runs in the background, not in this request. A
    large Drive/SharePoint folder can mean many extraction calls; blocking
    this HTTP request on all of them (the old behavior) meant a slow sync
    was also a slow, easy-to-time-out API call for no real benefit.

    This means a failure (including a spend-cap hit) can no longer be
    reported in this response the way it used to be -- there's nothing left
    to report it to once the job runs after the response is already sent.
    Poll GET /connectors and check status/last_error instead; that's the
    same place a scheduled sync's outcome has always had to be checked
    (see app/graph/connector_scheduler.py), so this just makes the manual
    and scheduled paths consistent instead of the manual one being special.
    """
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    connector = connectors.get_connector(tenant.tenant_id, connector_id, repo=repo)
    if connector is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")

    # _CONNECTOR_FACTORIES is the one dispatch point -- every type from here
    # down is handled generically via the SourceConnector interface.
    factory = _CONNECTOR_FACTORIES[connector["type"]]
    connectors.mark_sync_queued(tenant.tenant_id, connector_id, repo=repo)

    async def _job() -> None:
        # A fresh GraphRepository, not the request-scoped `repo` above --
        # this closure outlives the request it was created in (that's the
        # whole point), so it needs its own; the Neo4jClient itself is a
        # driver-level connection pool, safe to share across both.
        await run_connector_sync(tenant, connector, factory, repo=GraphRepository(neo4j_client=request.app.state.neo4j_client))

    await request.app.state.ingestion_queue.enqueue(_job)
    return {"queued": True}
