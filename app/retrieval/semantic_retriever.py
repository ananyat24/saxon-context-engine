# Placeholder for a vector-similarity search path (e.g. over document chunks stored
# in a vector index) that's independent of Neo4j's graph traversal / Graphiti's own
# search. Not implemented yet -- there is no vector index configured in this project
# at this stage, so this always returns an empty result rather than pretending to
# search something that doesn't exist.
from typing import Any


class SemanticRetriever:
    """Retriever for vector index search over unstructured context."""

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        return []
