# Common shape every text-query retriever implements, so ContextOrchestrator
# can call a list of them without knowing which specific kind each one is.
#
# GraphRetriever is the only implementation today, and semantic search isn't
# a separate retriever waiting to be added here. Graphiti's own hybrid
# search (semantic + BM25 + graph traversal) already runs as a fallback
# inside GraphRepository.search_graphiti_facts(), which GraphRetriever wraps
# (see app/context/orchestrator.py's module docstring for why). This
# interface stays in place for a genuinely different future retrieval
# source, e.g. a live external API lookup that shouldn't live in the graph
# at all, so plugging that in is "add one line to a list" in
# ContextOrchestrator rather than a restructure.
from typing import Any, Optional, Protocol


class TextRetriever(Protocol):
    async def retrieve(
        self,
        query: str,
        group_ids: Optional[list[str]] = None,
        visible_uuids: Optional[set[str]] = None,
        num_results: int = 8,
    ) -> list[dict[str, Any]]:
        ...
