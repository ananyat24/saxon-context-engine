# Role-based visibility: which of a knowledge base's entities a given user is
# allowed to see, based on where they sit in that knowledge base's org
# hierarchy.
#
# This is a deliberately separate layer from Graphiti's own fact graph. A
# tenant's business facts (customers, orders, contracts...) are extracted by
# an LLM and carry temporal validity; who-reports-to-whom and who-owns-what
# are authorization data, and authorization data must be exact, not
# probabilistic. So instead of asking an LLM to infer "Priya manages this
# account" from text, org structure and ownership are written directly as
# :User nodes and :REPORTS_TO / :ASSIGNED_TO edges (see scripts/seed_roles.py
# for how a real onboarding process would populate them -- through an admin
# action or an HR/CRM sync, the same way a real company's access control data
# arrives, not through document extraction).
#
# :User and :REPORTS_TO/:ASSIGNED_TO are intentionally not part of the
# ontology YAML (app/ontology/): that schema constrains what Graphiti's LLM
# extraction is allowed to invent from text, and authorization data is never
# something an extraction pipeline should be inventing.
import logging
from typing import Optional

from fastapi import HTTPException, status

from app.graph.graph_repository import GraphRepository

logger = logging.getLogger(__name__)

# Bounds how far up/down the org chart a single query will walk. An
# unbounded variable-length path is the one thing that *could* make this
# slow on a pathological input (e.g. a cyclic REPORTS_TO chain from bad data);
# 50 levels is far deeper than any real org chart, so this is a safety rail,
# not a real limit in practice.
MAX_HIERARCHY_DEPTH = 50


def ensure_authorization_indexes(repo: Optional[GraphRepository] = None) -> None:
    """Creates the indexes role-based visibility depends on for its scaling
    claim to hold. Idempotent -- safe to call on every app startup, not just
    once during setup.

    Without User(group_id, id), resolving "who does this user outrank" would
    mean scanning every User node in the database to find the starting point
    of the traversal, on every single request.
    """
    repo = repo or GraphRepository()
    repo.execute_cypher(
        "CREATE INDEX user_group_id IF NOT EXISTS FOR (u:User) ON (u.group_id, u.id)"
    )


def user_exists(group_id: str, user_id: str, repo: Optional[GraphRepository] = None) -> bool:
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        "MATCH (u:User {group_id: $group_id, id: $user_id}) RETURN u.id AS id LIMIT 1",
        {"group_id": group_id, "user_id": user_id},
    )
    return len(rows) > 0


def resolve_as_user(group_id: str, as_user: Optional[str], repo: Optional[GraphRepository] = None) -> Optional[str]:
    """Validates a client-supplied as_user id the same way resolve_knowledge_base
    validates a knowledge_base id: None means "no role filtering" (unfiltered,
    the current behavior), but a *given* id must actually exist in this
    knowledge base's org chart or the request is rejected. This is what stops
    a client from probing for a valid-looking user id from a knowledge base
    they can't otherwise see structure for.
    """
    if as_user is None:
        return None
    if not user_exists(group_id, as_user, repo=repo):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown user '{as_user}' for this knowledge base.",
        )
    return as_user


def list_users(group_id: str, repo: Optional[GraphRepository] = None) -> list[dict]:
    """Every user in this knowledge base's org chart, with enough structure
    (manager_id) for a client to render it as a hierarchy rather than a flat
    list."""
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        """
        MATCH (u:User {group_id: $group_id})
        OPTIONAL MATCH (u)-[:REPORTS_TO]->(manager:User)
        RETURN u.id AS id, u.name AS name, u.role AS role, manager.id AS manager_id
        ORDER BY u.name
        """,
        {"group_id": group_id},
    )
    return rows


def _visible_user_ids_clause() -> str:
    """The Cypher fragment shared by every visibility-scoped query: the
    requesting user plus everyone who (transitively) reports to them.

    This only ever traverses the :User subgraph, which is sized to the
    org -- hundreds or thousands of employees -- regardless of how many
    millions of business entities the knowledge base holds. That's what
    keeps this cheap at any data scale: the expensive dimension (entity
    count) never enters into this part of the query.
    """
    return f"""
        MATCH (u:User {{group_id: $group_id, id: $user_id}})
        CALL {{
            WITH u
            MATCH (sub:User {{group_id: $group_id}})-[:REPORTS_TO*0..{MAX_HIERARCHY_DEPTH}]->(u)
            RETURN collect(sub.id) AS subordinate_ids
        }}
        WITH [u.id] + subordinate_ids AS visible_owner_ids
    """


def _visible_entities_clause() -> str:
    """Builds on _visible_user_ids_clause(): expands visible_owner_ids outward
    to the entities they own via an indexed User lookup + relationship
    traversal, rather than matching every :Entity in the knowledge base and
    checking its owner against the visible-user list.

    That second shape is what actually failed to scale under
    scripts/load_test_query_scale.py at 100k entities (4.2s for a query the
    module's own docstring claims is entity-count-independent): it touches
    every entity in the group no matter how small the visible-user set is,
    because the Entity side -- not the User side -- is where the match
    starts. Starting from `visible_owner_ids` (bounded by org size) and
    traversing outward via the existing ASSIGNED_TO edges instead costs
    proportional to what those users actually own, which is what the
    scaling claim requires. Binds `n`, one row per visible entity.
    """
    return (
        _visible_user_ids_clause()
        + """
        UNWIND visible_owner_ids AS owner_id
        MATCH (n:Entity {group_id: $group_id})-[:ASSIGNED_TO]->(:User {group_id: $group_id, id: owner_id})
        WITH DISTINCT n
    """
    )


def get_visible_node_count(group_id: str, user_id: str, repo: Optional[GraphRepository] = None) -> int:
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        _visible_entities_clause() + "RETURN count(n) AS c",
        {"group_id": group_id, "user_id": user_id},
    )
    return rows[0]["c"]


def get_visible_relationship_count(group_id: str, user_id: str, repo: Optional[GraphRepository] = None) -> int:
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        _visible_entities_clause()
        + """
        WITH collect(n.uuid) AS visible_entity_uuids
        UNWIND visible_entity_uuids AS uuid
        MATCH (n:Entity {group_id: $group_id, uuid: uuid})-[r:RELATES_TO]-(m:Entity {group_id: $group_id})
        WHERE NOT m:Decision
        RETURN count(DISTINCT r) AS c
        """,
        {"group_id": group_id, "user_id": user_id},
    )
    return rows[0]["c"]


def get_visible_entity_types(group_id: str, user_id: str, repo: Optional[GraphRepository] = None) -> list[list[str]]:
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        _visible_entities_clause() + "RETURN DISTINCT labels(n) AS labels",
        {"group_id": group_id, "user_id": user_id},
    )
    return [row["labels"] for row in rows]


def get_visible_nodes(
    group_id: str, user_id: str, limit: int = 50, repo: Optional[GraphRepository] = None
) -> list[dict]:
    repo = repo or GraphRepository()
    return repo.execute_cypher(
        _visible_entities_clause()
        + """
        RETURN n.uuid AS id, n.name AS name, labels(n) AS labels, n.summary AS summary, n.created_at AS created_at
        ORDER BY n.created_at DESC
        LIMIT $limit
        """,
        {"group_id": group_id, "user_id": user_id, "limit": limit},
    )


def get_visible_relationships(
    group_id: str, user_id: str, limit: int = 50, repo: Optional[GraphRepository] = None
) -> list[dict]:
    repo = repo or GraphRepository()
    return repo.execute_cypher(
        _visible_entities_clause()
        + """
        WITH collect(n.uuid) AS visible_entity_uuids
        UNWIND visible_entity_uuids AS uuid
        MATCH (a:Entity {group_id: $group_id, uuid: uuid})-[r:RELATES_TO]-(b:Entity {group_id: $group_id})
        WHERE NOT b:Decision
        RETURN DISTINCT a.name AS source, r.name AS type, b.name AS target, r.fact AS fact, r.created_at AS created_at
        ORDER BY r.created_at DESC
        LIMIT $limit
        """,
        {"group_id": group_id, "user_id": user_id, "limit": limit},
    )


def get_visible_entity_uuids(group_id: str, user_id: str, repo: Optional[GraphRepository] = None) -> set[str]:
    """Every entity uuid this user can see, as a set for O(1) membership
    checks. Used to post-filter Graphiti's own search results (see
    GraphRepository.search_graphiti_facts) -- Graphiti's search already
    returns a small, bounded top-K list, so filtering that list against this
    set is cheap regardless of how large the underlying knowledge base is;
    the set itself is bounded by how much this user's org owns, not by total
    graph size, for the same reason the queries above scale. No ORDER BY/LIMIT
    here since every matching id is needed, not a page of them.
    """
    repo = repo or GraphRepository()
    rows = repo.execute_cypher(
        _visible_entities_clause() + "RETURN n.uuid AS id",
        {"group_id": group_id, "user_id": user_id},
    )
    return {row["id"] for row in rows}
