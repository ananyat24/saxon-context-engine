# GET endpoints for browsing a tenant's own graph data directly, mainly
# for the UI (see frontend/) so it has something real to render beyond the
# ontology's static type list. Every route here requires a tenant's API key
# and scopes its Cypher query to one of that tenant's own knowledge bases
# (group_ids). A caller can pick which one via ?knowledge_base=, but never
# one outside their own tenant. See app/security.py's resolve_knowledge_base.
#
# Routes also accept an optional ?as_user=, which further scopes results to
# what that person can see in their org hierarchy. See
# app/graph/authorization.py for how that's enforced and why it scales.
from fastapi import APIRouter, Depends, Request

from app.config import TenantConfig
from app.graph import authorization
from app.graph.graph_repository import GraphRepository
from app.security import require_tenant, resolve_knowledge_base

router = APIRouter()


@router.get("/knowledge-bases")
def list_knowledge_bases(tenant: TenantConfig = Depends(require_tenant)):
    """Every knowledge base (dataset) this tenant can query, plus which one
    is the default. Lets a client build a picker without hardcoding ids."""
    return {
        "knowledge_bases": [kb.model_dump() for kb in tenant.knowledge_bases],
        "default": tenant.default_knowledge_base_id(),
    }


@router.get("/users")
def list_users(request: Request, knowledge_base: str | None = None, tenant: TenantConfig = Depends(require_tenant)):
    """Every user in the selected knowledge base's org chart, with manager_id
    so a client can render it as a hierarchy rather than a flat list. This
    is empty for knowledge bases that don't have role data seeded yet."""
    group_id = resolve_knowledge_base(tenant, knowledge_base)
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    return authorization.list_users(group_id, repo=repo)


@router.get("/summary")
def get_summary(
    request: Request,
    knowledge_base: str | None = None,
    as_user: str | None = None,
    tenant: TenantConfig = Depends(require_tenant),
):
    """Node and relationship counts, plus which entity types are actually
    present, for the selected knowledge base. A quick "is there anything
    in here, and what kind of thing is it" view. Scoped further to
    as_user's visibility when given."""
    group_id = resolve_knowledge_base(tenant, knowledge_base)
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    user_id = authorization.resolve_as_user(group_id, as_user, repo=repo)

    if user_id is not None:
        node_count = authorization.get_visible_node_count(group_id, user_id, repo=repo)
        rel_count = authorization.get_visible_relationship_count(group_id, user_id, repo=repo)
        labels = authorization.get_visible_entity_types(group_id, user_id, repo=repo)
    else:
        # Every query in this route excludes NOT n:SaxonRecommendation. A
        # :SaxonRecommendation node is Saxon's own internal audit record of
        # a past generated causal recommendation (see app/graph/decisions.py),
        # not a business entity this "what does the graph actually know"
        # summary should count or list as a present entity type. This is
        # deliberately a narrower label than the ontology's own :Decision
        # entity type, since a real client dataset can have genuine
        # Decision business entities (an approval, a rejection) that must
        # NOT be hidden the same way. See GraphRepository._entity_own_facts's
        # docstring for the full story on why this exclusion exists.
        node_count = repo.execute_cypher(
            "MATCH (n:Entity {group_id: $group_id}) WHERE NOT n:SaxonRecommendation RETURN count(n) AS c",
            {"group_id": group_id},
        )[0]["c"]
        rel_count = repo.execute_cypher(
            "MATCH (a:Entity {group_id: $group_id})-[r:RELATES_TO]->(b:Entity {group_id: $group_id}) "
            "WHERE NOT a:SaxonRecommendation AND NOT b:SaxonRecommendation RETURN count(r) AS c",
            {"group_id": group_id},
        )[0]["c"]
        labels = [
            row["labels"]
            for row in repo.execute_cypher(
                "MATCH (n:Entity {group_id: $group_id}) WHERE NOT n:SaxonRecommendation RETURN DISTINCT labels(n) AS labels",
                {"group_id": group_id},
            )
        ]

    entity_types = sorted({label for label_list in labels for label in label_list if label != "Entity"})

    return {
        "group_id": group_id,
        "node_count": node_count,
        "relationship_count": rel_count,
        "entity_types_present": entity_types,
    }


@router.get("/nodes")
def get_nodes(
    request: Request,
    limit: int = 50,
    knowledge_base: str | None = None,
    as_user: str | None = None,
    connector_id: str | None = None,
    tenant: TenantConfig = Depends(require_tenant),
):
    """This knowledge base's nodes, most recently created first, scoped to
    as_user's visibility when given. ?connector_id= narrows further to only
    entities an episode from that one connector actually mentioned. See
    connector_id's own docstring below for why that's a separate filter
    from knowledge_base."""
    group_id = resolve_knowledge_base(tenant, knowledge_base)
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    limit = max(1, min(limit, 200))  # stops an unbounded ?limit= from becoming a full graph dump

    if connector_id is not None:
        return _get_nodes_for_connector(repo, group_id, connector_id, limit)

    user_id = authorization.resolve_as_user(group_id, as_user, repo=repo)
    if user_id is not None:
        return authorization.get_visible_nodes(group_id, user_id, limit=limit, repo=repo)

    return repo.execute_cypher(
        """
        MATCH (n:Entity {group_id: $group_id}) WHERE NOT n:SaxonRecommendation
        RETURN n.uuid AS id, n.name AS name, labels(n) AS labels, n.summary AS summary
        ORDER BY n.created_at DESC
        LIMIT $limit
        """,
        {"group_id": group_id, "limit": limit},
    )


def _get_nodes_for_connector(repo: GraphRepository, group_id: str, connector_id: str, limit: int):
    """Entities mentioned by an episode this specific connector produced
    (see app/ingestion/connector_sync.py's Episodic.connector_id tagging).
    This is not "everything in this connector's knowledge base," which is
    what the plain group_id filter above gives and is wrong here: a
    knowledge base commonly has more than one connector feeding it, so
    that used to show a connector's preview padded out with every other
    connector's facts too. Deliberately not combined with as_user, since
    this is "what did this source add," an operator/creator view, not a
    role-scoped end-user query."""
    return repo.execute_cypher(
        """
        MATCH (ep:Episodic {group_id: $group_id, connector_id: $connector_id})-[:MENTIONS]->(n:Entity)
        WHERE NOT n:SaxonRecommendation
        WITH DISTINCT n
        RETURN n.uuid AS id, n.name AS name, labels(n) AS labels, n.summary AS summary
        ORDER BY n.created_at DESC
        LIMIT $limit
        """,
        {"group_id": group_id, "connector_id": connector_id, "limit": limit},
    )


@router.get("/relationships")
def get_relationships(
    request: Request,
    limit: int = 50,
    knowledge_base: str | None = None,
    as_user: str | None = None,
    connector_id: str | None = None,
    tenant: TenantConfig = Depends(require_tenant),
):
    """This knowledge base's relationships, most recently created first,
    scoped to as_user's visibility when given. ?connector_id= narrows
    further. See get_nodes' matching parameter and
    _get_relationships_for_connector below."""
    group_id = resolve_knowledge_base(tenant, knowledge_base)
    repo = GraphRepository(neo4j_client=request.app.state.neo4j_client)
    limit = max(1, min(limit, 200))

    if connector_id is not None:
        return _get_relationships_for_connector(repo, group_id, connector_id, limit)

    user_id = authorization.resolve_as_user(group_id, as_user, repo=repo)
    if user_id is not None:
        return authorization.get_visible_relationships(group_id, user_id, limit=limit, repo=repo)

    return repo.execute_cypher(
        """
        MATCH (a:Entity {group_id: $group_id})-[r:RELATES_TO]->(b:Entity {group_id: $group_id})
        WHERE NOT a:SaxonRecommendation AND NOT b:SaxonRecommendation
        RETURN a.name AS source, r.name AS type, b.name AS target, r.fact AS fact
        ORDER BY r.created_at DESC
        LIMIT $limit
        """,
        {"group_id": group_id, "limit": limit},
    )


def _get_relationships_for_connector(repo: GraphRepository, group_id: str, connector_id: str, limit: int):
    """A RELATES_TO edge's own `episodes` property (already used by
    GraphRepository._resolve_episode_sources for per-fact source tags)
    lists which Episodic node(s) produced or last touched it. A fact
    counts as this connector's own when any of those episodes is one this
    connector wrote. Collecting this connector's episode uuids first in
    one small query, rather than joining per episode, avoids returning
    the same fact once for every episode that touched it."""
    return repo.execute_cypher(
        """
        MATCH (ep:Episodic {group_id: $group_id, connector_id: $connector_id})
        WITH collect(ep.uuid) AS connector_episode_uuids
        MATCH (a:Entity {group_id: $group_id})-[r:RELATES_TO]->(b:Entity {group_id: $group_id})
        WHERE NOT a:SaxonRecommendation AND NOT b:SaxonRecommendation
          AND ANY(ep_uuid IN r.episodes WHERE ep_uuid IN connector_episode_uuids)
        RETURN a.name AS source, r.name AS type, b.name AS target, r.fact AS fact
        ORDER BY r.created_at DESC
        LIMIT $limit
        """,
        {"group_id": group_id, "connector_id": connector_id, "limit": limit},
    )
