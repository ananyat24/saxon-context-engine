import logging
from typing import Any, Dict, List, Optional
from graphiti_core import Graphiti
from app.graph.neo4j_client import Neo4jClient
from app.models.entity import Entity
from app.models.fact import Fact

logger = logging.getLogger(__name__)


class GraphRepository:
    """Repository encapsulating Neo4j Cypher operations and Graphiti graph queries."""

    def __init__(self, graphiti_instance: Optional[Graphiti] = None):
        self.graphiti = graphiti_instance

    def execute_cypher(self, query: str, parameters: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Run a custom Cypher query against Neo4j using Neo4jClient."""
        client = Neo4jClient()
        results = []
        try:
            with client.driver.session() as session:
                res = session.run(query, parameters or {})
                results = [record.data() for record in res]
        finally:
            client.close()
        return results

    async def search_graphiti_facts(self, query_text: str, group_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Query Graphiti for hybrid semantic + temporal search facts."""
        if not self.graphiti:
            logger.warning("Graphiti instance not set in GraphRepository.")
            return []
        
        results = await self.graphiti.search(query_text, group_ids=group_ids)
        facts = []
        for r in results:
            facts.append({
                "fact": r.fact,
                "source_node_uuid": getattr(r, "source_node_uuid", ""),
                "target_node_uuid": getattr(r, "target_node_uuid", ""),
                "valid_at": getattr(r, "valid_at", None),
                "invalid_at": getattr(r, "invalid_at", None),
                "expired_at": getattr(r, "expired_at", None),
                "is_valid": getattr(r, "expired_at", None) is None and getattr(r, "invalid_at", None) is None,
            })
        return facts
