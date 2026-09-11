# Run this before walking into a live demo. Non-destructive: it never
# deletes or purges anything, only re-asserts state that should already be
# true and reports anything that looks off so you can decide what to do
# about it yourself.
#
# What it actually does:
#   1. Re-runs scripts/seed_roles_solandra.py's seeding (safe to repeat --
#      MERGE-based, same as running that script directly). Confirms the org
#      chart and order-ownership assignments role-based visibility depends
#      on are in place, in case they were never seeded on this database or
#      order ownership has shifted since the last sync.
#   2. Lists every connector for every tenant configured in settings and
#      reports its health (error / never_synced / queued / authorized_needs_
#      files / ok -- see app/api/connectors.py's _connector_health), so you
#      know before you're in front of an audience whether anything needs a
#      manual "Sync now" click, rather than finding out live.
#
# What it deliberately does NOT do:
#   - Trigger any connector sync (that's a real cost -- do it from the UI's
#     "Sync now" if a connector below needs it).
#   - Touch data older than the current run in any way.
#   - Delete, purge, or reset anything. There is no --force flag. If you
#     need to fix a specific bad connector, use the UI's own "Fix
#     attribution" flow, which is a real, scoped, explicit re-ingestion --
#     not something this script does on your behalf.
#
# Usage:
#   python scripts/reset_demo_state.py
import logging

from app.config import settings
from app.graph import connectors
from app.graph.graph_repository import GraphRepository
from scripts.seed_roles_solandra import main as seed_solandra_roles

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _connector_health(c: dict) -> str:
    # Mirrors app/api/connectors.py's _connector_health exactly (not
    # imported from there since that module also pulls in the full FastAPI
    # router and every connector-type dependency just to define one small
    # pure function -- this keeps the script's own dependency footprint to
    # "talk to Neo4j," like every other script in this directory).
    if c["status"] == "authorized_needs_files":
        return "authorized_needs_files"
    if c["status"] == "error":
        return "error"
    if c["status"] == "queued":
        return "queued"
    if not c["last_synced_at"]:
        return "never_synced"
    return "ok"


def report_connector_health() -> None:
    repo = GraphRepository()
    logger.info("\nConnector health, every tenant:")
    any_needs_attention = False
    seen_tenant_ids = {tc.tenant_id for tc in settings.tenant_api_keys.values()}
    for tenant_id in sorted(seen_tenant_ids):
        for c in connectors.list_connectors(tenant_id, repo=repo):
            health = _connector_health(c)
            flag = "  <- check before the demo" if health in ("error", "never_synced") else ""
            logger.info(f"  [{tenant_id}] {c['name']} ({c['group_id']}): {health}{flag}")
            if flag:
                any_needs_attention = True
    if any_needs_attention:
        logger.info(
            "\nOne or more connectors above need attention. This script does not sync them for "
            "you -- use \"Sync now\" (or \"Fix attribution\" for the untagged-data case) in the UI."
        )
    else:
        logger.info("\nAll connectors look ready.")


def main() -> None:
    logger.info("=== Re-seeding Solandra role-based visibility (safe to repeat) ===")
    seed_solandra_roles()
    report_connector_health()
    logger.info("\nDone. Nothing was deleted or synced -- this only reported and re-asserted seed data.")


if __name__ == "__main__":
    main()
