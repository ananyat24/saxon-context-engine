# Needs a real, reachable Neo4j -- same caveat as test_entity_reconciliation.py.
# Covers app/graph/scheduler_lock.py: the distributed lease lock that keeps
# more than one replica of this app from redundantly running the same
# connector-sync tick at once (see connector_scheduler.py's _tick).
import time
import uuid

import pytest

from app.graph.graph_repository import GraphRepository
from app.graph.scheduler_lock import ensure_scheduler_lock_indexes, release_lock, try_acquire_lock


@pytest.fixture
def repo():
    from unittest.mock import Mock
    repo = GraphRepository(graphiti_instance=Mock())
    # Idempotent (IF NOT EXISTS) -- the concurrent-acquire test below
    # specifically depends on this constraint existing to be a meaningful
    # test at all (see that test's docstring and this module's own).
    ensure_scheduler_lock_indexes(repo.execute_cypher)
    return repo


def test_first_acquirer_gets_the_lock(repo):
    lock_id = f"test_lock_{uuid.uuid4().hex[:8]}"
    try:
        assert try_acquire_lock(repo.execute_cypher, lock_id, "holder-a", lease_seconds=60) is True
    finally:
        repo.execute_cypher("MATCH (l:SchedulerLock {id: $id}) DETACH DELETE l", {"id": lock_id})


def test_a_second_holder_cannot_acquire_a_live_lease(repo):
    lock_id = f"test_lock_{uuid.uuid4().hex[:8]}"
    try:
        assert try_acquire_lock(repo.execute_cypher, lock_id, "holder-a", lease_seconds=60) is True
        assert try_acquire_lock(repo.execute_cypher, lock_id, "holder-b", lease_seconds=60) is False
    finally:
        repo.execute_cypher("MATCH (l:SchedulerLock {id: $id}) DETACH DELETE l", {"id": lock_id})


def test_the_same_holder_can_renew_its_own_lease(repo):
    lock_id = f"test_lock_{uuid.uuid4().hex[:8]}"
    try:
        assert try_acquire_lock(repo.execute_cypher, lock_id, "holder-a", lease_seconds=60) is True
        # A later tick from the SAME replica must not be treated as "someone
        # else holds it" -- that would starve the very replica that's
        # supposed to keep running this job every interval.
        assert try_acquire_lock(repo.execute_cypher, lock_id, "holder-a", lease_seconds=60) is True
    finally:
        repo.execute_cypher("MATCH (l:SchedulerLock {id: $id}) DETACH DELETE l", {"id": lock_id})


def test_a_second_holder_can_acquire_once_the_lease_expires(repo):
    lock_id = f"test_lock_{uuid.uuid4().hex[:8]}"
    try:
        assert try_acquire_lock(repo.execute_cypher, lock_id, "holder-a", lease_seconds=1) is True
        time.sleep(1.5)
        assert try_acquire_lock(repo.execute_cypher, lock_id, "holder-b", lease_seconds=60) is True
    finally:
        repo.execute_cypher("MATCH (l:SchedulerLock {id: $id}) DETACH DELETE l", {"id": lock_id})


def test_release_lock_only_releases_its_own_holders_lock(repo):
    lock_id = f"test_lock_{uuid.uuid4().hex[:8]}"
    try:
        assert try_acquire_lock(repo.execute_cypher, lock_id, "holder-a", lease_seconds=60) is True
        # Not the real holder -- must be a no-op, not clear someone else's
        # still-live lease.
        release_lock(repo.execute_cypher, lock_id, "holder-b")
        assert try_acquire_lock(repo.execute_cypher, lock_id, "holder-c", lease_seconds=60) is False

        release_lock(repo.execute_cypher, lock_id, "holder-a")
        assert try_acquire_lock(repo.execute_cypher, lock_id, "holder-c", lease_seconds=60) is True
    finally:
        repo.execute_cypher("MATCH (l:SchedulerLock {id: $id}) DETACH DELETE l", {"id": lock_id})


def test_concurrent_first_acquires_only_let_one_holder_win(repo):
    # The actual property this whole module exists for: two "replicas"
    # racing to become the very first holder of a lock that doesn't exist
    # yet must never both come back True. Real threads (not asyncio) against
    # the real Neo4j driver, so this exercises actual concurrent transactions,
    # not just sequential calls that happen to look concurrent.
    import threading

    lock_id = f"test_lock_{uuid.uuid4().hex[:8]}"
    results = {}

    def attempt(holder):
        from app.graph.neo4j_client import Neo4jClient
        thread_repo = GraphRepository(neo4j_client=Neo4jClient())
        results[holder] = try_acquire_lock(thread_repo.execute_cypher, lock_id, holder, lease_seconds=60)

    try:
        threads = [threading.Thread(target=attempt, args=(f"holder-{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(1 for won in results.values() if won) == 1
    finally:
        repo.execute_cypher("MATCH (l:SchedulerLock {id: $id}) DETACH DELETE l", {"id": lock_id})
