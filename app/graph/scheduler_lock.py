# The multi-instance gap connector_scheduler.py's own module docstring
# already flags: this app's Azure Container Apps config scales 0-3 replicas
# (see scripts/deploy_azure.sh), and each replica runs its own in-process
# APScheduler -- so under real concurrent load, every replica independently
# ticks the same tenants' connectors on the same interval. Not unsafe (the
# content-hash dedup guard in run_connector_sync() still prevents duplicate
# ingestion), but wasteful: N replicas means N redundant fetches per tick,
# N redundant reconciliation passes, N times the outbound calls to whatever
# external source a connector reads from.
#
# Rather than standing up a separate dedicated worker process (the "real"
# fix the docstring names, and a bigger infra change -- a new deployable,
# its own scaling/health story), this is a distributed lease lock using
# Neo4j, which every replica already has a connection to: whichever replica
# acquires the lock for this tick actually runs it; the rest skip. A lease
# (not a lock held for the tick's duration and explicitly released) so a
# replica that crashes mid-tick can't strand the lock forever -- it just
# expires and the next tick (from any surviving replica) picks it up.
#
# Needs a real MERGE-on-a-uniquely-constrained-node pattern to be safe
# against two replicas racing at the same instant, not just "read, check
# expiry, write" from Python -- see ensure_scheduler_lock_indexes and
# try_acquire_lock's docstrings.
import logging

from app.graph.entity_resolution import ExecuteCypher

logger = logging.getLogger(__name__)


def ensure_scheduler_lock_indexes(execute_cypher: ExecuteCypher) -> None:
    """A uniqueness CONSTRAINT, not just an index -- this is what makes
    try_acquire_lock's MERGE safe under real concurrency. Without it, two
    replicas racing to MERGE the same not-yet-existing lock node could both
    take the ON CREATE branch and end up with two :SchedulerLock nodes with
    the same id, silently defeating the whole point of this module. With
    it, Neo4j itself guarantees only one such node can ever exist -- a
    concurrent MERGE that would create a duplicate fails with a constraint
    violation instead, and the caller's retry (see try_acquire_lock) then
    correctly sees the other replica's already-committed node."""
    execute_cypher(
        "CREATE CONSTRAINT scheduler_lock_id_unique IF NOT EXISTS FOR (l:SchedulerLock) REQUIRE l.id IS UNIQUE", None
    )


def try_acquire_lock(execute_cypher: ExecuteCypher, lock_id: str, holder: str, lease_seconds: int) -> bool:
    """Attempts to become (or remain) the holder of `lock_id` for the next
    `lease_seconds`. Returns True if `holder` now owns the lock -- either
    because nothing held it, because `holder` already did (renewing its own
    lease is always allowed), or because the previous holder's lease has
    expired. Returns False if someone else currently holds a live lease.

    Single Cypher statement, not read-then-write from Python: Neo4j
    evaluates one statement's MERGE/SET as one atomic unit against the
    uniquely-constrained node (see ensure_scheduler_lock_indexes), so two
    replicas calling this at the same instant can't both believe they won.
    """
    rows = execute_cypher(
        "MERGE (l:SchedulerLock {id: $lock_id}) "
        "ON CREATE SET l.holder = $holder, l.expires_at = datetime() + duration({seconds: $lease_seconds}) "
        "ON MATCH SET "
        "  l.holder = CASE WHEN l.holder = $holder OR l.expires_at < datetime() THEN $holder ELSE l.holder END, "
        "  l.expires_at = CASE WHEN l.holder = $holder OR l.expires_at < datetime() "
        "                 THEN datetime() + duration({seconds: $lease_seconds}) ELSE l.expires_at END "
        "RETURN l.holder AS holder",
        {"lock_id": lock_id, "holder": holder, "lease_seconds": lease_seconds},
    )
    won = bool(rows) and rows[0]["holder"] == holder
    logger.debug("scheduler_lock: '%s' %s the lock for '%s'", holder, "acquired" if won else "did not acquire", lock_id)
    return won


def release_lock(execute_cypher: ExecuteCypher, lock_id: str, holder: str) -> None:
    """Best-effort early release (a tick that finishes well before its
    lease expires frees the lock immediately rather than making the next
    replica wait out the rest of the lease) -- only releases if `holder`
    still owns it, so this can never clobber a lock some other replica
    legitimately acquired after this one's lease already expired."""
    execute_cypher(
        "MATCH (l:SchedulerLock {id: $lock_id, holder: $holder}) DETACH DELETE l",
        {"lock_id": lock_id, "holder": holder},
    )
