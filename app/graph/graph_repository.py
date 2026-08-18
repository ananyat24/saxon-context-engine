# Sits between the rest of the app and the two ways this project talks to the graph:
# raw Cypher queries (Neo4j's query language, similar in spirit to SQL) via Neo4jClient,
# and Graphiti's own higher-level search API, which understands time ("what was true
# on this date") on top of the same underlying graph.
import logging
import re
from typing import Any, Optional
from graphiti_core import Graphiti
from app.graph.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

# Graphiti's search ranks edges by RRF-fused vector/text similarity with no
# relevance threshold -- it always returns its top-N, even when nothing in the
# graph is actually related to the query. That's fine for open-ended questions,
# but for a query naming a specific entity (e.g. "What's changed about Rhodes
# Furniture?") it means an entity with few or no edges of its own gets padded
# out with other entities' unrelated facts that just happen to read similarly.
# This regex pulls out multi-word capitalized phrases so a named entity can be
# resolved directly against the graph and its own edges/summary used instead.
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*)+\b")


def _extract_candidate_entities(query_text: str) -> list[str]:
    candidates = set(_PROPER_NOUN_RE.findall(query_text))
    return sorted(candidates, key=len, reverse=True)


class GraphRepository:
    """Repository encapsulating Neo4j Cypher operations and Graphiti graph queries."""

    def __init__(
        self,
        graphiti_instance: Optional[Graphiti] = None,
        neo4j_client: Optional[Neo4jClient] = None,
    ):
        self.graphiti = graphiti_instance
        # If a caller hands us a Neo4jClient, reuse its connection pool across every
        # execute_cypher() call. If not, we fall back to opening a short-lived client
        # per call (see execute_cypher) -- less efficient, but keeps this class usable
        # with zero setup for one-off scripts and tests.
        self._owned_client = neo4j_client

    def execute_cypher(self, query: str, parameters: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """Run a Cypher query against Neo4j and return each result row as a dict."""
        client = self._owned_client or Neo4jClient()
        try:
            with client.driver.session() as session:
                result = session.run(query, parameters or {})
                return [record.data() for record in result]
        finally:
            # Only close a client we created ourselves for this call -- closing one
            # the caller gave us would break their ability to reuse it afterward.
            if client is not self._owned_client:
                client.close()

    def _resolve_named_entities(
        self, query_text: str, group_id: str, visible_uuids: Optional[set[str]]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Looks for specifically-named entities in the query and matches each
        against real node names in this knowledge base, so a query like "What's
        changed about Rhodes Furniture?" can be grounded to that exact node
        instead of left to semantic search to guess at.

        Returns (resolved_rows, saw_unresolved_candidate) -- resolved_rows is
        deduped by uuid (so "Contoso Store Washington DC" and, say, an overlapping
        shorter candidate matching the same node don't count twice), and
        saw_unresolved_candidate is True if some name-shaped phrase in the query
        didn't match anything visible, which the caller uses to say "not found"
        rather than silently falling back to an ungrounded search.
        """
        candidates = _extract_candidate_entities(query_text)
        resolved: dict[str, dict[str, Any]] = {}
        saw_unresolved = False

        for candidate in candidates:
            rows = self.execute_cypher(
                "MATCH (n:Entity {group_id: $group_id}) WHERE toLower(n.name) = toLower($name) "
                "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary LIMIT 1",
                {"group_id": group_id, "name": candidate},
            )
            if not rows:
                rows = self.execute_cypher(
                    "MATCH (n:Entity {group_id: $group_id}) WHERE toLower(n.name) CONTAINS toLower($name) "
                    "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary LIMIT 1",
                    {"group_id": group_id, "name": candidate},
                )
            if rows and (visible_uuids is None or rows[0]["uuid"] in visible_uuids):
                resolved[rows[0]["uuid"]] = rows[0]
            else:
                # Either nothing matched, or it matched something this caller
                # can't see -- don't leak existence either way.
                saw_unresolved = True

        return list(resolved.values()), saw_unresolved

    @staticmethod
    def _to_native(value):
        """execute_cypher() returns raw neo4j.time.DateTime objects (unlike
        Graphiti's own search results, which come back as plain datetimes) --
        FastAPI can't serialize those, so anything read via execute_cypher and
        handed back as a fact needs this before it can go in an API response."""
        to_native = getattr(value, "to_native", None)
        return to_native() if callable(to_native) else value

    def _entity_own_facts(self, uuid: str, visible_uuids: Optional[set[str]]) -> list[dict[str, Any]]:
        """Pulls every edge directly touching a resolved entity straight from
        Neo4j -- precise by construction, unlike semantic search, since it can
        only ever return facts that are actually about this entity."""
        rows = self.execute_cypher(
            "MATCH (n:Entity {uuid: $uuid})-[r:RELATES_TO]-(m) "
            "RETURN r.fact AS fact, r.valid_at AS valid_at, r.invalid_at AS invalid_at, "
            "r.expired_at AS expired_at, startNode(r).uuid AS source_node_uuid, "
            "endNode(r).uuid AS target_node_uuid",
            {"uuid": uuid},
        )
        facts = []
        for row in rows:
            if (
                visible_uuids is not None
                and row["source_node_uuid"] not in visible_uuids
                and row["target_node_uuid"] not in visible_uuids
            ):
                continue
            expired_at = self._to_native(row["expired_at"])
            invalid_at = self._to_native(row["invalid_at"])
            facts.append({
                "fact": row["fact"],
                "source_node_uuid": row["source_node_uuid"],
                "target_node_uuid": row["target_node_uuid"],
                "valid_at": self._to_native(row["valid_at"]),
                "invalid_at": invalid_at,
                "expired_at": expired_at,
                "is_valid": expired_at is None and invalid_at is None,
            })
        return facts

    def _relationship_path_facts(
        self, uuid_a: str, uuid_b: str, visible_uuids: Optional[set[str]]
    ) -> Optional[list[str]]:
        """Finds the shortest chain of facts connecting two resolved entities, so
        "How is X connected to Y?" answers the actual question asked instead of
        just describing one of the two entities. Bounded to 4 hops -- beyond that
        a "connection" is really just everything-connects-to-everything noise,
        not a meaningful answer. Returns None if no such path exists (or none of
        its nodes are visible to this caller)."""
        rows = self.execute_cypher(
            "MATCH p = shortestPath((a:Entity {uuid: $uuid_a})-[:RELATES_TO*1..4]-(b:Entity {uuid: $uuid_b})) "
            "RETURN [rel IN relationships(p) | rel.fact] AS facts, "
            "[n IN nodes(p) | n.uuid] AS path_uuids",
            {"uuid_a": uuid_a, "uuid_b": uuid_b},
        )
        if not rows or not rows[0]["facts"]:
            return None
        row = rows[0]
        if visible_uuids is not None and not all(u in visible_uuids for u in row["path_uuids"]):
            return None
        return row["facts"]

    async def search_graphiti_facts(
        self,
        query_text: str,
        group_ids: Optional[list[str]] = None,
        visible_uuids: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        """Query for facts relevant to query_text, including whether each fact is
        still currently valid or has since been superseded/invalidated.

        `visible_uuids`, if given, drops any fact where *neither* end is
        something this caller directly owns -- role-based visibility for the
        Ask path (see app/graph/authorization.py). Requiring only *one* end to
        be owned (not both) is deliberate: a visible customer's employer or
        assets should still show up as context about that customer, even
        though the employer/asset itself isn't separately assigned to anyone.
        What this does still prevent is a fact between two entities *neither*
        of which the caller owns just because it matched the search
        semantically.

        If the query names one or more specific entities (e.g. "What's changed
        about Rhodes Furniture?" or "How is X connected to Y?"), those names are
        resolved against the graph first instead of relying on Graphiti's hybrid
        search -- which has no relevance threshold and will happily pad out a
        sparsely-connected entity with other entities' unrelated facts that
        merely read similarly, or answer a two-entity connection question by
        just describing one of them. A single resolved entity gets its own
        edges/summary directly; two resolved entities get the path of facts
        connecting them, if any. A name that doesn't resolve to anything real
        short-circuits with a clear "not found" rather than substituting
        unrelated results.
        """
        if not self.graphiti:
            logger.warning("Graphiti instance not set in GraphRepository.")
            return []

        group_id = group_ids[0] if group_ids else None
        resolved_entities, saw_unresolved = (
            self._resolve_named_entities(query_text, group_id, visible_uuids) if group_id else ([], False)
        )

        if saw_unresolved and not resolved_entities:
            return [{
                "fact": "No entity matching that name was found in this knowledge base.",
                "source_node_uuid": "",
                "target_node_uuid": "",
                "valid_at": None,
                "invalid_at": None,
                "expired_at": None,
                "is_valid": True,
            }]

        if len(resolved_entities) >= 2:
            a, b = resolved_entities[0], resolved_entities[1]
            path_facts = self._relationship_path_facts(a["uuid"], b["uuid"], visible_uuids)
            if path_facts:
                return [{
                    "fact": fact,
                    "source_node_uuid": "",
                    "target_node_uuid": "",
                    "valid_at": None,
                    "invalid_at": None,
                    "expired_at": None,
                    "is_valid": True,
                } for fact in path_facts]
            return [{
                "fact": f'No connection found between "{a["name"]}" and "{b["name"]}" in this knowledge base.',
                "source_node_uuid": "",
                "target_node_uuid": "",
                "valid_at": None,
                "invalid_at": None,
                "expired_at": None,
                "is_valid": True,
            }]

        if len(resolved_entities) == 1:
            # A single named entity resolved -- use its own edges directly
            # (precise by construction) plus its summary as a single,
            # already-consolidated lead line. Also skips a paid search call
            # we'd otherwise throw away.
            resolved = resolved_entities[0]
            facts = self._entity_own_facts(resolved["uuid"], visible_uuids)
            if resolved.get("summary"):
                facts.insert(0, {
                    "fact": resolved["summary"],
                    "source_node_uuid": resolved["uuid"],
                    "target_node_uuid": resolved["uuid"],
                    "valid_at": None,
                    "invalid_at": None,
                    "expired_at": None,
                    "is_valid": True,
                    "kind": "entity_summary",
                })
            return facts

        results = await self.graphiti.search(query_text, group_ids=group_ids)
        facts = []
        for r in results:
            source_uuid = getattr(r, "source_node_uuid", "")
            target_uuid = getattr(r, "target_node_uuid", "")
            if visible_uuids is not None and source_uuid not in visible_uuids and target_uuid not in visible_uuids:
                continue
            facts.append({
                "fact": r.fact,
                "source_node_uuid": source_uuid,
                "target_node_uuid": target_uuid,
                "valid_at": getattr(r, "valid_at", None),
                "invalid_at": getattr(r, "invalid_at", None),
                "expired_at": getattr(r, "expired_at", None),
                "is_valid": getattr(r, "expired_at", None) is None and getattr(r, "invalid_at", None) is None,
            })
        return facts
