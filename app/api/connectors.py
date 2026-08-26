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
from app.ingestion.google_drive_source import GoogleDriveConnector
from app.ingestion.web_source import WebConnector
from app.security import require_tenant

router = APIRouter()

# The one place a connector `type` maps to its SourceConnector implementation.
# Adding a new type (a real SharePoint/CRM API, once credentials exist) is:
# implement SourceConnector (see app/ingestion/connector_base.py) and add one
# entry here -- no changes needed to the sync route below, IngestionPipeline,
# or ontology handling. "database"/"documents"/"email" read bundled mock
# data rather than a live source (see each module's docstring) -- they exist
# to prove the connector types most clients actually have (a CRM/DB, a
# document store, an inbox) work end to end. "google_drive" is the first
# real live source connector -- see app/ingestion/google_drive_source.py.
_CONNECTOR_FACTORIES: dict[str, Callable[[dict], SourceConnector]] = {
    "web": lambda connector: WebConnector(connector["url"]),
    "database": lambda connector: DatabaseConnector(),
    "documents": lambda connector: DocumentConnector(),
    "email": lambda connector: EmailConnector(),
    "google_drive": lambda connector: GoogleDriveConnector(connector["url"]),
}

# Which types read from a tenant-supplied address (a URL, or here a Drive
# folder link/id) vs. a fixed bundled sample with nothing to collect (see
# _CONNECTOR_FACTORIES above) or an operator-wide credential with no
# per-connector address at all.
_TYPES_REQUIRING_URL = {"web", "google_drive"}


class CreateConnectorRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = "web"
    # Which of the tenant's own knowledge bases a sync writes into -- must be
    # one the tenant already has (see app/security.py's resolve_knowledge_base
    # for the same boundary applied elsewhere), not an arbitrary new value.
    group_id: str
    url: Optional[str] = Field(default=None, max_length=2000)


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
    }


@router.get("")
def list_connectors(request: Request, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    return [_serialize(c) for c in connectors.list_connectors(tenant.tenant_id, repo=repo)]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_connector(req: CreateConnectorRequest, request: Request, tenant: TenantConfig = Depends(require_tenant)):
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

    if req.type == "google_drive" and not settings.google_drive_service_account_json:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google Drive isn't configured on this server yet -- ask your operator to set it up.",
        )

    if req.type in _TYPES_REQUIRING_URL:
        url = (req.url or "").strip()
        if not url:
            detail = "Paste a Drive folder link or id." if req.type == "google_drive" else "This connector type needs a URL."
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
        if req.type == "google_drive":
            try:
                GoogleDriveConnector(url)  # validates the folder id/link shape up front
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
    return _serialize(created)


@router.delete("/{connector_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connector(connector_id: str, request: Request, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    deleted = connectors.delete_connector(tenant.tenant_id, connector_id, repo=repo)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")


@router.post("/{connector_id}/sync")
async def sync_connector(connector_id: str, request: Request, tenant: TenantConfig = Depends(require_tenant)):
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    connector = connectors.get_connector(tenant.tenant_id, connector_id, repo=repo)
    if connector is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found.")

    # _CONNECTOR_FACTORIES is the one dispatch point -- every type from here
    # down is handled generically via the SourceConnector interface.
    factory = _CONNECTOR_FACTORIES[connector["type"]]
    result = await run_connector_sync(tenant, connector, factory, repo=repo)
    if result["spend_limit_exceeded"]:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=result["error"])
    return {"synced": result["synced"], "skipped_unchanged": result["skipped_unchanged"], "error": result["error"]}
