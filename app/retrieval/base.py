# Common shape every *text-query* retriever implements, so ContextOrchestrator
# can call a list of them without knowing which specific kind each one is.
#
# GraphRetriever implements this today. SemanticRetriever (vector search over
# documents) is the next one expected to -- its query/group_ids signature already
# matches. This exists now, ahead of semantic search actually being built, so
# that plugging it in later is "add one line to a list" in ContextOrchestrator
# rather than a restructure. LiveDataRetriever deliberately does NOT implement
# this: it looks up one specific entity_id, not a free-text query, so forcing it
# into this shape would distort its interface rather than simplify anything.
from typing import Any, Optional, Protocol


class TextRetriever(Protocol):
    async def retrieve(
        self, query: str, group_ids: Optional[list[str]] = None, visible_uuids: Optional[set[str]] = None
    ) -> list[dict[str, Any]]:
        ...
