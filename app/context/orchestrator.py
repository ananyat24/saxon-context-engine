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
from datetime import datetime
from typing import List, Optional
from graphiti_core import Graphiti
from app.graph.neo4j_client import Neo4jClient
from app.models.context_packet import ContextPacket
from app.retrieval.base import TextRetriever
from app.retrieval.graph_retriever import GraphRetriever

logger = logging.getLogger(__name__)


def _parse_iso(timestamp) -> Optional[datetime]:
    """Accepts either an ISO string or a neo4j.time.DateTime -- valid_at/invalid_at
    arrive as the latter straight out of the driver, and only look like strings
    once FastAPI serializes the HTTP response, so this has to handle both."""
    if timestamp is None:
        return None
    if isinstance(timestamp, str):
        try:
            return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
    to_native = getattr(timestamp, "to_native", None)
    if callable(to_native):
        return to_native()
    return timestamp if isinstance(timestamp, datetime) else None


def _find_transitions(facts: list[dict]) -> tuple[list[tuple[dict, dict]], set[int]]:
    """Pairs an invalidated fact with whatever fact replaced it, so the summary can
    describe the change instead of just stating the latest value and burying the
    history in the raw facts list.

    Graphiti sets a fact's invalid_at to the valid_at of whatever new fact
    contradicted it, so matching on (source_node_uuid, timestamp) finds real
    replacements rather than unrelated facts that happen to share a source. When
    more than one old fact matches the same replacement (extraction sometimes
    produces several overlapping edges for one real-world change), this just
    keeps the first one to avoid repeating near-duplicate transition sentences.
    """
    by_source_and_valid_at: dict[tuple[str, str], dict] = {}
    for f in facts:
        if f.get("is_valid") and f.get("valid_at") and f.get("source_node_uuid"):
            by_source_and_valid_at.setdefault((f["source_node_uuid"], f["valid_at"]), f)

    transitions_by_replacement: dict[int, tuple[dict, dict]] = {}
    for f in facts:
        if f.get("is_valid") or not f.get("invalid_at") or not f.get("source_node_uuid"):
            continue
        replacement = by_source_and_valid_at.get((f["source_node_uuid"], f["invalid_at"]))
        if replacement is not None:
            transitions_by_replacement.setdefault(id(replacement), (f, replacement))

    transitions = list(transitions_by_replacement.values())
    replaced_fact_ids = {id(new) for _old, new in transitions}
    return transitions, replaced_fact_ids


def _describe_transition(old: dict, new: dict) -> str:
    changed_at = _parse_iso(new.get("valid_at"))
    when = f" on {changed_at:%B} {changed_at.day}, {changed_at.year}" if changed_at else ""
    return f'This changed{when}: it used to read "{old["fact"]}" and now reads "{new["fact"]}".'


class ContextOrchestrator:
    """Orchestrates multi-source context retrieval and packages the context response."""

    def __init__(
        self,
        graphiti_instance: Graphiti,
        extra_retrievers: Optional[List[TextRetriever]] = None,
        neo4j_client: Optional[Neo4jClient] = None,
    ):
        self.retrievers: List[TextRetriever] = [GraphRetriever(graphiti_instance, neo4j_client=neo4j_client)]
        if extra_retrievers:
            self.retrievers.extend(extra_retrievers)

    async def get_context_packet(
        self,
        query: str,
        group_ids: Optional[List[str]] = None,
        visible_uuids: Optional[set[str]] = None,
    ) -> ContextPacket:
        raw_facts = []
        for retriever in self.retrievers:
            raw_facts.extend(await retriever.retrieve(query, group_ids=group_ids, visible_uuids=visible_uuids))

        # A resolved entity's own summary (see GraphRepository._resolve_named_entity)
        # is already one consolidated, holistic statement about it, generated at
        # ingestion time -- lead with that rather than the individual edges below it.
        entity_summary_lines = [
            line
            for f in raw_facts
            if f.get("kind") == "entity_summary"
            for line in f["fact"].splitlines()
            if line.strip()
        ]
        edge_facts = [f for f in raw_facts if f.get("kind") != "entity_summary"]

        transitions, replaced_fact_ids = _find_transitions(edge_facts)
        transition_lines = [_describe_transition(old, new) for old, new in transitions]
        plain_lines = [
            f["fact"]
            for f in edge_facts
            if f.get("is_valid", True) and id(f) not in replaced_fact_ids
        ]

        # A resolved entity's summary can restate facts that are also present as
        # individual edges verbatim (Graphiti doesn't guarantee the two are
        # distinct) -- dedupe by exact text, keeping first occurrence, so the
        # same sentence never shows up twice.
        summary_lines = []
        seen = set()
        for line in entity_summary_lines + transition_lines + plain_lines:
            if line not in seen:
                seen.add(line)
                summary_lines.append(line)
        summary_text = "\n".join(summary_lines) if summary_lines else "No matching graph context found."

        return ContextPacket(
            query=query,
            metadata={
                "group_ids": group_ids,
                "summary": summary_text,
                "facts": raw_facts,
            },
        )
