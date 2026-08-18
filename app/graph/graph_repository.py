# Sits between the rest of the app and the two ways this project talks to the graph:
# raw Cypher queries (Neo4j's query language, similar in spirit to SQL) via Neo4jClient,
# and Graphiti's own higher-level search API, which understands time ("what was true
# on this date") on top of the same underlying graph.
import logging
from typing import Any, Optional
from graphiti_core import Graphiti
from app.graph.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)


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

    async def search_graphiti_facts(
        self,
        query_text: str,
        group_ids: Optional[list[str]] = None,
        visible_uuids: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        """Query Graphiti for facts relevant to query_text, including whether each
        fact is still currently valid or has since been superseded/invalidated.

        `visible_uuids`, if given, drops any fact where *neither* end is
        something this caller directly owns -- role-based visibility for the
        Ask path (see app/graph/authorization.py). Graphiti's own search has
        no concept of this, so it's applied here as a post-filter; that's
        cheap rather than a scaling risk, since Graphiti already caps its own
        result count before this ever runs, so this loop is bounded by that
        cap, not by knowledge base size. Requiring only *one* end to be owned
        (not both) is deliberate: a visible customer's employer or assets
        should still show up as context about that customer, even though the
        employer/asset itself isn't separately assigned to anyone. What this
        does still prevent is a fact between two entities *neither* of which
        the caller owns just because it matched the search semantically.
        """
        if not self.graphiti:
            logger.warning("Graphiti instance not set in GraphRepository.")
            return []

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
