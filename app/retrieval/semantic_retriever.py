from typing import Any, Dict, List


class SemanticRetriever:
    """Retriever for vector index search over unstructured context."""

    async def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        return []
