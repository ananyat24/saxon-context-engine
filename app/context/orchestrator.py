# The main coordination point for answering a query: runs every configured
# retriever, pools what they find, and packages it into a ContextPacket.
#
# Only GraphRetriever is wired in, and that's deliberate, not a placeholder
# waiting on a separate semantic-search retriever. Graphiti already ships
# hybrid search (semantic + BM25 + graph traversal); GraphRepository.
# search_graphiti_facts() calls it directly as its own fallback branch, only
# after trying to resolve the query to a specific named entity first (see
# that method's docstring). That resolve-first-fall-back-to-search-second
# decision is this system's query planner: it already skips the paid
# hybrid-search call whenever a named entity answers the question directly,
# which is the actual cost-saving goal a separate ContextPlanner class would
# have existed to achieve. A standalone vector-index-backed SemanticRetriever
# was scaffolded early on and later removed once it became clear Graphiti's
# own hybrid search covers this without a second index to maintain.
#
# This retriever list still exists (rather than calling GraphRetriever
# directly) so a genuinely different retrieval source, such as live external
# data that shouldn't live in the graph at all, can be appended here later
# without restructuring this class.
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
from app.graph import connectors
from app.graph.decisions import record_decision
from app.graph.graph_repository import GraphRepository
from app.graph.neo4j_client import Neo4jClient
from app.models.context_packet import ContextPacket
from app.retrieval.base import TextRetriever
from app.retrieval.graph_retriever import GraphRetriever

logger = logging.getLogger(__name__)


class _SynthesizedAnswer(BaseModel):
    answer: str


class _CausalRecommendation(BaseModel):
    what_happened: str
    why: str
    impact: str
    recommendation: str


# Kept short for the same reason _SYNTHESIS_MAX_TOKENS is: this runs on every
# causal query, not just at ingestion time, and a recommendation is meant to
# be a few sentences a person can act on, not a report. Raised from 300 after
# it was found live that routing more (real, historical) facts into
# chain_lines (see _build_answer_lines's use above) means a richer
# chain_lines input pushes the model to reference more of it in the answer,
# and 300 tokens wasn't enough headroom for all 4 structured fields to
# complete. The response got cut off mid-field, which is a pydantic
# validation failure (a field is simply missing), not a content problem, and
# silently degraded every such query to no recommendation at all.
_RECOMMENDATION_MAX_TOKENS = 500


# Kept low deliberately: this is one short sentence, not a report. Also
# bounds the cost of a call that (unlike extraction) runs on every multi-fact
# query, not just at ingestion time. Raised from 80 for the same reason
# _RECOMMENDATION_MAX_TOKENS was: _build_answer_lines now feeds real
# historical facts in too, and 80 tokens left no margin before a longer
# synthesized sentence got cut off mid-JSON-field and failed validation.
_SYNTHESIS_MAX_TOKENS = 150


def _parse_iso(timestamp) -> Optional[datetime]:
    """Accepts either an ISO string or a neo4j.time.DateTime: valid_at/invalid_at
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


def _build_answer_lines(facts: list[dict]) -> list[str]:
    """Turns a raw, possibly mixed valid/invalid fact list into the plain-
    English lines _synthesize_answer (or a single-line shortcut) should
    see. Shared by the plain Ask path (get_context_packet) and the
    causal path's fact-only fallbacks (_fact_only_causal_packet), which
    used to each filter to `is_valid` facts only and silently drop
    everything else.

    Three kinds of line, in this order: a transition description for every
    invalidated fact _find_transitions could pair with whatever replaced
    it ("this changed on <date>: it used to read X, now reads Y"); every
    still-current fact, as-is; and, the actual fix, every other
    invalidated fact, one _find_transitions couldn't pair (its
    replacement wasn't in this particular batch), explicitly marked as no
    longer current rather than silently dropped. Real historical
    information a person asked about shouldn't vanish just because
    nothing superseded it in this retrieval. Found for real against
    live data: a question whose only relevant fact was exactly this kind
    of unpaired-invalid fact got "No matching graph context found" as its
    answer, even though metadata.facts (the raw, unfiltered response) had
    the fact the whole time.

    A fact with `is_authoritative: False` (see _apply_authority_tie_break)
    is excluded throughout, same as before this function existed.
    """
    transitions, replaced_fact_ids = _find_transitions(facts)
    transition_lines = [_describe_transition(old, new) for old, new in transitions]
    old_fact_ids_in_transitions = {id(old) for old, _new in transitions}

    plain_lines = []
    historical_lines = []
    for f in facts:
        if id(f) in replaced_fact_ids or not f.get("is_authoritative", True):
            continue
        if f.get("is_valid", True):
            plain_lines.append(f["fact"])
        elif id(f) not in old_fact_ids_in_transitions:
            historical_lines.append(f'No longer current, but real: "{f["fact"]}"')

    seen: set[str] = set()
    lines: list[str] = []
    for line in transition_lines + plain_lines + historical_lines:
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


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
        # Used both for the causal-chain walk and the source-authority tie
        # break. Neither is a Graphiti hybrid-search call, so both go
        # straight to Neo4j rather than through a retriever.
        self._repo = GraphRepository(graphiti_instance, neo4j_client=neo4j_client)

    def _apply_authority_tie_break(self, tenant_id: str, edge_facts: list[dict]) -> None:
        """When two different connectors' facts disagree about the same
        relationship (same source/target pair and relationship type, both
        currently valid, different text, and actually attributed to more
        than one group_id), marks the one from the higher-authority
        connector `is_authoritative` and the other(s) not. See
        app/graph/connectors.py's source_authority field. Every fact still
        comes back in the response's facts list either way (see
        get_context_packet below); this only decides which lines feed the
        synthesized answer, per the pivot's explicit "don't use it to hide
        or filter anything" rule. Mutates edge_facts in place, adding
        "source_authority" and "is_authoritative" to every entry.

        Two things had to be tightened after testing against real data,
        both cases where a group of facts looked like a disagreement
        under the original (source, target) grouping but was really just
        several true, complementary facts, and treating them as a
        disagreement silently dropped one from the answer:

        1. Grouping by relationship_type too (not just the node pair): an
           order's ship city and ship country point at the same two nodes
           via different edges.
        2. Requiring the group to actually span more than one group_id: it
           turns out a single connector can extract two same-typed,
           same-pair edges that are still both true (found for real in the
           northwind data: "Order 10252 was shipped to the city of
           Charleroi" and "...to the country of Belgium" are both
           LOCATED_AT edges from the same connector). source_authority
           ranks one connector's data over another's, so arbitrating
           between a connector and itself is never meaningful; only a
           group whose facts come from 2+ distinct group_ids is a real
           cross-source disagreement worth breaking a tie on.

        A fact with no real source/target uuid (e.g. the "how is X connected
        to Y" path-lookup branch in search_graphiti_facts, which returns
        every hop with source_node_uuid/target_node_uuid both "") is skipped
        from grouping entirely, rather than left to collide on that shared
        blank key. Otherwise every hop of a multi-hop path looks like one
        giant disagreement and the answer collapses to a single hop.
        """
        authority_map = connectors.authority_by_group_id(tenant_id, repo=self._repo)
        for f in edge_facts:
            f["source_authority"] = authority_map.get(f.get("group_id"), 0)
            f["is_authoritative"] = True

        groups: dict[tuple, list[dict]] = {}
        for f in edge_facts:
            if not f.get("is_valid", True):
                continue
            if not f.get("source_node_uuid") or not f.get("target_node_uuid"):
                continue
            key = (f.get("source_node_uuid"), f.get("target_node_uuid"), f.get("relationship_type"))
            groups.setdefault(key, []).append(f)

        for facts_for_pair in groups.values():
            distinct_texts = {f["fact"] for f in facts_for_pair}
            distinct_sources = {f.get("group_id") for f in facts_for_pair}
            if len(distinct_texts) <= 1 or len(distinct_sources) <= 1:
                continue  # no disagreement, nothing to break a tie on
            winner = max(facts_for_pair, key=lambda f: f["source_authority"])
            for f in facts_for_pair:
                f["is_authoritative"] = f is winner

    async def _synthesize_answer(self, query: str, current_lines: list[str]) -> str:
        """Condenses several current facts into one clear sentence answering
        `query`, via a small, tightly-bounded LLM call. Used only when
        there's more than one current fact (a single fact is already a clear
        one-line answer on its own, so that case skips this entirely, at
        zero extra cost).

        Deliberately does NOT use each entity's Graphiti-generated `summary`
        for this (see GraphRepository._resolve_named_entities): that
        summary accumulates every fact ever seen with no temporal awareness,
        which is exactly the stale-answer bug fixed in graph_repository.py.
        This only ever sees the already-filtered current_lines, so it can't
        reintroduce a superseded fact as part of the "answer."

        Falls back to a plain joined list on any failure (including the
        local spend cap being hit): a failed synthesis should degrade to
        the older, plainer behavior, not break the whole query.

        The system prompt explicitly guards against one specific failure
        mode found live: "Who approved the expedited fix, and what did it
        cost?" answered "Grace Okafor approved..." when the only retrieved
        fact was "Owen Whitfield requests a budget exception FROM Grace
        Okafor". A request addressed to someone is not that person acting
        on it, but the model conflated the two rather than noticing the
        actual approval fact was simply missing from what it was given.
        """
        try:
            messages = [
                Message(
                    role="system",
                    content=(
                        "You answer questions using ONLY the facts given -- never add, infer, "
                        "or assume anything not explicitly stated. In particular, do not "
                        "conflate a fact that someone requested, referenced, or addressed "
                        "something TO a person with that person actually doing it -- a request "
                        "made to someone is not that person's action. If the facts don't state "
                        "an answer explicitly, say the information isn't available in the given "
                        "facts rather than guessing -- but say what's specifically missing, the "
                        "way you'd tell a colleague, not with a generic disclaimer. Never preface "
                        "the answer with throat-clearing like \"Based on the facts provided\" or "
                        "\"According to the given information\" -- start directly with the "
                        "answer itself. Respond with one clear, concise sentence."
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
        tenant_id: Optional[str] = None,
    ) -> ContextPacket:
        raw_facts = []
        for retriever in self.retrievers:
            raw_facts.extend(
                await retriever.retrieve(query, group_ids=group_ids, visible_uuids=visible_uuids, num_results=num_results)
            )

        # A resolved entity's own summary (see GraphRepository._resolve_named_entity)
        # is already one consolidated, holistic statement about it, generated at
        # ingestion time, so lead with that rather than the individual edges below it.
        entity_summary_lines = [
            line
            for f in raw_facts
            if f.get("kind") == "entity_summary"
            for line in f["fact"].splitlines()
            if line.strip()
        ]
        edge_facts = [f for f in raw_facts if f.get("kind") != "entity_summary"]

        if tenant_id and group_ids:
            self._apply_authority_tie_break(tenant_id, edge_facts)

        # A resolved entity's summary can restate facts that are also present as
        # individual edges verbatim (Graphiti doesn't guarantee the two are
        # distinct), so dedupe by exact text, keeping first occurrence, so the
        # same sentence never shows up twice.
        summary_lines = []
        seen = set()
        for line in entity_summary_lines + _build_answer_lines(edge_facts):
            if line not in seen:
                seen.add(line)
                summary_lines.append(line)
        if not summary_lines:
            summary_text = "No matching graph context found."
        elif len(summary_lines) == 1:
            # Already a single clear statement, so synthesizing would just
            # cost a call to say the same thing differently.
            summary_text = summary_lines[0]
        else:
            summary_text = await self._synthesize_answer(query, summary_lines)

        # Only the semantic-search fallback (see GraphRepository.search_graphiti_facts)
        # is ever capped by num_results: a resolved entity's own facts always
        # come back in full. Hitting the cap there is the signal a client uses
        # to offer "see more results" instead of always guessing at one.
        semantic_result_count = sum(1 for f in raw_facts if f.get("kind") == "semantic_search")
        result_limit_hit = semantic_result_count >= num_results

        # Which path actually answered this query: observability for the
        # cost story. "entity_resolution" means GraphRepository.search_graphiti_facts
        # resolved a named entity directly and never paid for Graphiti's hybrid
        # search at all (see that method's docstring); "semantic_search" means
        # it fell back to that paid call; "none" means nothing matched either
        # way. A query can only ever be one of these: GraphRepository returns
        # from whichever branch applies and never mixes facts from both.
        if not raw_facts:
            retrieval_path = "none"
        elif semantic_result_count > 0:
            retrieval_path = "semantic_search"
        else:
            retrieval_path = "entity_resolution"

        return ContextPacket(
            query=query,
            metadata={
                "group_ids": group_ids,
                "summary": summary_text,
                "facts": raw_facts,
                "result_limit_hit": result_limit_hit,
                "retrieval_path": retrieval_path,
            },
        )

    async def _synthesize_recommendation(self, query: str, anchor_name: str, chain_lines: list[str]) -> _CausalRecommendation:
        """The one place in this codebase an LLM call is deliberately allowed
        to infer, not just condense (everywhere else, see
        _synthesize_answer above, is explicitly fact-only). This is a
        separate, clearly-labeled mode for exactly that reason: a
        what-happened/why/impact/recommendation answer requires connecting
        facts across a causal chain, which is a different (and riskier)
        kind of output than restating what a single retrieval pass already
        found. Still grounded, told to reason only from the given facts,
        not invent new ones, but allowed to draw a conclusion from how
        they connect, which is the actual point of this mode."""
        messages = [
            Message(
                role="system",
                content=(
                    "You analyze a chain of related facts pulled from an enterprise "
                    "knowledge graph to explain a situation and recommend a next step. "
                    "Reason only from the facts given -- never invent a fact that isn't "
                    "there -- but you MAY infer likely cause, impact, and a recommendation "
                    "from how the given facts connect; that inference is the point of "
                    "this analysis. Keep each field to one or two concise sentences."
                ),
            ),
            Message(
                role="user",
                content=(
                    f"Question: {query}\nStarting point: {anchor_name}\n\n"
                    "Related facts, in causal-chain order:\n"
                    + "\n".join(f"- {line}" for line in chain_lines)
                ),
            ),
        ]
        result = await self.graphiti.llm_client.generate_response(
            messages, response_model=_CausalRecommendation, max_tokens=_RECOMMENDATION_MAX_TOKENS
        )
        return _CausalRecommendation.model_validate(result)

    async def _fact_only_causal_packet(
        self, query: str, group_ids: Optional[List[str]], facts: list[dict], retrieval_path: str
    ) -> ContextPacket:
        """Shared by both fact-only fallback shapes in get_causal_context_packet
        below (a single entity with no causal-typed edges of its own, and a
        two-entity connecting path that isn't entirely causal-typed). Uses
        the same fact-only synthesis the plain Ask path uses (_synthesize_answer),
        never a fabricated recommendation, never a :Decision. `facts` must be
        non-empty; can be a mix of valid and invalidated facts (see
        _build_answer_lines, which turns that mix into lines without
        silently dropping an invalidated fact that has no paired
        replacement in this batch). retrieval_path distinguishes which
        shape this is in telemetry."""
        # `or` fallback: _build_answer_lines can filter everything out (every
        # fact non-authoritative) even though `facts` itself is guaranteed
        # non-empty by this method's contract, so `lines` should never be
        # left empty.
        lines = _build_answer_lines(facts) or [f["fact"] for f in facts]
        summary = lines[0] if len(lines) == 1 else await self._synthesize_answer(query, lines)
        return ContextPacket(
            query=query,
            metadata={
                "group_ids": group_ids,
                "summary": summary,
                "facts": facts,
                "recommendation": None,
                "decision_id": None,
                "retrieval_path": retrieval_path,
            },
        )

    async def get_causal_context_packet(
        self,
        query: str,
        group_ids: Optional[List[str]] = None,
        visible_uuids: Optional[set[str]] = None,
        tenant_id: Optional[str] = None,
    ) -> ContextPacket:
        """The causal-reasoning mode: resolves query to a starting entity (or,
        for a "how is X connected to Y" question, two of them, see
        GraphRepository.causal_chain_for_query), assembles either the
        causal-typed chain out from one entity or the shortest path
        connecting two, and synthesizes a what-happened/why/impact/
        recommendation answer from it, but only when that assembled
        context is entirely causal-typed (GraphRepository.is_entirely_causal).
        When it isn't (a two-entity path made of ordinary relationships, or
        a single entity with no causal-typed edges of its own at all), this
        falls back to the exact same fact-only synthesis the plain Ask path
        uses, with the full evidence still attached: never a fabricated
        causal chain, never a recommendation, never a :Decision.

        Deliberately a separate method from get_context_packet, not a mode
        flag on it: the fact-only synthesis path above must never be
        loosened to also infer/recommend, and keeping this as its own
        method (with its own, separately-labeled ContextPacket.metadata
        field, "recommendation", never blended into "summary") is what
        guarantees that. See CLAUDE.md's pivot notes.

        Any recommendation produced is recorded as a :Decision graph node
        (see app/graph/decisions.py) when tenant_id is given. Best-effort:
        a failure to record doesn't fail the query itself, since the
        recommendation is still valid to hand back even if logging it
        failed.
        """
        anchor, second_entity, chain_facts = await self._repo.causal_chain_for_query(query, group_ids, visible_uuids)
        if anchor is None:
            if chain_facts:
                # No entity resolved by name, but causal_chain_for_query's
                # semantic-search fallback found something. Fact-only,
                # same as every other non-causal path: this isn't a
                # resolved anchor or a real causal walk, so it never gets a
                # recommendation or a :Decision, only sourced evidence and a
                # plain synthesized answer.
                return await self._fact_only_causal_packet(query, group_ids, chain_facts, "causal_semantic_fallback")
            return ContextPacket(
                query=query,
                metadata={
                    "group_ids": group_ids,
                    "summary": "No entity matching that name was found in this knowledge base.",
                    "facts": [],
                    "recommendation": None,
                    "decision_id": None,
                    "retrieval_path": "none",
                },
            )

        if not chain_facts:
            if second_entity is not None:
                # Mirrors search_graphiti_facts's own two-entity "not found"
                # phrasing: there's genuinely nothing to fall back to here
                # (a fact-only answer about one entity's own edges wouldn't
                # answer "how are these two connected" either).
                summary = f'No connection found between "{anchor["name"]}" and "{second_entity["name"]}" in this knowledge base.'
            else:
                # Single entity, no causal-typed chain: fall back to its
                # own directly-touching facts of any relationship type,
                # rather than just saying nothing was found. Deliberately
                # not "walk any relationship as if it were causal": routed
                # through _fact_only_causal_packet below, same as the
                # two-entity-but-not-causal case, so neither ever produces a
                # recommendation/Decision. The relevance nuance
                # (_synthesize_answer answers the actual question, so an
                # irrelevant fact never makes it into a multi-fact summary)
                # comes for free from that shared helper. Deliberately not
                # filtered to is_valid here: _fact_only_causal_packet (via
                # _build_answer_lines) handles a mix of valid/invalidated
                # facts correctly now; pre-filtering here would silently
                # throw away real historical facts before they ever got that
                # chance, the exact bug this was fixed for.
                direct_facts = self._repo.direct_facts_for(anchor["uuid"], visible_uuids)
                if direct_facts:
                    return await self._fact_only_causal_packet(query, group_ids, direct_facts, "causal_fallback_direct_facts")
                summary = f'No causal chain, or any other recorded fact, connecting "{anchor["name"]}" to anything else in this knowledge base.'
            return ContextPacket(
                query=query,
                metadata={
                    "group_ids": group_ids,
                    "summary": summary,
                    "facts": [],
                    "recommendation": None,
                    "decision_id": None,
                    "retrieval_path": "causal_chain_empty",
                },
            )

        # A single-anchor causal walk that found only one causal-typed edge
        # is too thin to trust as "the" explanation on its own when the
        # anchor genuinely has other, non-causal-typed facts a plain Ask
        # would surface. Committing straight to a recommendation here
        # means that one fact silently outranks everything else known about
        # the entity, with no way for the more relevant context to ever be
        # seen. Found live: "Who owns the Brightpeak Automation account
        # right now, and does that affect whether it's at risk?" walked to
        # exactly one causal-typed fact (a PRODUCES edge to its order), and
        # the real ownership-handoff facts (MANAGED_BY-shaped, not causal)
        # never entered the recommendation at all, even though the plain
        # Ask path resolves the same entity and shows all of it correctly.
        # Only overrides when direct_facts_for turns up something the thin
        # chain doesn't already have; when the anchor truly has nothing
        # else (the common, well-tested case), this is a no-op and the
        # normal single-fact recommendation below is exactly right.
        if second_entity is None and len(chain_facts) <= 1:
            direct_facts = self._repo.direct_facts_for(anchor["uuid"], visible_uuids)
            chain_fact_texts = {f["fact"] for f in chain_facts}
            extra_facts = [f for f in direct_facts if f["fact"] not in chain_fact_texts]
            if extra_facts:
                return await self._fact_only_causal_packet(
                    query, group_ids, chain_facts + extra_facts, "causal_fallback_direct_facts"
                )

        if not self._repo.is_entirely_causal(chain_facts):
            # A two-entity connecting path isn't restricted to causal-typed
            # edges the way the single-anchor walk is (see
            # causal_chain_for_query): "how is X connected to Y" needs the
            # real shortest path, whatever it's made of. When that path
            # isn't entirely causal, treating it as one would mean inferring
            # a "why" from, say, a shared location, which isn't a real
            # causal story, so this returns a fact-only, fully-sourced
            # answer instead.
            return await self._fact_only_causal_packet(query, group_ids, chain_facts, "causal_path_between_entities")

        # _build_answer_lines (not a raw is_valid filter), so a real but
        # superseded fact, e.g. the root-cause defect edge that later got
        # invalidated once the lot was quarantined, still reaches the
        # recommendation synthesis below instead of silently vanishing the
        # same way it used to vanish from the plain Ask path before that fix.
        # Found live: the actual root cause of an at-risk order was a
        # solder-joint-defect fact whose invalid_at made it fail the old
        # `is_valid` filter, so the recommendation's "why" never mentioned it
        # even though it was sitting right there in the causal chain.
        chain_lines = _build_answer_lines(chain_facts) or [f["fact"] for f in chain_facts]
        recommendation = None
        decision_id = None
        try:
            rec = await self._synthesize_recommendation(query, anchor["name"], chain_lines)
            recommendation = {
                "what_happened": rec.what_happened,
                "why": rec.why,
                "impact": rec.impact,
                "recommendation": rec.recommendation,
            }
        except Exception as e:
            logger.warning(f"Causal recommendation synthesis failed, returning the chain without one: {e}")

        if recommendation and tenant_id and group_ids:
            try:
                decision_id = record_decision(
                    self._repo,
                    group_id=group_ids[0],
                    anchor_uuid=anchor["uuid"],
                    query=query,
                    recommendation_text=(
                        f"What happened: {recommendation['what_happened']}\n"
                        f"Why: {recommendation['why']}\nImpact: {recommendation['impact']}\n"
                        f"Recommendation: {recommendation['recommendation']}"
                    ),
                    rationale="; ".join(chain_lines[:5]),
                )
            except Exception as e:
                logger.warning(f"Failed to record Decision entity for causal recommendation: {e}")

        return ContextPacket(
            query=query,
            metadata={
                "group_ids": group_ids,
                "summary": "\n".join(chain_lines),
                "facts": chain_facts,
                "recommendation": recommendation,
                "decision_id": decision_id,
                "retrieval_path": "causal_chain",
            },
        )
