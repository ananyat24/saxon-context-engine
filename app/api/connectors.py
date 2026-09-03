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
from pathlib import Path
from typing import Callable, Optional

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field

from app.config import TenantConfig, settings
from app.context.response_cache import get_response_cache
from app.graph import connectors
from app.graph.graph_repository import GraphRepository
from app.graph.token_crypto import TokenEncryptionNotConfigured, decrypt_token, encrypt_token
from app.ingestion.connector_base import ConnectorFetchError, SourceConnector
from app.ingestion.connector_sync import run_connector_sync
from app.ingestion.database_source import DatabaseConnector, connector_upload_dir
from app.ingestion.document_source import DocumentConnector
from app.ingestion.email_source import EmailConnector
from app.ingestion.gmail_source import GmailConnector
# Reused as the Picker's own selection cap (GoogleOAuthFilesRequest below) --
# picking more than google_drive_source.py will actually read per sync would
# just be silently ignored later, so it's rejected up front with a clear reason.
from app.ingestion.google_drive_source import _MAX_FILES as _MAX_FILES_PER_OAUTH_CONNECTOR
from app.ingestion.google_drive_source import GoogleDriveConnector, GoogleDriveOAuthConnector
from app.ingestion.google_oauth import exchange_code, refresh_access_token, revoke_token
from app.ingestion.outlook_mail_source import OutlookMailConnector
from app.ingestion.sharepoint_source import SharePointConnector
from app.ingestion.web_source import WebConnector
from app.retrieval.foundry_iq_retriever import FoundryIQRetriever
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
    "database": lambda connector: DatabaseConnector(connector.get("id", "")),
    "documents": lambda connector: DocumentConnector(),
    "email": lambda connector: EmailConnector(
        connector.get("id", ""), source_label=connector.get("name") or "Email"
    ),
    "google_drive": lambda connector: GoogleDriveConnector(connector["url"]),
    "google_drive_oauth": lambda connector: GoogleDriveOAuthConnector(
        connector.get("oauth_file_ids") or [], connector["tenant_id"], connector["id"]
    ),
    "sharepoint": lambda connector: SharePointConnector(connector["url"]),
    "gmail": lambda connector: GmailConnector(connector["url"]),
    "outlook_mail": lambda connector: OutlookMailConnector(connector["url"]),
}

# Which types read from a tenant-supplied address (a URL/site link/mailbox)
# vs. a fixed bundled sample with nothing to collect (see _CONNECTOR_FACTORIES
# above) or an operator-wide credential with no per-connector address at all.
# "google_drive_oauth" isn't here either -- it's never created through the
# generic POST /connectors route below at all (see _OAUTH_ONLY_TYPES), so it
# has no "supply a URL up front" step to require.
_TYPES_REQUIRING_URL = {"web", "google_drive", "sharepoint", "gmail", "outlook_mail"}

# Created only via the dedicated OAuth connect flow (oauth/exchange +
# oauth/files below), never via the generic "New source connector" form --
# there's no URL/credential for a tenant to type in, the whole point is that
# the consent popup + file picker replace that. Rejected explicitly in the
# generic create route with a message pointing at the real flow, rather than
# just failing _TYPES_REQUIRING_URL's "needs a URL" check with a confusing
# error.
_OAUTH_ONLY_TYPES = {"google_drive_oauth"}

# "foundry_iq" (app/retrieval/foundry_iq_retriever.py) is a live, per-query
# retriever, not an ingestion connector -- it never implements
# SourceConnector/fetch(), so it's deliberately never added to
# _CONNECTOR_FACTORIES above. Handled as its own branch everywhere that dict
# would otherwise be the single dispatch point: creation (below, its own
# validation/storage instead of the generic URL-connector path) and "Sync
# now" (_enqueue_sync, a live connectivity check instead of
# run_connector_sync). One retriever covers Fabric IQ and Work IQ too --
# see CLAUDE.md's v7 section for why.
_RETRIEVER_ONLY_TYPES = {"foundry_iq"}

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
    "google_drive_oauth": lambda: bool(
        settings.google_oauth_client_id and settings.google_oauth_client_secret and settings.token_encryption_key
    ),
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
    # Higher = more authoritative -- only used to break ties when two
    # connectors' facts disagree about the same relationship at the same
    # point in time (see app/context/orchestrator.py). Never hides or
    # filters a fact; every source's own facts stay visible regardless of
    # rank. 0 (the default) means "no special standing".
    source_authority: int = Field(default=0, ge=0, le=100)
    # Only used when type == "foundry_iq" (see _RETRIEVER_ONLY_TYPES below)
    # -- `url` above doubles as the Azure AI Search endpoint for this type,
    # same "the address field is the address field" convention every other
    # connector already follows.
    foundry_iq_api_key: Optional[str] = Field(default=None, max_length=500)
    foundry_iq_knowledge_base: Optional[str] = Field(default=None, max_length=200)


# A connector past this many sync intervals since its last success is
# flagged "stale" rather than "ok" -- generous enough to absorb a transient
# failure or two without crying wolf, but still catches "the background
# scheduler stopped running for this connector" (see
# app/graph/connector_scheduler.py), which nothing else surfaces.
_STALE_AFTER_INTERVAL_MULTIPLE = 3


def _connector_health(c: dict) -> str:
    """"error" | "never_synced" | "queued" | "stale" | "ok" |
    "authorized_needs_files" -- a coarse freshness signal computed
    server-side so the staleness threshold lives in one place rather than
    being duplicated in the frontend."""
    if c["status"] == "authorized_needs_files":
        # A google_drive_oauth connector whose consent popup succeeded but
        # whose file picker was never finished (closed early, or the tab
        # was closed) -- distinct from "never_synced" so the frontend can
        # offer "finish connecting" instead of a "Sync now" that has
        # nothing to sync yet.
        return "authorized_needs_files"
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
    # Deliberately never includes oauth_refresh_token_enc or the raw
    # oauth_file_ids list -- see app/graph/connectors.py's
    # get_oauth_refresh_token docstring for why the encrypted token has its
    # own query path instead of living in _FIELDS at all. file_ids aren't
    # secret, just not needed by the UI once picked (the connector's own
    # name/status already reflect them).
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
        "source_authority": c.get("source_authority") or 0,
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
    if req.type not in _CONNECTOR_FACTORIES and req.type not in _RETRIEVER_ONLY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported connector type '{req.type}'. "
                f"Supported: {', '.join(sorted(_CONNECTOR_FACTORIES | _RETRIEVER_ONLY_TYPES))}."
            ),
        )
    if req.type in _OAUTH_ONLY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{req.type}' connectors are created by clicking its \"Connect\" button, not this form.",
        )
    if req.group_id not in tenant.knowledge_base_ids():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown knowledge base '{req.group_id}' for this tenant.",
        )

    if req.type in _RETRIEVER_ONLY_TYPES:
        return _create_foundry_iq_connector(req, request, tenant)

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
        tenant.tenant_id, req.name.strip(), req.type, req.group_id, url, repo=repo,
        source_authority=req.source_authority,
    )
    if req.type in _PUSH_CAPABLE_TYPES:
        await _try_enable_push(tenant, created, url, repo)
        created = connectors.get_connector(tenant.tenant_id, created["id"], repo=repo)
    return _serialize(created)


def _create_foundry_iq_connector(req: CreateConnectorRequest, request: Request, tenant: TenantConfig) -> dict:
    """The foundry_iq branch of create_connector above -- its own
    validation and storage instead of the generic SourceConnector-URL path,
    since this type has three required fields (endpoint, api key,
    knowledge base name) instead of one URL and stores an encrypted
    credential instead of nothing secret at all. Synchronous (no network
    call here -- the actual Foundry IQ connectivity check happens on
    "Sync now", see _enqueue_sync below) so it's easy to call directly from
    the async route without an extra await that does nothing."""
    search_endpoint = (req.url or "").strip()
    knowledge_base = (req.foundry_iq_knowledge_base or "").strip()
    api_key = (req.foundry_iq_api_key or "").strip()
    missing = [
        label for label, value in (
            ("an Azure AI Search endpoint (the URL field)", search_endpoint),
            ("a knowledge base name", knowledge_base),
            ("an API key", api_key),
        ) if not value
    ]
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"foundry_iq needs {', and '.join(missing)}.")

    try:
        api_key_enc = encrypt_token(api_key)
    except TokenEncryptionNotConfigured as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    created = connectors.create_foundry_iq_connector(
        tenant.tenant_id, req.name.strip(), req.group_id, search_endpoint, knowledge_base, api_key_enc, repo=repo,
    )
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
    if connector["type"] == "google_drive_oauth":
        # Deleting the connector here should also mean "and Saxon can no
        # longer read anything in your Drive" at Google's end, not just
        # "Saxon stopped keeping its own pointer to it" -- otherwise the
        # grant would sit live in the user's Google Account forever with
        # nothing in this app referencing it. Best-effort: an unreachable
        # or already-invalid token shouldn't block deleting the connector
        # itself (see revoke_token's own docstring).
        encrypted = connectors.get_oauth_refresh_token(tenant.tenant_id, connector_id, repo=repo)
        if encrypted:
            try:
                await revoke_token(decrypt_token(encrypted))
            except Exception as e:
                logging.getLogger(__name__).warning(
                    f"Could not revoke Google Drive grant for connector '{connector_id}' ({tenant.tenant_id}): {e}"
                )
    connectors.delete_connector(tenant.tenant_id, connector_id, repo=repo)


@router.delete("/{connector_id}/data")
def purge_connector_data(connector_id: str, request: Request, tenant: TenantConfig = Depends(require_tenant)):
    """Undoes what this connector's own syncs wrote to the graph -- not the
    connector itself, which keeps its config and can be synced again right
    after this, clean (see app/graph/connectors.py's purge_connector_data
    for the exact fact/entity/episode semantics). For recovering from a bad
    sync: wrong data uploaded before a fix, a sync that partially completed
    before erroring out, content that shouldn't have landed -- without
    wiping the rest of the knowledge base or leaving the connector
    permanently unable to produce a clean sync again."""
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    connector = connectors.get_connector(tenant.tenant_id, connector_id, repo=repo)
    if connector is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")
    result = connectors.purge_connector_data(connector_id, connector["group_id"], repo=repo)
    # A purge invalidates whatever the response cache last knew about this
    # group -- same reasoning as a real sync (see connector_sync.py).
    get_response_cache().invalidate_group(tenant.tenant_id, connector["group_id"])
    return result


# Generous enough for a real CSV export, small enough that an upload can't
# be used to fill up disk -- this app has no per-tenant storage quota
# elsewhere, so a fixed cap here is the only thing bounding it.
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@router.post("/{connector_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_connector_file(
    connector_id: str, file: UploadFile, request: Request, tenant: TenantConfig = Depends(require_tenant)
):
    """Drops a file into this connector's own upload folder (see
    app/ingestion/database_source.py's DatabaseConnector and
    app/ingestion/email_source.py's EmailConnector) -- a plain "Sync now"
    afterward ingests it, the same as any other connector type. "database"
    accepts a .csv (one file per record type); "email" accepts a .json
    array export ({from, to, subject, date, body} objects -- what a Gmail/
    Outlook data export or a small script hitting either API would
    reasonably produce); every other type is still the bundled-mock-data
    placeholder CLAUDE.md's v1 note describes.

    The uploaded filename never becomes a filesystem path as given -- only
    its basename (Path(...).name, which drops any "../" or directory
    component) is used, and only under this connector's own id-derived
    folder, so there's no way a client-supplied name can write outside it.
    """
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    connector = connectors.get_connector(tenant.tenant_id, connector_id, repo=repo)
    if connector is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")
    allowed_extension = {"database": ".csv", "email": ".json"}.get(connector["type"])
    if allowed_extension is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File upload is only supported for Database/CRM and Email connectors.",
        )

    filename = Path(file.filename or "").name
    if not filename or filename in (".", "..") or not filename.lower().endswith(allowed_extension):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Only a named {allowed_extension} file is accepted."
        )

    body = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(body) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"File exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB limit.",
        )

    upload_dir = connector_upload_dir(connector_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    (upload_dir / filename).write_bytes(body)
    return {"filename": filename, "size": len(body)}


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

    await _enqueue_sync(tenant, connector, request, repo)
    return {"queued": True}


async def _enqueue_sync(tenant: TenantConfig, connector: dict, request: Request, repo: GraphRepository) -> None:
    """The actual "accept this connector's sync onto the ingestion queue"
    logic -- shared by the manual /sync route above and the OAuth
    oauth/files route below, which also wants to kick off a first sync the
    moment a user finishes picking files, without duplicating the queue
    plumbing. _CONNECTOR_FACTORIES is the one dispatch point -- every type
    from here down is handled generically via the SourceConnector interface."""
    connectors.mark_sync_queued(tenant.tenant_id, connector["id"], repo=repo)

    if connector["type"] in _RETRIEVER_ONLY_TYPES:
        async def _job() -> None:
            await _check_foundry_iq_connectivity(
                tenant, connector, repo=GraphRepository(neo4j_client=request.app.state.neo4j_client)
            )

        await request.app.state.ingestion_queue.enqueue(_job)
        return

    factory = _CONNECTOR_FACTORIES[connector["type"]]

    async def _job() -> None:
        # A fresh GraphRepository, not the request-scoped `repo` above --
        # this closure outlives the request it was created in (that's the
        # whole point), so it needs its own; the Neo4jClient itself is a
        # driver-level connection pool, safe to share across both.
        await run_connector_sync(tenant, connector, factory, repo=GraphRepository(neo4j_client=request.app.state.neo4j_client))

    await request.app.state.ingestion_queue.enqueue(_job)


async def _check_foundry_iq_connectivity(tenant: TenantConfig, connector: dict, repo: GraphRepository) -> None:
    """"Sync now" for a foundry_iq connector -- there's nothing to ingest
    (see app/retrieval/foundry_iq_retriever.py's module docstring), so this
    is a real, live connectivity check instead: decrypt the stored
    credential, run one trivial retrieval, and record whether it actually
    reached the knowledge base. Never raises -- same "every failure mode
    reported back via record_sync_result, not an exception the caller has
    to handle" contract run_connector_sync already follows."""
    credential = connectors.get_foundry_iq_credential(tenant.tenant_id, connector["id"], repo=repo)
    if credential is None or not credential.get("api_key_enc"):
        connectors.record_sync_result(
            tenant.tenant_id, connector["id"], status="error", last_error="Connector configuration is missing.", repo=repo
        )
        return
    try:
        api_key = decrypt_token(credential["api_key_enc"])
    except InvalidToken:
        connectors.record_sync_result(
            tenant.tenant_id, connector["id"], status="error",
            last_error="Stored credential could not be decrypted -- it may have been saved under a different "
            "TOKEN_ENCRYPTION_KEY. Delete and recreate this connector.",
            repo=repo,
        )
        return

    retriever = FoundryIQRetriever(
        search_endpoint=credential["search_endpoint"], api_key=api_key, knowledge_base=credential["knowledge_base"],
    )
    facts = await retriever.retrieve("connectivity check", num_results=1)
    # retrieve() never raises (see its own docstring) -- an unreachable
    # endpoint or bad credential comes back as an empty list, same shape as
    # "reached the knowledge base but it genuinely has nothing to say about
    # this query" would. Good enough for "is this configured correctly"
    # (a real misconfiguration -- wrong endpoint, wrong key, wrong
    # knowledge base name -- fails on every query, not just this one), not
    # precise enough to distinguish those two cases from each other; that
    # would need retrieve() to surface its own error detail instead of
    # swallowing it, real follow-up scope if this check's accuracy turns
    # out to matter more than "reachable at all".
    if facts:
        connectors.record_sync_result(tenant.tenant_id, connector["id"], status="synced", repo=repo)
    else:
        connectors.record_sync_result(
            tenant.tenant_id, connector["id"], status="error",
            last_error="Could not retrieve anything from this knowledge base -- check the endpoint, API key, and "
            "knowledge base name.",
            repo=repo,
        )


# --- Google Drive one-click connect (see app/ingestion/google_oauth.py and
# app/ingestion/google_drive_source.py's GoogleDriveOAuthConnector) ---------


class GoogleOAuthExchangeRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    group_id: str
    # The one-time authorization code from the browser's Google Identity
    # Services popup (see frontend/app.js) -- never a token of any kind
    # itself, so there's nothing sensitive in this request body beyond what
    # the browser already had to send Google to get it.
    code: str = Field(min_length=1, max_length=2000)


class GoogleOAuthFilesRequest(BaseModel):
    # What the user picked in the Google Picker -- file ids only carry
    # meaning inside that one Drive account's own OAuth grant, not a secret
    # on their own.
    file_ids: list[str] = Field(min_length=1, max_length=_MAX_FILES_PER_OAUTH_CONNECTOR)
    file_names: list[str] = Field(default_factory=list, max_length=_MAX_FILES_PER_OAUTH_CONNECTOR)


@router.get("/oauth/providers")
def oauth_providers(tenant: TenantConfig = Depends(require_tenant)):
    """Tells the frontend whether the one-click Drive button should even be
    shown, and hands it the OAuth client id it needs to launch Google's
    consent popup -- a client id is meant to be public (it identifies the
    app, not a secret; see Google's own OAuth docs), unlike client_secret,
    which never leaves this module."""
    available = bool(settings.google_oauth_client_id and settings.google_oauth_client_secret and settings.token_encryption_key)
    return {
        "google_drive": {
            "available": available,
            "client_id": settings.google_oauth_client_id if available else None,
        }
    }


@router.post("/google/oauth/exchange", status_code=status.HTTP_201_CREATED)
async def google_oauth_exchange(
    req: GoogleOAuthExchangeRequest, request: Request, tenant: TenantConfig = Depends(require_tenant)
):
    """Step 1 of the one-click connect: the browser already ran Google's
    consent popup and got back a one-time authorization code (see
    frontend/app.js) -- this trades it server-side for real tokens, stores
    the refresh token encrypted, and creates the connector in
    'authorized_needs_files' state. Returns a short-lived access_token
    too, but only so the *same* browser can open the Google Picker
    immediately afterward (step 2, oauth/files below) -- it's handed back
    once, never persisted or logged, and expires on its own within the
    hour even if the picker step is never finished."""
    if not _OPERATOR_CONFIG_CHECKS["google_drive_oauth"]():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google Drive one-click connect isn't configured on this server yet.",
        )
    if req.group_id not in tenant.knowledge_base_ids():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown knowledge base '{req.group_id}' for this tenant.",
        )

    try:
        tokens = await exchange_code(req.code)
    except ConnectorFetchError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    try:
        encrypted_refresh_token = encrypt_token(tokens["refresh_token"])
    except TokenEncryptionNotConfigured as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    created = connectors.create_oauth_pending_connector(
        tenant.tenant_id, req.name.strip(), req.group_id, encrypted_refresh_token, repo=repo
    )
    return {**_serialize(created), "access_token": tokens["access_token"]}


@router.get("/{connector_id}/oauth/access-token")
async def google_oauth_resume_access_token(
    connector_id: str, request: Request, tenant: TenantConfig = Depends(require_tenant)
):
    """Lets the frontend resume a connect flow that was interrupted before
    the Picker step finished (the browser tab closed, the popup was
    dismissed) without asking the user to sign into Google again -- mints a
    fresh, short-lived access token from the refresh token already stored
    from step 1. Only works while the connector is still
    'authorized_needs_files'; once files are picked, re-picking means
    reconnecting from scratch (see finalize_oauth_files's own docstring),
    so this deliberately doesn't offer a "change my files" path for an
    already-active connector."""
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    connector = connectors.get_connector(tenant.tenant_id, connector_id, repo=repo)
    if connector is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")
    if connector["type"] != "google_drive_oauth" or connector["status"] != "authorized_needs_files":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This connector isn't awaiting file selection.",
        )
    encrypted = connectors.get_oauth_refresh_token(tenant.tenant_id, connector_id, repo=repo)
    if not encrypted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No stored Drive connection found.")
    try:
        refresh_token = decrypt_token(encrypted)
    except TokenEncryptionNotConfigured as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except InvalidToken:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This Drive connection can't be decrypted -- delete it and connect again.",
        )
    try:
        access_token = await refresh_access_token(refresh_token)
    except ConnectorFetchError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {"access_token": access_token}


@router.post("/{connector_id}/oauth/files", status_code=status.HTTP_200_OK)
async def google_oauth_finalize_files(
    connector_id: str, req: GoogleOAuthFilesRequest, request: Request, tenant: TenantConfig = Depends(require_tenant)
):
    """Step 2: the user picked files in the Google Picker (see
    frontend/app.js) -- records which ones, flips the connector out of
    'authorized_needs_files', and immediately queues its first sync so
    "Connect Google Drive" feels like one action rather than two."""
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    connector = connectors.get_connector(tenant.tenant_id, connector_id, repo=repo)
    if connector is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")
    if connector["type"] != "google_drive_oauth":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Not a Google Drive connect-flow connector.")

    names = req.file_names or [f"file {i + 1}" for i in range(len(req.file_ids))]
    description = f"Google Drive ({', '.join(names[:5])}{', …' if len(names) > 5 else ''})"
    updated = connectors.finalize_oauth_files(tenant.tenant_id, connector_id, req.file_ids, description, repo=repo)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This connection has already been finished or wasn't awaiting file selection. "
            "Reconnect Google Drive to pick files again.",
        )

    connector = connectors.get_connector(tenant.tenant_id, connector_id, repo=repo)
    await _enqueue_sync(tenant, connector, request, repo)
    return _serialize(connector)
