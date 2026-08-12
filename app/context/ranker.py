# Placeholder for reranking retrieved items before they're composed into a
# ContextPacket -- e.g. boosting items that are more semantically relevant to the
# query, or down-weighting facts that are older / closer to their valid_to cutoff.
# Currently a no-op that returns items in whatever order they arrived in.
from typing import Any


class ContextRanker:
    """Reranks retrieved context items based on query relevance and temporal decay."""

    def rank(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return items
