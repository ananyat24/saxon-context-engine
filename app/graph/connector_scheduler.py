# Background scheduler that periodically syncs every tenant's connectors,
# so "Sync now" isn't the only way a connector's content stays current --
# see CLAUDE.md's v1 goal ("...without a human clicking sync"). Uses
# APScheduler's AsyncIOScheduler since this runs inside the same asyncio
# event loop uvicorn already owns -- no separate process/worker needed at
# this scale. Started/stopped from app/main.py's lifespan.
#
# Runs in-process, once per running container instance. That's fine for a
# single-replica deployment (this app's current default is min/max replicas
# 0-3, but a demo/pilot workload realistically runs one instance at a time);
# if this ever runs with more than one replica actually serving traffic
# concurrently, each instance would independently try to sync the same
# connectors on its own schedule -- redundant but not unsafe, since the
# content-hash dedup guard in run_connector_sync() still prevents duplicate
# ingestion, just wasted fetch calls. A real multi-instance deployment
# should move this to a single dedicated worker (see CLAUDE.md's v2 "worker
# pool" step) rather than running it on every instance.
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.graph import connectors
from app.graph.graph_repository import GraphRepository
from app.graph.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

_JOB_ID = "connector_sync"


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


def start_connector_scheduler(neo4j_client: Neo4jClient) -> AsyncIOScheduler | None:
    """Returns None (and starts nothing) if disabled via settings --
    app/main.py's lifespan handles that case by simply not stopping
    anything at shutdown either."""
    if not settings.connector_sync_enabled:
        logger.info("Connector sync scheduler disabled (connector_sync_enabled=False).")
        return None

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _sync_all_connectors,
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
