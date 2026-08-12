import logging
from typing import List, Optional
from graphiti_core import Graphiti
from app.models.context_packet import ContextPacket
from app.retrieval.graph_retriever import GraphRetriever

logger = logging.getLogger(__name__)


class ContextOrchestrator:
    """Orchestrates multi-source context retrieval and packages the context response."""

    def __init__(self, graphiti_instance: Graphiti):
        self.graph_retriever = GraphRetriever(graphiti_instance)

    async def get_context_packet(self, query: str, group_ids: Optional[List[str]] = None) -> ContextPacket:
        raw_facts = await self.graph_retriever.retrieve_context_facts(query, group_ids=group_ids)
        fact_statements = [f["fact"] for f in raw_facts if f.get("is_valid", True)]
        summary_text = "\n".join(fact_statements) if fact_statements else "No matching graph context found."

        return ContextPacket(
            query=query,
            metadata={
                "group_id": group_ids[0] if group_ids else None,
                "summary": summary_text,
            },
        )
