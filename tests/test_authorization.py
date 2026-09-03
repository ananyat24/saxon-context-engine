# Integration tests for role-based visibility: these need a real, reachable
# Neo4j (same as tests/test_graph.py). They build a small synthetic org chart
# and business entity under a throwaway group_id, assert on visibility, then
# clean up everything they created so repeated runs don't accumulate data.
import uuid

import pytest

from app.graph import authorization
from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    return GraphRepository()


@pytest.fixture
def hierarchy(repo):
    """rep reports to manager reports to exec. rep owns one entity; manager
    and exec own nothing directly, so their visibility comes only from the
    hierarchy expansion."""
    group_id = f"test_authz_{uuid.uuid4().hex[:8]}"
    entity_uuid = str(uuid.uuid4())

    authorization.ensure_authorization_indexes(repo)
    repo.execute_cypher(
        """
        MERGE (exec:User {group_id: $group_id, id: 'exec'})
        MERGE (manager:User {group_id: $group_id, id: 'manager'})
        MERGE (rep:User {group_id: $group_id, id: 'rep'})
        MERGE (outsider:User {group_id: $group_id, id: 'outsider'})
        MERGE (manager)-[:REPORTS_TO]->(exec)
        MERGE (rep)-[:REPORTS_TO]->(manager)
        MERGE (n:Entity {group_id: $group_id, uuid: $entity_uuid})
        SET n.name = 'Test Account', n.created_at = datetime()
        MERGE (n)-[:ASSIGNED_TO]->(rep)
        """,
        {"group_id": group_id, "entity_uuid": entity_uuid},
    )
    try:
        yield group_id
    finally:
        repo.execute_cypher(
            """
            MATCH (n) WHERE n.group_id = $group_id
            DETACH DELETE n
            """,
            {"group_id": group_id},
        )


def test_rep_sees_own_entity(hierarchy, repo):
    assert authorization.get_visible_node_count(hierarchy, "rep", repo=repo) == 1


def test_manager_sees_reports_entity(hierarchy, repo):
    """The core hierarchy guarantee: visibility isn't just direct ownership,
    it includes everything owned anywhere below you in the org chart."""
    assert authorization.get_visible_node_count(hierarchy, "manager", repo=repo) == 1


def test_exec_sees_entity_two_levels_down(hierarchy, repo):
    assert authorization.get_visible_node_count(hierarchy, "exec", repo=repo) == 1


def test_outsider_sees_nothing(hierarchy, repo):
    """A user with no reports and nothing directly assigned to them sees an
    empty knowledge base, not an error and not everyone else's data."""
    assert authorization.get_visible_node_count(hierarchy, "outsider", repo=repo) == 0


def test_resolve_as_user_rejects_unknown_id(hierarchy):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        authorization.resolve_as_user(hierarchy, "not_a_real_user")
    assert exc_info.value.status_code == 400


def test_resolve_as_user_accepts_none():
    assert authorization.resolve_as_user("any_group", None) is None


def test_resolve_as_user_accepts_known_user(hierarchy):
    assert authorization.resolve_as_user(hierarchy, "rep") == "rep"


def test_visible_entity_uuids_matches_node_count(hierarchy, repo):
    uuids = authorization.get_visible_entity_uuids(hierarchy, "manager", repo=repo)
    assert len(uuids) == 1
