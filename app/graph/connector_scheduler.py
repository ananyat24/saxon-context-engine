# Background scheduler that periodically syncs every tenant's connectors,
# so "Sync now" isn't the only way a connector's content stays current --
# see CLAUDE.md's v1 goal ("...without a human clicking sync"). Uses
# APScheduler's AsyncIOScheduler since this runs inside the same asyncio
# event loop uvicorn already owns -- no separate process/worker needed at
# this scale. Started/stopped from app/main.py's lifespan.
#
# Runs in-process, once per running container instance -- this app's Azure
# Container Apps config scales 0-3 replicas (see scripts/deploy_azure.sh),
# so under real concurrent load more than one instance really can be
# running this scheduler at once. Without a lock, each would independently
# try to sync the same connectors on its own schedule: redundant, not
# unsafe (the content-hash dedup guard in run_connector_sync() still
# prevents duplicate ingestion), but N replicas means N times the outbound
# calls to whatever each connector reads from, and N redundant
# reconciliation passes. _tick acquires app/graph/scheduler_lock.py's
# distributed lease lock before doing any real work -- only whichever
# replica wins it for this tick actually runs; the rest skip and try again
# next tick. See that module's docstring for why a Neo4j-backed lease
# (rather than moving this to a separate dedicated worker process) was the
# right-sized fix here.
import logging
import uuid
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.graph import connectors
from app.graph.graph_repository import GraphRepository
from app.graph.neo4j_client import Neo4jClient
from app.graph.scheduler_lock import try_acquire_lock

logger = logging.getLogger(__name__)

_JOB_ID = "connector_sync"
# One id per process, generated once at import time -- stable for this
# replica's whole lifetime, so try_acquire_lock's "same holder renews its
# own lease" branch actually recognizes repeated calls from this replica
# across ticks, not just within one.
_HOLDER_ID = f"scheduler-{uuid.uuid4().hex[:12]}"


async def _sync_all_connectors(neo4j_client: Neo4jClient) -> None:
    # Imported here, not at module load time -- app.api.connectors imports
    # from app.main indirectly (via the router), so importing it at the top
    # of this module would risk a circular import at app startup.
    from app.api.connectors import _CONNECTOR_FACTORIES
    from app.graph import tenants
    from app.ingestion.connector_sync import run_connector_sync

    repo = GraphRepository(neo4j_client=neo4j_client)
    # Statically-configured tenants (settings.tenant_api_keys) plus any
    # created live through the admin API (app/api/admin.py) -- a background
    # sync has to reach every tenant's connectors, not just the ones known
    # at process startup, or a tenant onboarded without a redeploy would
    # silently never get the auto-sync half of what "add a connector" means.
    all_tenants = list(settings.tenant_api_keys.values()) + tenants.list_tenant_configs(repo=repo)
    for tenant in all_tenants:
        for connector in connectors.list_connectors(tenant.tenant_id, repo=repo):
            factory = _CONNECTOR_FACTORIES.get(connector["type"])
            if factory is None:
                continue  # a connector type that's since been removed -- skip, don't crash the whole tick
            try:
                result = await run_connector_sync(tenant, connector, factory, repo=repo)
                if result["error"]:
                    logger.warning(
                        f"Scheduled sync failed for connector '{connector['id']}' "
                        f"({tenant.tenant_id}): {result['error']}"
                    )
            except Exception:
                # One connector's unexpected failure shouldn't stop the rest
                # of this tick, or the next one, from running.
                logger.exception(
                    f"Unexpected error during scheduled sync of connector "
                    f"'{connector['id']}' ({tenant.tenant_id})"
                )


async def _renew_expiring_push_subscriptions(neo4j_client: Neo4jClient) -> None:
    """Every connector's Microsoft Graph push subscription (see
    app/ingestion/graph_subscriptions.py) that's due to expire soon gets
    renewed -- a subscription left unrenewed silently stops delivering
    notifications a few days after it's created, with no other symptom
    than the connector quietly going back to polling-only (which still
    works, just not in real time). Runs every tick alongside
    _sync_all_connectors, not on its own separate schedule -- one
    background job is simpler to reason about than two."""
    from app.ingestion.graph_subscriptions import RENEW_WHEN_WITHIN, renew_subscription
    from app.ingestion.connector_base import ConnectorFetchError

    repo = GraphRepository(neo4j_client=neo4j_client)
    now = datetime.now(timezone.utc)
    for connector in connectors.list_connectors_with_push_subscriptions(repo=repo):
        expires_at = connector.get("push_expires_at")
        if expires_at is None or (expires_at - now) > RENEW_WHEN_WITHIN:
            continue
        try:
            new_expires_at = await renew_subscription(connector["push_subscription_id"])
            connectors.set_push_subscription(
                connector["tenant_id"], connector["id"],
                subscription_id=connector["push_subscription_id"],
                client_state=connector["push_client_state"],
                expires_at=new_expires_at, repo=repo,
            )
        except ConnectorFetchError as e:
            # Can't be renewed (permission revoked, subscription already
            # gone, etc.) -- fall back to polling-only rather than retrying
            # a renewal that will keep failing every tick.
            logger.warning(f"Could not renew push subscription for connector '{connector['id']}': {e}")
            connectors.clear_push_subscription(connector["tenant_id"], connector["id"], repo=repo)


async def _tick(neo4j_client: Neo4jClient) -> None:
    repo = GraphRepository(neo4j_client=neo4j_client)
    # Lease = one full sync interval: a replica that dies mid-tick can only
    # strand the lock for at most one missed cycle before another replica's
    # next tick is able to acquire it, and a replica that's still alive and
    # ticking on schedule renews (not re-acquires) its own lease every time,
    # since try_acquire_lock treats "same holder" as always allowed.
    if not try_acquire_lock(
        repo.execute_cypher, _JOB_ID, _HOLDER_ID, lease_seconds=settings.connector_sync_interval_minutes * 60
    ):
        logger.debug(f"Skipping this tick -- another replica already holds the '{_JOB_ID}' scheduler lock.")
        return
    await _sync_all_connectors(neo4j_client)
    await _renew_expiring_push_subscriptions(neo4j_client)


def start_connector_scheduler(neo4j_client: Neo4jClient) -> AsyncIOScheduler | None:
    """Returns None (and starts nothing) if disabled via settings --
    app/main.py's lifespan handles that case by simply not stopping
    anything at shutdown either."""
    if not settings.connector_sync_enabled:
        logger.info("Connector sync scheduler disabled (connector_sync_enabled=False).")
        return None

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _tick,
        "interval",
        minutes=settings.connector_sync_interval_minutes,
        args=[neo4j_client],
        id=_JOB_ID,
        max_instances=1,  # a slow tick shouldn't overlap with the next one
        coalesce=True,  # if a tick was missed (e.g. process was busy), run once on catch-up, not once per missed tick
    )
    scheduler.start()
    logger.info(f"Connector sync scheduler started (every {settings.connector_sync_interval_minutes} minutes).")
    return scheduler
