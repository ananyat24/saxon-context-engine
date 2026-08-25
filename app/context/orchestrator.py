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
from graphiti_core.prompts.models import Message
from pydantic import BaseModel
from app.graph.neo4j_client import Neo4jClient
from app.models.context_packet import ContextPacket
from app.retrieval.base import TextRetriever
from app.retrieval.graph_retriever import GraphRetriever

logger = logging.getLogger(__name__)


class _SynthesizedAnswer(BaseModel):
    answer: str


# Kept low deliberately -- this is one short sentence, not a report. Also
# bounds the cost of a call that (unlike extraction) runs on every multi-fact
# query, not just at ingestion time.
_SYNTHESIS_MAX_TOKENS = 80


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
        self.graphiti = graphiti_instance
        self.retrievers: List[TextRetriever] = [GraphRetriever(graphiti_instance, neo4j_client=neo4j_client)]
        if extra_retrievers:
            self.retrievers.extend(extra_retrievers)

    async def _synthesize_answer(self, query: str, current_lines: list[str]) -> str:
        """Condenses several current facts into one clear sentence answering
        `query`, via a small, tightly-bounded LLM call -- used only when
        there's more than one current fact (a single fact is already a clear
        one-line answer on its own, so that case skips this entirely, at
        zero extra cost).

        Deliberately does NOT use each entity's Graphiti-generated `summary`
        for this (see GraphRepository._resolve_named_entities) -- that
        summary accumulates every fact ever seen with no temporal awareness,
        which is exactly the stale-answer bug fixed in graph_repository.py.
        This only ever sees the already-filtered current_lines, so it can't
        reintroduce a superseded fact as part of the "answer."

        Falls back to a plain joined list on any failure (including the
        local spend cap being hit) -- a failed synthesis should degrade to
        the older, plainer behavior, not break the whole query.
        """
        try:
            messages = [
                Message(
                    role="system",
                    content=(
                        "You answer questions using ONLY the facts given -- never add, infer, "
                        "or assume anything not explicitly stated. Respond with one clear, "
                        "concise sentence."
                    ),
                ),
                Message(
                    role="user",
                    content=(
                        f"Question: {query}\n\nFacts:\n"
                        + "\n".join(f"- {line}" for line in current_lines)
                    ),
                ),
            ]
            result = await self.graphiti.llm_client.generate_response(
                messages, response_model=_SynthesizedAnswer, max_tokens=_SYNTHESIS_MAX_TOKENS
            )
            answer = result.get("answer", "").strip()
            return answer or "\n".join(current_lines)
        except Exception as e:
            logger.warning(f"Answer synthesis failed, falling back to a plain fact list: {e}")
            return "\n".join(current_lines)

    async def get_context_packet(
        self,
        query: str,
        group_ids: Optional[List[str]] = None,
        visible_uuids: Optional[set[str]] = None,
        num_results: int = 8,
    ) -> ContextPacket:
        raw_facts = []
        for retriever in self.retrievers:
            raw_facts.extend(
                await retriever.retrieve(query, group_ids=group_ids, visible_uuids=visible_uuids, num_results=num_results)
            )

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
        if not summary_lines:
            summary_text = "No matching graph context found."
        elif len(summary_lines) == 1:
            # Already a single clear statement -- synthesizing would just cost
            # a call to say the same thing differently.
            summary_text = summary_lines[0]
        else:
            summary_text = await self._synthesize_answer(query, summary_lines)

        # Only the semantic-search fallback (see GraphRepository.search_graphiti_facts)
        # is ever capped by num_results -- a resolved entity's own facts always
        # come back in full. Hitting the cap there is the signal a client uses
        # to offer "see more results" instead of always guessing at one.
        semantic_result_count = sum(1 for f in raw_facts if f.get("kind") == "semantic_search")
        result_limit_hit = semantic_result_count >= num_results

        return ContextPacket(
            query=query,
            metadata={
                "group_ids": group_ids,
                "summary": summary_text,
                "facts": raw_facts,
                "result_limit_hit": result_limit_hit,
            },
        )
