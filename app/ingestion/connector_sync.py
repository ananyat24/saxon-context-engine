# The actual "run one connector's sync" logic, extracted so it's identical
# regardless of who triggers it -- a manual "Sync now" click
# (app/api/connectors.py) or the background scheduler
# (app/graph/connector_scheduler.py). Both call this; neither reimplements
# the fetch -> dedup-check -> ingest -> record-result sequence on its own,
# so there's no way for the two paths to quietly drift apart.
import logging
from typing import Callable, TypedDict

from app.config import TenantConfig
from app.graph import connectors
from app.graph.graph_repository import GraphRepository
from app.graph.graphiti_adapter import build_graphiti
from app.graph.spend_limiter import SpendLimitExceeded
from app.ingestion.connector_base import ConnectorFetchError, SourceConnector
from app.ingestion.pipeline import IngestionPipeline
from app.ontology.bootstrap import build_scoped_registry
from app.ontology.graphiti_types import build_graphiti_schema

logger = logging.getLogger(__name__)


class SyncResult(TypedDict):
    synced: bool
    skipped_unchanged: bool
    error: str | None
    # True only for a spend-cap failure -- the one error a caller (the HTTP
    # route) needs to react to differently (402, not just "sync failed").
    spend_limit_exceeded: bool


async def run_connector_sync(
    tenant: TenantConfig,
    connector: dict,
    factory: Callable[[dict], SourceConnector],
    *,
    repo: GraphRepository,
) -> SyncResult:
    """Never raises -- every failure mode (a bad fetch, an unchanged
    fetch, a spend cap, an unexpected extraction error) is reported back in
    the returned dict instead, so a caller can react however fits it rather
    than this function assuming one caller's error-handling shape."""
    connector_id = connector["id"]
    source = factory(connector)
    try:
        records = await source.fetch()
    except ConnectorFetchError as e:
        connectors.record_sync_result(tenant.tenant_id, connector_id, status="error", last_error=str(e), repo=repo)
        return {"synced": False, "skipped_unchanged": False, "error": str(e), "spend_limit_exceeded": False}

    new_hash = source.content_hash(records)
    if new_hash == connector.get("content_hash"):
        # Fetched fine, but it's word-for-word what the last successful sync
        # already ingested -- re-running extraction on identical text would
        # just spend real money to add a near-duplicate episode for no new
        # information, so this is a genuine cost guard, not just bookkeeping.
        # It's what makes a frequent background sync (see
        # connector_scheduler.py) affordable in the first place: most ticks
        # for most connectors do nothing more than this cheap check.
        connectors.record_sync_result(tenant.tenant_id, connector_id, status="unchanged", repo=repo)
        return {"synced": False, "skipped_unchanged": True, "error": None, "spend_limit_exceeded": False}

    # Deliberately NOT the tenant's pooled Graphiti client (see
    # app/graph/tenant_graphiti_pool.py) -- that one is tagged bucket="query"
    # and shares the query spend budget. Ingestion has its own budget (see
    # app/graph/spend_limiter.py), so this needs its own client tagged
    # bucket="ingestion", same as scripts/ingest_samples.py, rather than
    # silently spending a sync against the query budget.
    graphiti = build_graphiti(google_api_key=tenant.gemini_api_key, bucket="ingestion")
    try:
        # Content from any of these source types is arbitrary -- no domain
        # pack fits it a priori, unlike a known dataset like Northwind
        # (sales/supply_chain). Core-only keeps extraction focused on the
        # generic entity/relationship vocabulary rather than guessing at a
        # domain.
        scoped = build_scoped_registry([])
        entity_types, edge_types, edge_type_map = build_graphiti_schema(scoped)
        pipeline = IngestionPipeline(graphiti, entity_types=entity_types, edge_types=edge_types, edge_type_map=edge_type_map)
        for record in records:
            await pipeline.ingest_episode(
                name=record.name,
                body=record.body,
                source_description=record.source_description,
                group_id=connector["group_id"],
            )
    except SpendLimitExceeded as e:
        connectors.record_sync_result(tenant.tenant_id, connector_id, status="error", last_error=str(e), repo=repo)
        return {"synced": False, "skipped_unchanged": False, "error": str(e), "spend_limit_exceeded": True}
    except Exception as e:
        logger.error(f"Connector sync failed for '{connector_id}': {e}")
        connectors.record_sync_result(tenant.tenant_id, connector_id, status="error", last_error=str(e), repo=repo)
        return {"synced": False, "skipped_unchanged": False, "error": str(e), "spend_limit_exceeded": False}
    finally:
        await graphiti.close()

    connectors.record_sync_result(tenant.tenant_id, connector_id, status="synced", content_hash=new_hash, repo=repo)
    return {"synced": True, "skipped_unchanged": False, "error": None, "spend_limit_exceeded": False}
