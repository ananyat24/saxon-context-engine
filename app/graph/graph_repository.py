# Sits between the rest of the app and the two ways this project talks to the graph:
# raw Cypher queries (Neo4j's query language, similar in spirit to SQL) via Neo4jClient,
# and Graphiti's own higher-level search API, which understands time ("what was true
# on this date") on top of the same underlying graph.
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from graphiti_core import Graphiti
from app.graph.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)


def _not_yet_invalidated(invalid_at) -> bool:
    """True if `invalid_at` is None or still in the future.

    invalid_at isn't only set when Graphiti detects a real contradiction
    (always a past timestamp by construction) -- extraction can also set it
    directly from a future business date in the source text (e.g. a CRM row's
    "renewal date" column, read as this edge's own validity bound). Treating
    any invalid_at as already-invalid conflated those two cases and made an
    account's still-current facts vanish from results the moment their
    renewal date was extracted, well before that date actually arrived.
    Falls back to "invalidated" (the original, conservative behavior) if the
    value can't be parsed, rather than risk surfacing a genuinely-superseded
    fact as current.
    """
    if invalid_at is None:
        return True
    if isinstance(invalid_at, str):
        try:
            invalid_at = datetime.fromisoformat(invalid_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(invalid_at, datetime):
        return False
    if invalid_at.tzinfo is None:
        invalid_at = invalid_at.replace(tzinfo=timezone.utc)
    return invalid_at > datetime.now(timezone.utc)

# Graphiti's search ranks edges by RRF-fused vector/text similarity with no
# relevance threshold -- it always returns its top-N, even when nothing in the
# graph is actually related to the query. That's fine for open-ended questions,
# but for a query naming a specific entity (e.g. "What's changed about Rhodes
# Furniture?") it means an entity with few or no edges of its own gets padded
# out with other entities' unrelated facts that just happen to read similarly.
# This regex pulls out multi-word capitalized phrases so a named entity can be
# resolved directly against the graph and its own edges/summary used instead.
#
# Tolerates a single lowercase connector ("and"/"of"/"&") between capitalized
# words, so a real company name like "Fenwick & Cole Legal" or "Bank of
# America" extracts as one whole candidate instead of stopping at the first
# connector and only ever matching a truncated fragment ("Cole Legal") --
# which, notably, defeats the exact-match reconciliation in
# _match_entities_by_name below, since a truncated fragment only ever
# CONTAINS-matches (deliberately not reconciled across connectors) rather
# than exactly matching the entity's real, full name.
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][\w'.-]*(?:\s+(?:(?:and|of|&)\s+)?[A-Z][\w'.-]*)+\b")

# Records ingested without a human-readable name (see FileSourceSpec.name_column
# in app/ingestion/file_source.py) end up named "<Type> <id>", e.g. "Order
# 10248" -- a single capitalized word plus a numeric id, which _PROPER_NOUN_RE
# above never matches (it requires two consecutive capitalized words). Without
# this, "order 10248" falls through to plain semantic search, which has no
# relevance threshold and pads the answer out with facts about several other,
# unrelated orders that just happen to score similarly. This is deliberately
# looser (case-insensitive, no second-word capitalization requirement) so it
# also catches how people actually type these queries -- lowercase, "#" before
# the number, etc. Precisely because it's loose, an unresolved match here must
# NOT be treated the way an unresolved proper noun is (see _resolve_named_entities)
# -- "since 2023" or "in 2024" would also match this pattern and aren't meant to
# short-circuit an otherwise normal query into a false "not found".
_ID_PHRASE_RE = re.compile(r"\b[A-Za-z][\w'.-]*\s+#?[\w-]*\d[\w-]*\b")

# Trailing legal-entity words that don't change *which* real-world company a
# name refers to -- "Fenwick & Cole Legal" and "Fenwick & Cole Legal, Inc."
# name the same entity, but the exact-match reconciliation in
# _match_entities_by_name below would treat them as two unrelated ones.
# Deliberately still an equality check after stripping these (not a fuzzy/
# edit-distance match) -- see that method's docstring for why a looser match
# risks merging two genuinely different entities.
_LEGAL_SUFFIX_WORDS = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation",
    "co", "company", "plc", "gmbh", "llp", "lp",
}
_NAME_PUNCT_RE = re.compile(r"[.,]")
_NAME_WS_RE = re.compile(r"\s+")

# Ordinary English filler words that show up in almost every question and
# would otherwise become spurious single-word candidates below -- excluding
# them keeps that fallback aimed at words that might actually be someone's
# name, not "what"/"the"/"about".
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for",
    "with", "about", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "what", "who", "when", "where", "why", "how",
    "which", "this", "that", "these", "those", "we", "you", "i", "it",
    "he", "she", "they", "them", "us", "our", "your", "his", "her",
    "its", "their", "know", "tell", "me", "recently", "changed",
    "status", "affected", "relevant", "connected", "between", "going",
    "on", "up", "not", "any", "all", "can", "will",
})
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")


def _extract_lowercase_word_candidates(query_text: str) -> list[str]:
    """Lenient, single-word fallback for a query that names someone/something
    casually, without capitalizing it -- "what do we know about diego"
    instead of "...Diego Alvarez?". _PROPER_NOUN_RE above requires
    capitalized, multi-word phrases, so a plain lowercase name never becomes
    a candidate at all, and the query falls straight through to Graphiti's
    own unconstrained semantic search -- which, per its own documented lack
    of a relevance threshold, pads the answer out with unrelated facts that
    merely score similarly. Found for real: a lowercase "diego" query
    returned Diego Alvarez's own facts mixed in with several unrelated
    orders/shipments/quality events, while the identical question typed as
    "Diego Alvarez" resolved precisely.

    Every word here still goes through the exact same resolution pipeline
    proper nouns use (_match_entities_by_name's exact/normalized/CONTAINS
    chain), so it only ever resolves to a name that's actually in the graph
    -- this doesn't loosen matching, only which words get a chance to try
    it. And, like the existing id-phrase candidates, a word here can never
    trigger the hard "not found" short-circuit on its own (see
    _resolve_named_entities: only a candidate in proper_noun_set can set
    saw_unresolved) -- most words in an ordinary sentence obviously won't
    name anything, and that must fall through to normal search, not a false
    "not found".
    """
    words = {w.lower() for w in _WORD_RE.findall(query_text)}
    return sorted(words - _STOPWORDS, key=len, reverse=True)


def _normalize_entity_name(name: str) -> str:
    """A name equality check that isn't defeated by a legal suffix, "&" vs.
    "and", punctuation, or extra whitespace -- see _LEGAL_SUFFIX_WORDS above.
    Two names that normalize the same are treated as the same real-world
    entity; two that don't are left alone."""
    normalized = name.lower().replace("&", " and ")
    normalized = _NAME_PUNCT_RE.sub("", normalized)
    words = _NAME_WS_RE.sub(" ", normalized).strip().split(" ")
    while words and words[-1] in _LEGAL_SUFFIX_WORDS:
        words.pop()
    return " ".join(words)


def _extract_candidate_entities(query_text: str) -> list[str]:
    candidates = set(_PROPER_NOUN_RE.findall(query_text))
    return sorted(candidates, key=len, reverse=True)


def _extract_id_candidates(query_text: str) -> list[str]:
    phrases = set(_ID_PHRASE_RE.findall(query_text))
    # Extraction sometimes drops the type-word prefix for one record but not
    # its siblings -- e.g. four Northwind orders end up named "Order 10250"
    # etc., but one ends up named just "10248", an inconsistency in how the
    # ingesting LLM happened to name that one record, not something a query
    # can know about. Without this, "what's the status of order 10248"
    # produces a candidate ("order 10248") that CONTAINS-matches nothing --
    # too long to be found inside the shorter actual name -- so resolution
    # silently fails and the query falls through to padded semantic search
    # instead of the one order actually asked about. Adding just the bare
    # trailing token (the id itself) as its own candidate covers that case
    # too, without loosening the match logic itself to accept shorter
    # substrings generally.
    bare_ids = {phrase.rsplit(" ", 1)[-1] for phrase in phrases if " " in phrase}
    return sorted(phrases | bare_ids, key=len, reverse=True)


class GraphRepository:
    """Repository encapsulating Neo4j Cypher operations and Graphiti graph queries."""

    def __init__(
        self,
        graphiti_instance: Optional[Graphiti] = None,
        neo4j_client: Optional[Neo4jClient] = None,
    ):
        self.graphiti = graphiti_instance
        # If a caller hands us a Neo4jClient, reuse its connection pool across every
        # execute_cypher() call. If not, we fall back to opening a short-lived client
        # per call (see execute_cypher) -- less efficient, but keeps this class usable
        # with zero setup for one-off scripts and tests.
        self._owned_client = neo4j_client

    def execute_cypher(self, query: str, parameters: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """Run a Cypher query against Neo4j and return each result row as a dict."""
        client = self._owned_client or Neo4jClient()
        try:
            with client.driver.session() as session:
                result = session.run(query, parameters or {})
                return [record.data() for record in result]
        finally:
            # Only close a client we created ourselves for this call -- closing one
            # the caller gave us would break their ability to reuse it afterward.
            if client is not self._owned_client:
                client.close()

    def _match_entities_by_name(self, name: str, group_ids: list[str]) -> list[dict[str, Any]]:
        """Matches `name` against real node names across every connector in
        group_ids -- a multi-connector document set (see
        app/graph/document_sets.py) needs entity resolution to work across
        all of them, not just the first.

        Returns *every* exact (case-insensitive) name match, not just one --
        this is the deterministic reconciliation step that lets "Fenwick &
        Cole Legal" mentioned in a CRM record and, separately, in an email
        resolve to the same real-world entity at query time, instead of only
        whichever one of its several per-connector nodes happened to be
        picked first (see _resolve_named_entities/search_graphiti_facts,
        which pool every returned row's own facts together as one entity).

        Deliberately exact-match only for this multi-row reconciliation:
        falling back to the looser CONTAINS match here too would risk
        merging two genuinely different entities that just share a word,
        undoing the padded-results fix search_graphiti_facts already has. A
        query naming a *partial* name isn't claiming "these are the same
        entity", so when nothing matches exactly, this falls back to the
        single best CONTAINS match instead (existing loose-match behavior,
        still capped at one).

        This is deliberately simple, deterministic reconciliation --
        normalized name equality (see _normalize_entity_name: strips a
        trailing legal suffix like "Inc"/"LLC", "&" vs. "and", and
        punctuation/whitespace differences) -- rather than matching on a
        real shared key (an email address, an external system id, ...)
        across sources; see CLAUDE.md's v2.5. The known tradeoff: two
        different real-world entities that happen to normalize to the same
        name would incorrectly merge. Worth revisiting once a stronger
        signal exists in the data.
        """
        # NOT n:Decision throughout this method (and every other general
        # entity/fact query in this class) -- a :Decision node is an
        # internal audit record of a past generated recommendation (see
        # app/graph/decisions.py), not a business entity a person would ever
        # be asking about. It's labeled :Entity too (the ontology models
        # Decision as extending Event, which extends Entity), so without
        # this exclusion it's indistinguishable from real data to every
        # query in this class -- found for real in production: a Decision
        # node's own auto-generated name ("Recommendation for: <query>") got
        # sampled as a suggested-question topic, and its INVOLVES edge's
        # boilerplate fact text ("Saxon generated this recommendation while
        # analyzing: <query>") got returned as if it were a real fact about
        # the entity the Decision was about.
        exact_rows = self.execute_cypher(
            "MATCH (n:Entity) WHERE n.group_id IN $group_ids AND toLower(n.name) = toLower($name) AND NOT n:Decision "
            "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary, n.group_id AS group_id",
            {"group_ids": group_ids, "name": name},
        )

        normalized_target = _normalize_entity_name(name)
        core_token = normalized_target.split(" ")[0] if normalized_target else ""
        normalized_rows: list[dict[str, Any]] = []
        # A short core token (e.g. "of", or nothing left after stripping a
        # suffix) would turn this into an unbounded CONTAINS scan for little
        # benefit -- skip it in that case.
        if len(core_token) >= 3:
            candidate_rows = self.execute_cypher(
                "MATCH (n:Entity) WHERE n.group_id IN $group_ids AND toLower(n.name) CONTAINS $core_token AND NOT n:Decision "
                "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary, n.group_id AS group_id",
                {"group_ids": group_ids, "core_token": core_token},
            )
            normalized_rows = [r for r in candidate_rows if _normalize_entity_name(r["name"]) == normalized_target]

        # Run both, not just normalized-as-fallback-after-exact-fails: an
        # exact match in one connector's data (e.g. "Fenwick and Cole
        # Legal") and a legal-suffix variant in another's ("Fenwick & Cole
        # Legal, Inc.") both need to end up in this reconciliation set, not
        # just whichever one the exact query alone happened to find.
        if exact_rows or normalized_rows:
            by_uuid = {r["uuid"]: r for r in exact_rows}
            for r in normalized_rows:
                by_uuid.setdefault(r["uuid"], r)
            return list(by_uuid.values())

        return self.execute_cypher(
            "MATCH (n:Entity) WHERE n.group_id IN $group_ids AND toLower(n.name) CONTAINS toLower($name) AND NOT n:Decision "
            "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary, n.group_id AS group_id LIMIT 1",
            {"group_ids": group_ids, "name": name},
        )

    async def _resolve_named_entities(
        self, query_text: str, group_ids: list[str], visible_uuids: Optional[set[str]]
    ) -> tuple[list[list[dict[str, Any]]], bool]:
        """Looks for specifically-named entities in the query and matches each
        against real node names in this knowledge base, so a query like "What's
        changed about Rhodes Furniture?" or "What's the status of order 10248?"
        can be grounded to that exact node instead of left to semantic search
        to guess at.

        Two candidate sources feed this: proper nouns (_extract_candidate_entities,
        e.g. "Rhodes Furniture") and id-style phrases (_extract_id_candidates,
        e.g. "order 10248"). Only a proper noun that fails to resolve counts as
        saw_unresolved_candidate -- an id-style phrase is loose enough to also
        match ordinary text like "since 2023", so an unresolved one there just
        falls through to normal search instead of forcing a "not found".

        Every candidate's lookup is independent, so they run concurrently (each
        off the event loop via to_thread, since execute_cypher is a blocking
        call) instead of one after another -- a query naming two entities (e.g.
        "How is X connected to Y?") no longer pays for two round trips back to
        back.

        Returns (resolved_groups, saw_unresolved_candidate). resolved_groups is
        a list of "same real-world entity" row-groups, one per distinct matched
        candidate (deduped by exact uuid-set, so an overlapping shorter/longer
        candidate matching the same node(s) doesn't produce a second group) --
        each group is one or more rows sharing that name across different
        connectors (see _match_entities_by_name), meant to be pooled together
        by the caller, not treated as separate entities. saw_unresolved_candidate
        is True if some proper-noun-shaped phrase in the query didn't match
        anything visible, which the caller uses to say "not found" rather than
        silently falling back to an ungrounded search.
        """
        proper_nouns = _extract_candidate_entities(query_text)
        all_candidates = proper_nouns + _extract_id_candidates(query_text)
        # Only when the query has no capitalized-phrase candidate at all --
        # a properly-capitalized "Diego Alvarez" (or a two-entity "X and Y")
        # already resolves precisely via proper_nouns above and shouldn't
        # pay for this broader, per-word scan too. See
        # _extract_lowercase_word_candidates's docstring for the bug this
        # covers (a casually-typed, uncapitalized name).
        if not proper_nouns:
            all_candidates += _extract_lowercase_word_candidates(query_text)
        if not all_candidates:
            return [], False

        rows_per_candidate = await asyncio.gather(
            *(asyncio.to_thread(self._match_entities_by_name, c, group_ids) for c in all_candidates)
        )

        groups: list[list[dict[str, Any]]] = []
        seen_uuid_sets: set[frozenset] = set()
        saw_unresolved = False
        proper_noun_set = set(proper_nouns)
        for candidate, rows in zip(all_candidates, rows_per_candidate):
            visible_rows = [r for r in rows if visible_uuids is None or r["uuid"] in visible_uuids]
            if visible_rows:
                key = frozenset(r["uuid"] for r in visible_rows)
                if key not in seen_uuid_sets:
                    seen_uuid_sets.add(key)
                    groups.append(visible_rows)
            elif candidate in proper_noun_set:
                # Either nothing matched, or it matched something this caller
                # can't see -- don't leak existence either way. An id-phrase
                # candidate that misses is expected (see docstring) and never
                # counts here.
                saw_unresolved = True

        return groups, saw_unresolved

    @staticmethod
    def fact_is_valid(expired_at, invalid_at) -> bool:
        """Public wrapper around _not_yet_invalidated for callers outside
        this module (e.g. app/api/odata.py) that need the same
        current-vs-superseded rule this class applies everywhere else,
        without reaching into a name-mangled internal."""
        return expired_at is None and _not_yet_invalidated(invalid_at)

    def _resolve_episode_sources(self, episode_uuid_lists: list[list[str]]) -> list[list[str]]:
        """Batch-resolves Graphiti's own `episodes` property (a list of
        Episodic-node uuids Graphiti already stores on every RELATES_TO edge,
        naming exactly which ingested episode(s) produced or last touched
        that fact) to the human-readable `source_description` recorded on
        each Episodic node -- e.g. "orders.csv (Order)" or a web connector's
        URL (see app/ingestion/file_source.py's SourceRecord and
        connector_sync.py, which set it per-record at ingest time).

        This is what lets a fact say WHERE it actually came from -- a
        specific document/row/page -- instead of just the group_id-level
        "which knowledge base" the UI already showed. One query for every
        distinct uuid across the whole batch, not one per fact.
        """
        all_uuids = {u for lst in episode_uuid_lists for u in (lst or [])}
        if not all_uuids:
            return [[] for _ in episode_uuid_lists]
        rows = self.execute_cypher(
            "MATCH (e:Episodic) WHERE e.uuid IN $uuids "
            "RETURN e.uuid AS uuid, e.source_description AS source_description",
            {"uuids": list(all_uuids)},
        )
        by_uuid = {row["uuid"]: row["source_description"] for row in rows}
        return [
            sorted({by_uuid[u] for u in (lst or []) if by_uuid.get(u)})
            for lst in episode_uuid_lists
        ]

    @staticmethod
    def _to_native(value):
        """execute_cypher() returns raw neo4j.time.DateTime objects (unlike
        Graphiti's own search results, which come back as plain datetimes) --
        FastAPI can't serialize those, so anything read via execute_cypher and
        handed back as a fact needs this before it can go in an API response."""
        to_native = getattr(value, "to_native", None)
        return to_native() if callable(to_native) else value

    def _entity_own_facts(self, uuid: str, visible_uuids: Optional[set[str]]) -> list[dict[str, Any]]:
        """Pulls every edge directly touching a resolved entity straight from
        Neo4j -- precise by construction, unlike semantic search, since it can
        only ever return facts that are actually about this entity.

        Excludes an edge to/from a :Decision node -- that's an internal
        audit record of a past generated recommendation (see
        app/graph/decisions.py), not a real fact about this entity, and its
        boilerplate INVOLVES-edge text ("Saxon generated this recommendation
        while analyzing: <query>") isn't something a person asking about
        this entity should ever see mixed in with its actual facts. See
        _match_entities_by_name's docstring for the same exclusion and why.
        """
        rows = self.execute_cypher(
            "MATCH (n:Entity {uuid: $uuid})-[r:RELATES_TO]-(m) WHERE NOT m:Decision "
            "RETURN r.fact AS fact, r.name AS relationship_type, r.valid_at AS valid_at, "
            "r.invalid_at AS invalid_at, r.expired_at AS expired_at, r.group_id AS group_id, "
            "r.episodes AS episodes, "
            "startNode(r).uuid AS source_node_uuid, endNode(r).uuid AS target_node_uuid",
            {"uuid": uuid},
        )
        kept_rows = [
            row for row in rows
            if visible_uuids is None
            or row["source_node_uuid"] in visible_uuids
            or row["target_node_uuid"] in visible_uuids
        ]
        sources_by_row = self._resolve_episode_sources([row["episodes"] for row in kept_rows])
        facts = []
        for row, sources in zip(kept_rows, sources_by_row):
            expired_at = self._to_native(row["expired_at"])
            invalid_at = self._to_native(row["invalid_at"])
            facts.append({
                "fact": row["fact"],
                "relationship_type": row["relationship_type"],
                "source_node_uuid": row["source_node_uuid"],
                "target_node_uuid": row["target_node_uuid"],
                "valid_at": self._to_native(row["valid_at"]),
                "invalid_at": invalid_at,
                "expired_at": expired_at,
                # Which connector/knowledge base this fact actually came
                # from -- lets the UI show "from X" per fact rather than
                # just the flat text (see frontend/app.js's renderFacts).
                # Real provenance, not a guess: this is the same group_id
                # every other part of this app already scopes queries by.
                "group_id": row["group_id"],
                # The actual document/row this fact was extracted from --
                # e.g. "orders.csv (Order)" -- resolved from Graphiti's own
                # edge.episodes property, not a guess (see
                # _resolve_episode_sources above).
                "sources": sources,
                "is_valid": expired_at is None and _not_yet_invalidated(invalid_at),
            })
        return facts

    def _relationship_path_facts(
        self, uuid_a: str, uuid_b: str, visible_uuids: Optional[set[str]]
    ) -> Optional[list[dict[str, Any]]]:
        """Finds the shortest chain of facts connecting two resolved entities, so
        "How is X connected to Y?" answers the actual question asked instead of
        just describing one of the two entities. Bounded to 4 hops -- beyond that
        a "connection" is really just everything-connects-to-everything noise,
        not a meaningful answer. Returns None if no such path exists (or none of
        its nodes are visible to this caller). Returns full fact dicts (with
        group_id and sources, same shape as _entity_own_facts) rather than bare
        strings, so this path's evidence is traceable to its actual source
        document(s) too -- not just the two-entity causal path
        (_relationship_path_full_facts) below."""
        rows = self.execute_cypher(
            "MATCH p = shortestPath((a:Entity {uuid: $uuid_a})-[:RELATES_TO*1..4]-(b:Entity {uuid: $uuid_b})) "
            "WHERE NONE(node IN nodes(p) WHERE node:Decision) "
            "RETURN [rel IN relationships(p) | rel.fact] AS facts, "
            "[rel IN relationships(p) | rel.name] AS relationship_types, "
            "[rel IN relationships(p) | rel.group_id] AS rel_group_ids, "
            "[rel IN relationships(p) | rel.episodes] AS rel_episodes, "
            "[rel IN relationships(p) | rel.valid_at] AS valid_ats, "
            "[rel IN relationships(p) | rel.invalid_at] AS invalid_ats, "
            "[rel IN relationships(p) | rel.expired_at] AS expired_ats, "
            "[n IN nodes(p) | n.uuid] AS path_uuids",
            {"uuid_a": uuid_a, "uuid_b": uuid_b},
        )
        if not rows or not rows[0]["facts"]:
            return None
        row = rows[0]
        if visible_uuids is not None and not all(u in visible_uuids for u in row["path_uuids"]):
            return None
        sources_by_hop = self._resolve_episode_sources(row["rel_episodes"])
        facts = []
        for i, fact in enumerate(row["facts"]):
            expired_at = self._to_native(row["expired_ats"][i])
            invalid_at = self._to_native(row["invalid_ats"][i])
            facts.append({
                "fact": fact,
                "relationship_type": row["relationship_types"][i],
                "source_node_uuid": "",
                "target_node_uuid": "",
                "valid_at": self._to_native(row["valid_ats"][i]),
                "invalid_at": invalid_at,
                "expired_at": expired_at,
                "group_id": row["rel_group_ids"][i],
                "sources": sources_by_hop[i],
                "is_valid": expired_at is None and _not_yet_invalidated(invalid_at),
            })
        return facts

    def _relationship_path_full_facts(
        self, uuid_a: str, uuid_b: str, group_ids: list[str], visible_uuids: Optional[set[str]]
    ) -> Optional[list[dict[str, Any]]]:
        """Like _relationship_path_facts, but returns full fact dicts (hop
        order, relationship_type, temporal validity) instead of bare fact
        strings -- built for the causal-reasoning "how is X connected to Y"
        case (see causal_chain_for_query below), which needs
        relationship_type to decide whether the whole connecting path is
        causal-typed (is_entirely_causal, a real inference-worthy chain) or
        not (a plain topological connection, reported as fact-only,
        fully-sourced evidence instead -- see
        ContextOrchestrator.get_causal_context_packet), and needs is_valid
        to badge each fact current/superseded in that evidence list.
        Scoped to group_ids the same way _causal_chain_facts_from is scoped
        (see that method's docstring for why -- the same reasoning applies
        here unchanged). Returns None if no path exists, or a node along it
        isn't in scope/visible to this caller.
        """
        rows = self.execute_cypher(
            f"""
            MATCH p = shortestPath((a:Entity {{uuid: $uuid_a}})-[:RELATES_TO*1..{self._CAUSAL_MAX_HOPS}]-(b:Entity {{uuid: $uuid_b}}))
            WHERE ALL(node IN nodes(p) WHERE node.group_id IN $group_ids)
              AND NONE(node IN nodes(p) WHERE node:Decision)
            WITH relationships(p) AS rels, [n IN nodes(p) | n.uuid] AS path_uuids
            UNWIND range(0, size(rels) - 1) AS hop
            WITH rels[hop] AS rel, hop, path_uuids
            RETURN rel.fact AS fact, rel.name AS relationship_type, hop,
                   rel.valid_at AS valid_at, rel.invalid_at AS invalid_at, rel.expired_at AS expired_at,
                   rel.group_id AS group_id, rel.episodes AS episodes, startNode(rel).uuid AS source_node_uuid,
                   endNode(rel).uuid AS target_node_uuid, path_uuids
            ORDER BY hop
            """,
            {"uuid_a": uuid_a, "uuid_b": uuid_b, "group_ids": group_ids},
        )
        if not rows:
            return None
        if visible_uuids is not None and not all(u in visible_uuids for u in rows[0]["path_uuids"]):
            return None
        sources_by_row = self._resolve_episode_sources([row["episodes"] for row in rows])
        facts: list[dict[str, Any]] = []
        for row, sources in zip(rows, sources_by_row):
            expired_at = self._to_native(row["expired_at"])
            invalid_at = self._to_native(row["invalid_at"])
            facts.append({
                "fact": row["fact"],
                "relationship_type": row["relationship_type"],
                "hop": row["hop"],
                "source_node_uuid": row["source_node_uuid"],
                "target_node_uuid": row["target_node_uuid"],
                "valid_at": self._to_native(row["valid_at"]),
                "invalid_at": invalid_at,
                "expired_at": expired_at,
                "group_id": row["group_id"],
                "sources": sources,
                "is_valid": expired_at is None and _not_yet_invalidated(invalid_at),
            })
        return facts

    # The ontology's own causal relationship types (see ontology/core.yaml) --
    # the causal-chain retriever below only ever walks edges typed one of
    # these, so it can't wander off into an unrelated part of the graph just
    # because a path happens to exist. Deliberately not "any relationship":
    # a generic BELONGS_TO/LOCATED_AT hop doesn't explain why something
    # happened, only that two things are related.
    _CAUSAL_RELATIONSHIP_TYPES = ["DEPENDS_ON", "CAUSED_BY", "AFFECTS", "RESULTED_IN", "SOURCED_FROM"]
    # "A few hops," not an open-ended traversal -- matches the shape of the
    # spec's own example (Order -> Product -> Component -> Supplier ->
    # QualityEvent is 4 hops) without risking a combinatorial blow-up on a
    # densely-connected graph.
    _CAUSAL_MAX_HOPS = 4
    # Keeps the chain small enough to hand an LLM as grounding for a
    # recommendation, not a graph dump -- this is a "few facts explaining a
    # situation" feature, not a bulk export.
    _CAUSAL_FACT_LIMIT = 25

    @classmethod
    def is_entirely_causal(cls, facts: list[dict[str, Any]]) -> bool:
        """True only if every fact's relationship_type is one of the
        ontology's causal types. _causal_chain_facts_from's single-anchor
        walk is already restricted to causal types by construction, so this
        is trivially true for it -- but _relationship_path_full_facts's
        two-entity connecting path is NOT type-restricted (a "how is X
        connected to Y" question needs the real shortest path, whatever
        types it's made of, the same way search_graphiti_facts's own
        two-entity branch already works), so that path can easily mix
        causal and non-causal hops. ContextOrchestrator uses this to decide
        whether a connecting path is a genuine causal chain worth inferring
        a recommendation from, or just a topological connection that should
        be reported as plain, fully-sourced facts instead -- never the
        reverse (loosening what counts as "causal enough" to infer from)."""
        return bool(facts) and all(f.get("relationship_type") in cls._CAUSAL_RELATIONSHIP_TYPES for f in facts)

    def _causal_chain_facts_from(
        self, uuid: str, group_ids: list[str], visible_uuids: Optional[set[str]]
    ) -> list[dict[str, Any]]:
        """Walks outward from an already-resolved entity over only the
        ontology's causal relationship types, instead of the entity's own
        directly-touching edges (see _entity_own_facts) -- built for
        "what happened -> why -> impact" questions that need to follow a
        chain, e.g. an at-risk Order to its Product to a Component to the
        Supplier to an open QualityEvent to a supporting document.

        Every node along the path is constrained to group_ids, the same way
        every other relationship traversal in this codebase scopes both
        ends of an edge (see app/graph/authorization.py, app/api/odata.py's
        list_facts_odata) -- Graphiti's own group_id partitioning means a
        RELATES_TO edge shouldn't naturally span two knowledge bases in
        practice, but this is the one multi-hop (up to 4 edges) traversal
        in the codebase, and relying on that as an unenforced assumption
        rather than checking it directly is exactly the kind of gap that
        turns a future data-quality bug or entity-merge feature into a
        silent cross-tenant leak -- worse here, since a causal answer also
        gets written into a permanent, auditable :Decision node.

        Returns facts in path order (shortest paths first, then hop order
        within a path), deduped by (source, relationship type, target) so a
        fact reachable via more than one path is only listed once.
        """
        rows = self.execute_cypher(
            f"""
            MATCH p = (n:Entity {{uuid: $uuid}})-[:RELATES_TO*1..{self._CAUSAL_MAX_HOPS}]-(m:Entity)
            WHERE ALL(rel IN relationships(p) WHERE rel.name IN $causal_types)
              AND ALL(node IN nodes(p) WHERE node.group_id IN $group_ids)
              AND NONE(node IN nodes(p) WHERE node:Decision)
            WITH p, relationships(p) AS rels, nodes(p) AS path_nodes, length(p) AS path_length
            UNWIND range(0, size(rels) - 1) AS hop
            WITH rels[hop] AS rel, hop, path_length, path_nodes
            RETURN DISTINCT rel.fact AS fact, rel.name AS relationship_type, hop, path_length,
                   rel.valid_at AS valid_at, rel.invalid_at AS invalid_at, rel.expired_at AS expired_at,
                   rel.group_id AS group_id, rel.episodes AS episodes, startNode(rel).uuid AS source_node_uuid,
                   endNode(rel).uuid AS target_node_uuid, [x IN path_nodes | x.uuid] AS path_uuids
            ORDER BY path_length, hop
            LIMIT {self._CAUSAL_FACT_LIMIT}
            """,
            {"uuid": uuid, "group_ids": group_ids, "causal_types": self._CAUSAL_RELATIONSHIP_TYPES},
        )
        kept_rows = []
        seen: set[tuple] = set()
        for row in rows:
            if visible_uuids is not None and not all(u in visible_uuids for u in row["path_uuids"]):
                continue
            key = (row["source_node_uuid"], row["relationship_type"], row["target_node_uuid"])
            if key in seen:
                continue
            seen.add(key)
            kept_rows.append(row)
        sources_by_row = self._resolve_episode_sources([row["episodes"] for row in kept_rows])
        facts: list[dict[str, Any]] = []
        for row, sources in zip(kept_rows, sources_by_row):
            expired_at = self._to_native(row["expired_at"])
            invalid_at = self._to_native(row["invalid_at"])
            facts.append({
                "fact": row["fact"],
                "relationship_type": row["relationship_type"],
                "hop": row["hop"],
                "source_node_uuid": row["source_node_uuid"],
                "target_node_uuid": row["target_node_uuid"],
                "valid_at": self._to_native(row["valid_at"]),
                "invalid_at": invalid_at,
                "expired_at": expired_at,
                "group_id": row["group_id"],
                "sources": sources,
                "is_valid": expired_at is None and _not_yet_invalidated(invalid_at),
            })
        return facts

    def direct_facts_for(self, uuid: str, visible_uuids: Optional[set[str]]) -> list[dict[str, Any]]:
        """Public wrapper around _entity_own_facts for a caller that already
        has a resolved entity uuid in hand (e.g. ContextOrchestrator's
        causal-chain-empty fallback below) and just needs that entity's own
        directly-touching facts of any relationship type, without going
        through search_graphiti_facts's full name-resolution flow again."""
        return self._entity_own_facts(uuid, visible_uuids)

    async def causal_chain_for_query(
        self, query_text: str, group_ids: Optional[list[str]], visible_uuids: Optional[set[str]]
    ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]], list[dict[str, Any]]]:
        """Resolves query_text the same way search_graphiti_facts does (see
        _resolve_named_entities), then either:

          - two named entities resolved (e.g. "How is X connected to Y?"):
            walks the shortest connecting path between them
            (_relationship_path_full_facts) -- the causal-mode counterpart
            of search_graphiti_facts's own two-entity branch. This used to
            not exist at all: a query naming two entities silently anchored
            on whichever ONE happened to resolve first (regex/candidate
            order, not query semantics) and returned that entity's own
            unrelated facts as if they explained a connection to the
            other -- a real bug, not just an unhelpful fallback, found
            against real production data (see CLAUDE.md).
          - one named entity resolved: walks the causal-typed chain out
            from it (_causal_chain_facts_from), same as before this fix.
          - nothing resolved: returns (None, None, []).

        Returns (anchor, second_entity, facts). anchor is the "primary"
        entity either way (the first resolved candidate), so a
        single-entity caller reads the same as before; second_entity is
        only ever set in the two-entity case. facts is empty when nothing
        resolved, or the chain/path itself came back empty (no causal-typed
        edges from the anchor, or no path at all between the two entities).
        Unlike the single-anchor walk, a two-entity path's facts are NOT
        guaranteed to be causal-typed -- see is_entirely_causal, which the
        caller uses to decide whether to treat it as a real causal chain or
        report it as a plain, fully-sourced connection instead.
        """
        if not self.graphiti or not group_ids:
            return None, None, []
        resolved_groups, _saw_unresolved = await self._resolve_named_entities(query_text, group_ids, visible_uuids)
        if not resolved_groups:
            return None, None, []
        anchor = resolved_groups[0][0]
        if len(resolved_groups) >= 2:
            second_entity = resolved_groups[1][0]
            facts = self._relationship_path_full_facts(
                anchor["uuid"], second_entity["uuid"], group_ids, visible_uuids
            ) or []
            return anchor, second_entity, facts
        facts = self._causal_chain_facts_from(anchor["uuid"], group_ids, visible_uuids)
        return anchor, None, facts

    async def search_graphiti_facts(
        self,
        query_text: str,
        group_ids: Optional[list[str]] = None,
        visible_uuids: Optional[set[str]] = None,
        num_results: int = 8,
    ) -> list[dict[str, Any]]:
        """Query for facts relevant to query_text, including whether each fact is
        still currently valid or has since been superseded/invalidated.

        num_results only bounds the fallback semantic-search branch at the
        bottom of this method (a named entity resolved via _resolve_named_entities
        returns all of that entity's own edges regardless -- see below).

        `visible_uuids`, if given, drops any fact where *neither* end is
        something this caller directly owns -- role-based visibility for the
        Ask path (see app/graph/authorization.py). Requiring only *one* end to
        be owned (not both) is deliberate: a visible customer's employer or
        assets should still show up as context about that customer, even
        though the employer/asset itself isn't separately assigned to anyone.
        What this does still prevent is a fact between two entities *neither*
        of which the caller owns just because it matched the search
        semantically.

        If the query names one or more specific entities (e.g. "What's changed
        about Rhodes Furniture?" or "How is X connected to Y?"), those names are
        resolved against the graph first instead of relying on Graphiti's hybrid
        search -- which has no relevance threshold and will happily pad out a
        sparsely-connected entity with other entities' unrelated facts that
        merely read similarly, or answer a two-entity connection question by
        just describing one of them. A single resolved entity gets its own
        edges/summary directly; two resolved entities get the path of facts
        connecting them, if any. A name that doesn't resolve to anything real
        short-circuits with a clear "not found" rather than substituting
        unrelated results.
        """
        if not self.graphiti:
            logger.warning("Graphiti instance not set in GraphRepository.")
            return []

        resolved_groups, saw_unresolved = (
            await self._resolve_named_entities(query_text, group_ids, visible_uuids) if group_ids else ([], False)
        )

        if saw_unresolved and not resolved_groups:
            return [{
                "fact": "No entity matching that name was found in this knowledge base.",
                "source_node_uuid": "",
                "target_node_uuid": "",
                "valid_at": None,
                "invalid_at": None,
                "expired_at": None,
                "is_valid": True,
            }]

        if len(resolved_groups) >= 2:
            # Two (or more) DIFFERENTLY-named entities resolved -- a
            # "how is X connected to Y" query. Each group may itself hold
            # several reconciled rows (see _match_entities_by_name); any one
            # row is a valid endpoint for the path lookup, since they're all
            # the same real-world thing.
            a_group, b_group = resolved_groups[0], resolved_groups[1]
            a, b = a_group[0], b_group[0]
            path_facts = self._relationship_path_facts(a["uuid"], b["uuid"], visible_uuids)
            if path_facts:
                return path_facts
            return [{
                "fact": f'No connection found between "{a["name"]}" and "{b["name"]}" in this knowledge base.',
                "source_node_uuid": "",
                "target_node_uuid": "",
                "valid_at": None,
                "invalid_at": None,
                "expired_at": None,
                "is_valid": True,
            }]

        if len(resolved_groups) == 1:
            # One named entity resolved -- possibly to several rows if it's
            # the same name across more than one connector (see
            # _match_entities_by_name's reconciliation). Pool every row's
            # own edges directly (precise by construction). Also skips a
            # paid search call we'd otherwise throw away.
            facts: list[dict[str, Any]] = []
            for resolved in resolved_groups[0]:
                own_facts = self._entity_own_facts(resolved["uuid"], visible_uuids)
                if not own_facts and resolved.get("summary"):
                    # Only fall back to this row's own `summary` property
                    # when it has no edges at all (e.g. an entity extracted
                    # with rich attributes but no relationships -- see the
                    # "Contoso Store Washington DC" case this was built
                    # for). When edges DO exist, summary must NOT be used:
                    # Graphiti's node summary is a running accumulation of
                    # every fact ever seen about the entity, with no
                    # temporal awareness -- for "Contoso Ltd", it still
                    # lists "Sarah Chen manages the account" verbatim even
                    # after Marcus Lee took over. Showing that as a lead
                    # line reintroduces exactly the inaccurate,
                    # already-superseded info the orchestrator's
                    # transition/is_valid handling exists to filter out.
                    own_facts = [{
                        "fact": resolved["summary"],
                        "source_node_uuid": resolved["uuid"],
                        "target_node_uuid": resolved["uuid"],
                        "valid_at": None,
                        "invalid_at": None,
                        "expired_at": None,
                        "group_id": resolved.get("group_id"),
                        "is_valid": True,
                        "kind": "entity_summary",
                    }]
                facts.extend(own_facts)
            return facts

        # Graphiti's own default (10) is tuned for open-ended browsing, not a
        # single answer -- trimmed by default since every extra fact both adds
        # noise to the response and lengthens the synthesis call that follows
        # it. The caller (see app/api/context.py's result_limit) can still ask
        # for more on a broad question via a "see more results" follow-up,
        # rather than everyone paying for a bigger default every time.
        results = await self.graphiti.search(query_text, group_ids=group_ids, num_results=num_results)
        kept = [
            r for r in results
            if visible_uuids is None
            or getattr(r, "source_node_uuid", "") in visible_uuids
            or getattr(r, "target_node_uuid", "") in visible_uuids
        ]
        # Graphiti's own EntityEdge already carries `episodes` (the same
        # property the raw-Cypher branches above read directly) -- no extra
        # round trip needed to get at it here, just to resolve it to source_description.
        sources_by_row = self._resolve_episode_sources([getattr(r, "episodes", []) for r in kept])
        facts = []
        for r, sources in zip(kept, sources_by_row):
            facts.append({
                "fact": r.fact,
                "relationship_type": getattr(r, "name", None),
                "source_node_uuid": getattr(r, "source_node_uuid", ""),
                "target_node_uuid": getattr(r, "target_node_uuid", ""),
                "valid_at": getattr(r, "valid_at", None),
                "invalid_at": getattr(r, "invalid_at", None),
                "expired_at": getattr(r, "expired_at", None),
                "group_id": getattr(r, "group_id", None),
                "sources": sources,
                "is_valid": getattr(r, "expired_at", None) is None and _not_yet_invalidated(getattr(r, "invalid_at", None)),
                # Distinguishes these from the entity-resolution branches above
                # (whose facts carry no "kind", or "entity_summary") -- the
                # orchestrator uses this to tell whether num_results actually
                # capped anything, since only this branch is capped at all.
                "kind": "semantic_search",
            })
        return facts
