# The main coordination point for answering a query: runs every configured
# retriever, pools what they find, and packages it into a ContextPacket.
#
# Only GraphRetriever is wired in today. It takes a list of retrievers (see
# app/retrieval/base.py's TextRetriever interface) specifically so that adding
# semantic search later is a one-line change here -- append a SemanticRetriever
# to the list -- rather than a restructure of this class.
#
# Note: this fills in ContextPacket.metadata with a plain-text summary and the
# raw per-fact records (including temporal validity), but not the packet's
# structured `facts` list -- turning retriever results into proper Fact model
# instances is a follow-up step, not yet implemented.
import logging
from typing import List, Optional
from graphiti_core import Graphiti
from app.models.context_packet import ContextPacket
from app.retrieval.base import TextRetriever
from app.retrieval.graph_retriever import GraphRetriever

logger = logging.getLogger(__name__)


class ContextOrchestrator:
    """Orchestrates multi-source context retrieval and packages the context response."""

    def __init__(self, graphiti_instance: Graphiti, extra_retrievers: Optional[List[TextRetriever]] = None):
        self.retrievers: List[TextRetriever] = [GraphRetriever(graphiti_instance)]
        if extra_retrievers:
            self.retrievers.extend(extra_retrievers)

    async def get_context_packet(self, query: str, group_ids: Optional[List[str]] = None) -> ContextPacket:
        raw_facts = []
        for retriever in self.retrievers:
            raw_facts.extend(await retriever.retrieve(query, group_ids=group_ids))

        fact_statements = [f["fact"] for f in raw_facts if f.get("is_valid", True)]
        summary_text = "\n".join(fact_statements) if fact_statements else "No matching graph context found."

        return ContextPacket(
            query=query,
            metadata={
                "group_ids": group_ids,
                "summary": summary_text,
                "facts": raw_facts,
            },
        )
