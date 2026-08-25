# Sits between the rest of the app and the two ways this project talks to the graph:
# raw Cypher queries (Neo4j's query language, similar in spirit to SQL) via Neo4jClient,
# and Graphiti's own higher-level search API, which understands time ("what was true
# on this date") on top of the same underlying graph.
import asyncio
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

# Records ingested without a human-readable name (see FileSourceSpec.name_column
# in app/ingestion/file_source.py) end up named "<Type> <id>", e.g. "Order
# 10248" -- a single capitalized word plus a numeric id, which _PROPER_NOUN_RE
# above never matches (it requires two consecutive capitalized words). Without
# this, "order 10248" falls through to plain semantic search, which has no
# relevance threshold and pads the answer out with facts about several other,
# unrelated orders that just happen to score similarly. This is deliberately
# looser (case-insensitive, no second-word capitalization requirement) so it
# also catches how people actually type these queries -- lowercase, "#" before
# the number, etc. Precisely because it's loose, an unresolved match here must
# NOT be treated the way an unresolved proper noun is (see _resolve_named_entities)
# -- "since 2023" or "in 2024" would also match this pattern and aren't meant to
# short-circuit an otherwise normal query into a false "not found".
_ID_PHRASE_RE = re.compile(r"\b[A-Za-z][\w'.-]*\s+#?[\w-]*\d[\w-]*\b")


def _extract_candidate_entities(query_text: str) -> list[str]:
    candidates = set(_PROPER_NOUN_RE.findall(query_text))
    return sorted(candidates, key=len, reverse=True)


def _extract_id_candidates(query_text: str) -> list[str]:
    phrases = set(_ID_PHRASE_RE.findall(query_text))
    # Extraction sometimes drops the type-word prefix for one record but not
    # its siblings -- e.g. four Northwind orders end up named "Order 10250"
    # etc., but one ends up named just "10248", an inconsistency in how the
    # ingesting LLM happened to name that one record, not something a query
    # can know about. Without this, "what's the status of order 10248"
    # produces a candidate ("order 10248") that CONTAINS-matches nothing --
    # too long to be found inside the shorter actual name -- so resolution
    # silently fails and the query falls through to padded semantic search
    # instead of the one order actually asked about. Adding just the bare
    # trailing token (the id itself) as its own candidate covers that case
    # too, without loosening the match logic itself to accept shorter
    # substrings generally.
    bare_ids = {phrase.rsplit(" ", 1)[-1] for phrase in phrases if " " in phrase}
    return sorted(phrases | bare_ids, key=len, reverse=True)


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

    def _match_entity_by_name(self, name: str, group_ids: list[str]) -> Optional[dict[str, Any]]:
        """One round trip instead of two: tries an exact match and a substring
        match in a single query (ORDER BY prefers the exact match when both
        would hit), rather than a separate follow-up query only issued when the
        first came back empty. Matches across every connector in group_ids --
        a multi-connector document set (see app/graph/document_sets.py) needs
        entity resolution to work across all of them, not just the first."""
        rows = self.execute_cypher(
            "MATCH (n:Entity) WHERE n.group_id IN $group_ids "
            "AND (toLower(n.name) = toLower($name) OR toLower(n.name) CONTAINS toLower($name)) "
            "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary "
            "ORDER BY CASE WHEN toLower(n.name) = toLower($name) THEN 0 ELSE 1 END LIMIT 1",
            {"group_ids": group_ids, "name": name},
        )
        return rows[0] if rows else None

    async def _resolve_named_entities(
        self, query_text: str, group_ids: list[str], visible_uuids: Optional[set[str]]
    ) -> tuple[list[dict[str, Any]], bool]:
        """Looks for specifically-named entities in the query and matches each
        against real node names in this knowledge base, so a query like "What's
        changed about Rhodes Furniture?" or "What's the status of order 10248?"
        can be grounded to that exact node instead of left to semantic search
        to guess at.

        Two candidate sources feed this: proper nouns (_extract_candidate_entities,
        e.g. "Rhodes Furniture") and id-style phrases (_extract_id_candidates,
        e.g. "order 10248"). Only a proper noun that fails to resolve counts as
        saw_unresolved_candidate -- an id-style phrase is loose enough to also
        match ordinary text like "since 2023", so an unresolved one there just
        falls through to normal search instead of forcing a "not found".

        Every candidate's lookup is independent, so they run concurrently (each
        off the event loop via to_thread, since execute_cypher is a blocking
        call) instead of one after another -- a query naming two entities (e.g.
        "How is X connected to Y?") no longer pays for two round trips back to
        back.

        Returns (resolved_rows, saw_unresolved_candidate) -- resolved_rows is
        deduped by uuid (so "Contoso Store Washington DC" and, say, an overlapping
        shorter candidate matching the same node don't count twice), and
        saw_unresolved_candidate is True if some proper-noun-shaped phrase in the
        query didn't match anything visible, which the caller uses to say "not
        found" rather than silently falling back to an ungrounded search.
        """
        proper_nouns = _extract_candidate_entities(query_text)
        all_candidates = proper_nouns + _extract_id_candidates(query_text)
        if not all_candidates:
            return [], False

        rows = await asyncio.gather(
            *(asyncio.to_thread(self._match_entity_by_name, c, group_ids) for c in all_candidates)
        )

        resolved: dict[str, dict[str, Any]] = {}
        saw_unresolved = False
        proper_noun_set = set(proper_nouns)
        for candidate, row in zip(all_candidates, rows):
            if row and (visible_uuids is None or row["uuid"] in visible_uuids):
                resolved[row["uuid"]] = row
            elif candidate in proper_noun_set:
                # Either nothing matched, or it matched something this caller
                # can't see -- don't leak existence either way. An id-phrase
                # candidate that misses is expected (see docstring) and never
                # counts here.
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
        num_results: int = 8,
    ) -> list[dict[str, Any]]:
        """Query for facts relevant to query_text, including whether each fact is
        still currently valid or has since been superseded/invalidated.

        num_results only bounds the fallback semantic-search branch at the
        bottom of this method (a named entity resolved via _resolve_named_entities
        returns all of that entity's own edges regardless -- see below).

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

        resolved_entities, saw_unresolved = (
            await self._resolve_named_entities(query_text, group_ids, visible_uuids) if group_ids else ([], False)
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
            # (precise by construction). Also skips a paid search call we'd
            # otherwise throw away.
            resolved = resolved_entities[0]
            facts = self._entity_own_facts(resolved["uuid"], visible_uuids)
            if not facts and resolved.get("summary"):
                # Only fall back to the entity's own `summary` property when it
                # has no edges at all (e.g. an entity extracted with rich
                # attributes but no relationships -- see the "Contoso Store
                # Washington DC" case this was built for). When edges DO exist,
                # summary must NOT be used: Graphiti's node summary is a running
                # accumulation of every fact ever seen about the entity, with no
                # temporal awareness -- for "Contoso Ltd", it still lists "Sarah
                # Chen manages the account" verbatim even after Marcus Lee took
                # over. Showing that as a lead line reintroduces exactly the
                # inaccurate, already-superseded info the orchestrator's
                # transition/is_valid handling exists to filter out.
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

        # Graphiti's own default (10) is tuned for open-ended browsing, not a
        # single answer -- trimmed by default since every extra fact both adds
        # noise to the response and lengthens the synthesis call that follows
        # it. The caller (see app/api/context.py's result_limit) can still ask
        # for more on a broad question via a "see more results" follow-up,
        # rather than everyone paying for a bigger default every time.
        results = await self.graphiti.search(query_text, group_ids=group_ids, num_results=num_results)
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
                # Distinguishes these from the entity-resolution branches above
                # (whose facts carry no "kind", or "entity_summary") -- the
                # orchestrator uses this to tell whether num_results actually
                # capped anything, since only this branch is capped at all.
                "kind": "semantic_search",
            })
        return facts
