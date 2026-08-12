# Wraps GraphRepository's Graphiti search behind the TextRetriever interface
# (see app/retrieval/base.py), so ContextOrchestrator can treat this the same way
# it will eventually treat a semantic retriever, instead of talking to
# GraphRepository directly.
import logging
from typing import Any, Optional
from graphiti_core import Graphiti
from app.graph.graph_repository import GraphRepository

logger = logging.getLogger(__name__)


class GraphRetriever:
    """Retriever responsible for querying graph context from Graphiti and Neo4j."""

    def __init__(self, graphiti_instance: Graphiti):
        self.graphiti = graphiti_instance
        self.repository = GraphRepository(graphiti_instance)

    async def retrieve(
        self, query: str, group_ids: Optional[list[str]] = None
    ) -> list[dict[str, Any]]:
        logger.info(f"Retrieving graph context for query: '{query}'")
        return await self.repository.search_graphiti_facts(query, group_ids=group_ids)
