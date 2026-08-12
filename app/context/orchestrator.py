# The main coordination point for answering a query: currently calls the graph
# retriever and packages what it finds into a ContextPacket. As semantic and
# live-data retrievers are implemented (see app/retrieval/), this is where their
# results would get merged in alongside the graph facts.
#
# Note: this only fills in ContextPacket.metadata with a plain-text summary, not
# the packet's structured `facts` list -- turning Graphiti's raw search hits into
# proper Fact model instances is a follow-up step, not yet implemented.
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
                "group_ids": group_ids,
                "summary": summary_text,
            },
        )
