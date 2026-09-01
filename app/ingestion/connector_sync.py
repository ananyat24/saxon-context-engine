# The actual "run one connector's sync" logic, extracted so it's identical
# regardless of who triggers it -- a manual "Sync now" click
# (app/api/connectors.py) or the background scheduler
# (app/graph/connector_scheduler.py). Both call this; neither reimplements
# the fetch -> dedup-check -> ingest -> record-result sequence on its own,
# so there's no way for the two paths to quietly drift apart.
import logging
from typing import Callable, TypedDict

from app.config import TenantConfig
from app.context.response_cache import get_response_cache
from app.graph import connectors
from app.graph.graph_repository import GraphRepository
from app.graph.graphiti_adapter import build_graphiti
from app.graph.reconciliation import reconcile_tenant
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
            result = await pipeline.ingest_episode(
                name=record.name,
                body=record.body,
                source_description=record.source_description,
                group_id=connector["group_id"],
            )
            # Tags the Episodic node this record produced with which
            # connector wrote it -- see app/api/graph.py's ?connector_id=
            # filter, which is what makes the connector preview modal
            # ("what's been pulled into the graph from this connector")
            # show only this connector's own facts instead of everything in
            # the whole knowledge base. A knowledge base can have more than
            # one connector feeding it (group_id alone doesn't distinguish
            # them), which is exactly what made the un-tagged version wrong.
            # Best-effort: a tagging failure shouldn't fail an otherwise-
            # successful sync -- it just means this one episode's facts
            # won't show up in that connector's own preview (they're still
            # in the graph and answerable by every other query path, which
            # is scoped by group_id, not connector_id).
            try:
                episode_uuid = result.episode.uuid
                repo.execute_cypher(
                    "MATCH (e:Episodic {uuid: $uuid}) SET e.connector_id = $connector_id",
                    {"uuid": episode_uuid, "connector_id": connector_id},
                )
            except Exception as e:
                # Deliberately doesn't reference `result` here -- the failure
                # this guards against includes result.episode not existing at
                # all (an unexpected return shape from a monkeypatched/future
                # ingest_episode), so the log message can't assume it does
                # either.
                logger.warning(f"Could not tag episode '{record.name}' with connector '{connector_id}': {e}")
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
    # New data just landed for this group -- don't leave a stale "no
    # information found" (or an outdated answer) sitting in the response
    # cache for however long its TTL has left. See app/context/response_cache.py.
    get_response_cache().invalidate_group(tenant.tenant_id, connector["group_id"])

    # The Reconcile stage (app/graph/reconciliation.py): a newly-ingested
    # entity in THIS connector's group_id might be the same real-world thing
    # as one already sitting in another of this tenant's group_ids, so this
    # runs across every group_id the tenant has, not just the one that just
    # synced. Best-effort -- a reconciliation failure shouldn't turn an
    # otherwise-successful sync into a reported error; the next sync (of any
    # of this tenant's connectors) will just try again.
    try:
        result = reconcile_tenant(repo.execute_cypher, tenant.tenant_id, sorted(tenant.knowledge_base_ids()))
        logger.info(
            f"Reconciliation after '{connector_id}' sync: "
            f"{result['same_as_created']} same_as, {result['proposals_created']} proposal(s) created"
        )
    except Exception as e:
        logger.error(f"Reconciliation failed after connector sync for '{connector_id}': {e}")

    return {"synced": True, "skipped_unchanged": False, "error": None, "spend_limit_exceeded": False}
