# The "Resolve" stage named explicitly in the reference architecture this
# project's own spec is built against (Connect -> Resolve -> Reconcile ->
# Plan -> Rank -> Assemble -> Deliver -- see CLAUDE.md Part 2's pipeline
# diagram). Before this module existed, that stage was smeared across a
# handful of module-level regexes and two methods living inline in
# graph_repository.py, extended one production bug at a time: a lowercase
# name never becoming a candidate at all, a :Decision audit node never being
# excluded from a name match, a two-entity query only ever anchoring on
# whichever name resolved first. Every one of those was a gap in this exact
# stage, found the hard way, because nothing owned "what counts as a match"
# as a single, directly-tested contract.
#
# This module now owns that contract -- not a rewrite of the matching logic
# itself (every existing caller's behavior is unchanged, and the full
# real-Neo4j test suite this had before the move still passes unmodified
# against it), but a real home for it: one file, one set of tests
# (tests/test_candidate_extraction.py, tests/test_entity_reconciliation.py),
# and structured logging at every match/no-match decision point, so the next
# "why didn't this resolve" question is answered by reading a log line
# instead of re-deriving it from behavior in production.
#
# Two stages, in order:
#   1. Turn a query's text into candidate names/phrases worth trying
#      (_extract_candidate_entities, _extract_id_candidates,
#      _extract_lowercase_word_candidates).
#   2. For each candidate, decide whether/how it matches a real node in the
#      graph (match_entities_by_name), tagged with WHY it matched.
# resolve_named_entities ties both stages together for a caller that just
# wants "what does this query name, and did it resolve."
import asyncio
import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

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
# match_entities_by_name below, since a truncated fragment only ever
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
# NOT be treated the way an unresolved proper noun is (see resolve_named_entities)
# -- "since 2023" or "in 2024" would also match this pattern and aren't meant to
# short-circuit an otherwise normal query into a false "not found".
_ID_PHRASE_RE = re.compile(r"\b[A-Za-z][\w'.-]*\s+#?[\w-]*\d[\w-]*\b")

# Trailing legal-entity words that don't change *which* real-world company a
# name refers to -- "Fenwick & Cole Legal" and "Fenwick & Cole Legal, Inc."
# name the same entity, but the exact-match reconciliation in
# match_entities_by_name below would treat them as two unrelated ones.
# Deliberately still an equality check after stripping these (not a fuzzy/
# edit-distance match) -- see that function's docstring for why a looser match
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
    # Auxiliary/modal verbs -- commonly the capitalized first word of a
    # yes/no question ("Has X shipped?", "Should we escalate Y?"). Added
    # after finding a real bug these words caused, not the possessive one
    # they were originally suspected of (see _extract_candidate_entities'
    # own comment): a sentence-initial "Has" glues onto an immediately
    # adjacent real proper noun ("Has Ferrotek...") into one spurious
    # two-word candidate, which then fails to resolve and, because it's
    # still a proper-noun candidate, hard-short-circuits the whole query
    # into a false "no entity matching that name was found" -- even though
    # the real entity two words later resolves fine on its own.
    "has", "have", "had", "should", "would", "could", "shall", "may",
    "might", "must",
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
    proper nouns use (match_entities_by_name's exact/normalized/CONTAINS
    chain), so it only ever resolves to a name that's actually in the graph
    -- this doesn't loosen matching, only which words get a chance to try
    it. And, like the existing id-phrase candidates, a word here can never
    trigger the hard "not found" short-circuit on its own (see
    resolve_named_entities: only a candidate in proper_noun_set can set
    saw_unresolved) -- most words in an ordinary sentence obviously won't
    name anything, and that must fall through to normal search, not a false
    "not found".
    """
    words = {w.lower() for w in _WORD_RE.findall(query_text)}
    return sorted(words - _STOPWORDS, key=len, reverse=True)


_POSSESSIVE_RE = re.compile(r"['’]s\b|['’]\B")


def _normalize_entity_name(name: str) -> str:
    """A name equality check that isn't defeated by a legal suffix, "&" vs.
    "and", punctuation, extra whitespace, or a possessive -- see
    _LEGAL_SUFFIX_WORDS above. Two names that normalize the same are
    treated as the same real-world entity; two that don't are left alone.

    The possessive strip (found for real: "Ferrotek's" failed to resolve
    to "Ferrotek Components" even via this normalized tier, because
    _NAME_PUNCT_RE only ever stripped "." and ",", never an apostrophe)
    has to run before the legal-suffix-word split below, or "Ferrotek's"
    normalizes to the two words "ferrotek" + "s" and "s" gets treated as
    a spurious trailing word rather than removed as part of the same
    possessive token. \\u2019 is the Unicode right single quote ('), which
    a real client's own text (an email, a web page) uses far more often
    than a plain ASCII apostrophe.
    """
    normalized = _POSSESSIVE_RE.sub("", name.lower()).replace("&", " and ")
    normalized = _NAME_PUNCT_RE.sub("", normalized)
    words = _NAME_WS_RE.sub(" ", normalized).strip().split(" ")
    while words and words[-1] in _LEGAL_SUFFIX_WORDS:
        words.pop()
    return " ".join(words)


def _extract_candidate_entities(query_text: str) -> list[str]:
    candidates = set()
    for match in _PROPER_NOUN_RE.findall(query_text):
        words = match.split()
        # A sentence-initial word capitalized only because it starts the
        # sentence ("Has", "Should", ...) can glue onto an immediately
        # adjacent real proper noun and get extracted as part of the same
        # candidate -- see _STOPWORDS' comment for the real bug this
        # caused (a hard "not found" short-circuit on a query naming an
        # entity that actually resolves fine). Strip one leading word if
        # it's a stopword; a genuine multi-word proper noun never starts
        # with a bare auxiliary verb or filler word, so this can't strip
        # a real name down to a wrong one.
        while len(words) > 1 and words[0].lower() in _STOPWORDS:
            words = words[1:]
        if words:
            candidates.add(" ".join(words))
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


# execute_cypher is injected (rather than this module owning a Neo4j client
# of its own) so GraphRepository stays the one place that knows how to talk
# to Neo4j -- this module owns the resolution *logic*, not a second
# connection story.
ExecuteCypher = Callable[[str, Optional[dict[str, Any]]], list[dict[str, Any]]]


def match_entities_by_name(
    execute_cypher: ExecuteCypher, name: str, group_ids: list[str],
    restrict_to_named_entities: bool = False,
) -> list[dict[str, Any]]:
    """Matches `name` against real node names across every connector in
    group_ids -- a multi-connector document set (see
    app/graph/document_sets.py) needs entity resolution to work across
    all of them, not just the first.

    Returns *every* exact (case-insensitive) name match, not just one --
    this is the deterministic reconciliation step that lets "Fenwick &
    Cole Legal" mentioned in a CRM record and, separately, in an email
    resolve to the same real-world entity at query time, instead of only
    whichever one of its several per-connector nodes happened to be
    picked first (see resolve_named_entities/GraphRepository.search_graphiti_facts,
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
    # Strips a trailing possessive ("Ferrotek's" -> "Ferrotek") before any
    # of the three tiers below run, not just the normalized-equality one
    # (_normalize_entity_name already strips it too, but that tier alone
    # can't bridge a possessive-truncated single-word reference to a
    # longer real name -- "ferrotek" normalized never equals "ferrotek
    # components"; it's this function's own CONTAINS fallback tier that
    # actually has to run against the possessive-free candidate for that
    # case to resolve at all). Found for real: "Ferrotek's" failed to
    # resolve to "Ferrotek Components" through any of the three tiers,
    # because none of them ever saw the apostrophe stripped -- the exact
    # tier compares the raw candidate, and the final CONTAINS fallback
    # (below) also used the raw, un-normalized `name` unchanged.
    name = _POSSESSIVE_RE.sub("", name)
    # `restrict_to_named_entities` (set only for a lowercase single-word
    # candidate -- see resolve_named_entities) additionally excludes
    # Task/Event/Activity/Process/Transaction/Interaction/Observation-typed
    # nodes from the two CONTAINS-based tiers below. Those ontology types
    # (ontology/core.yaml) get auto-generated, sentence-shaped names
    # describing an action ("expedited qualification lot", "Reviewed Q3
    # supplier risk register") -- exactly the kind of name a common English
    # word is likely to appear inside as an ordinary part of speech, not
    # because the query is actually about that logged action. A real named
    # business entity (a person, company, order, component) doesn't have
    # this problem; this fallback was built for that case (a casually-typed,
    # lowercase person/company name -- see _extract_lowercase_word_candidates)
    # and was never meant to ground a query on an action-log entry.
    #
    # Found for real: "Who approved the expedited fix, and what did it
    # cost?" CONTAINS-matched "expedited" straight into a Task node named
    # "expedited qualification lot", silently anchoring the entire causal
    # walk on an unrelated part of the story instead of falling through to
    # search (which could have found the actual DEC-2026-014 approval
    # facts). Proper-noun and id-style candidates are unaffected -- a real
    # multi-word name or an id like "QE-2091" legitimately can and should
    # still match an Event/Issue-typed node.
    action_type_exclusion = (
        " AND NOT (n:Task OR n:Event OR n:Activity OR n:Process OR n:Transaction "
        "OR n:Interaction OR n:Observation)"
        if restrict_to_named_entities else ""
    )
    # NOT n:SaxonRecommendation throughout this function (and every other
    # general entity/fact query in this codebase) -- a :SaxonRecommendation
    # node is an internal audit record of a past generated recommendation
    # (see app/graph/decisions.py), not a business entity a person would
    # ever be asking about. Deliberately narrower than the ontology's own
    # :Decision entity type (which this node also carries) -- a real
    # client dataset can have its own genuine Decision business entities
    # that must stay retrievable, so the exclusion can't just be "any
    # :Decision node". It's labeled :Entity too (the ontology models
    # Decision as extending Event, which extends Entity), so without
    # this exclusion Saxon's own recommendations would be indistinguishable
    # from real data to every query in this module -- found for real in
    # production: a Decision node's own auto-generated name ("Recommendation
    # for: <query>") got sampled as a suggested-question topic, and its INVOLVES edge's
    # boilerplate fact text ("Saxon generated this recommendation while
    # analyzing: <query>") got returned as if it were a real fact about
    # the entity the Decision was about.
    exact_rows = execute_cypher(
        "MATCH (n:Entity) WHERE n.group_id IN $group_ids AND toLower(n.name) = toLower($name) AND NOT n:SaxonRecommendation "
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
        candidate_rows = execute_cypher(
            "MATCH (n:Entity) WHERE n.group_id IN $group_ids AND toLower(n.name) CONTAINS $core_token "
            "AND NOT n:SaxonRecommendation" + action_type_exclusion + " "
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
        matched = list(by_uuid.values())
        logger.debug(
            "resolve: '%s' matched %d row(s) via %s", name, len(matched),
            "exact_name" if exact_rows else "normalized_name",
        )
        return matched

    contains_rows = execute_cypher(
        "MATCH (n:Entity) WHERE n.group_id IN $group_ids AND toLower(n.name) CONTAINS toLower($name) "
        "AND NOT n:SaxonRecommendation" + action_type_exclusion + " "
        "RETURN n.uuid AS uuid, n.name AS name, n.summary AS summary, n.group_id AS group_id LIMIT 1",
        {"group_ids": group_ids, "name": name},
    )
    logger.debug(
        "resolve: '%s' matched %s via %s", name,
        contains_rows[0]["name"] if contains_rows else "nothing",
        "contains_fallback" if contains_rows else "no_match",
    )
    return contains_rows


async def resolve_named_entities(
    execute_cypher: ExecuteCypher,
    query_text: str,
    group_ids: list[str],
    visible_uuids: Optional[set[str]],
) -> tuple[list[list[dict[str, Any]]], bool]:
    """Looks for specifically-named entities in the query and matches each
    against real node names in this knowledge base, so a query like "What's
    changed about Rhodes Furniture?" or "What's the status of order 10248?"
    can be grounded to that exact node instead of left to semantic search
    to guess at.

    Three candidate sources feed this, tried in order of how strong a signal
    they are: proper nouns (_extract_candidate_entities, e.g. "Rhodes
    Furniture"), id-style phrases (_extract_id_candidates, e.g. "order
    10248"), and -- only when no proper noun matched at all -- lenient
    single lowercase words (_extract_lowercase_word_candidates, e.g. "diego"
    from "what do we know about diego"). Only a proper noun that fails to
    resolve counts as saw_unresolved_candidate -- an id-style or lowercase-word
    candidate is loose enough to also match ordinary text ("since 2023",
    "what"), so an unresolved one there just falls through to normal search
    instead of forcing a "not found".

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
    connectors (see match_entities_by_name), meant to be pooled together
    by the caller, not treated as separate entities. saw_unresolved_candidate
    is True if some proper-noun-shaped phrase in the query didn't match
    anything visible, which the caller uses to say "not found" rather than
    silently falling back to an ungrounded search.
    """
    proper_nouns = _extract_candidate_entities(query_text)
    id_candidates = _extract_id_candidates(query_text)
    # Only when the query has no capitalized-phrase candidate at all --
    # a properly-capitalized "Diego Alvarez" (or a two-entity "X and Y")
    # already resolves precisely via proper_nouns above and shouldn't
    # pay for this broader, per-word scan too. See
    # _extract_lowercase_word_candidates's docstring for the bug this
    # covers (a casually-typed, uncapitalized name).
    lowercase_candidates = _extract_lowercase_word_candidates(query_text) if not proper_nouns else []
    all_candidates = proper_nouns + id_candidates + lowercase_candidates
    if not all_candidates:
        logger.debug("resolve: no candidates extracted from query, falling through to search")
        return [], False

    # Lowercase-word candidates alone get restrict_to_named_entities=True
    # (see match_entities_by_name) -- a proper noun or id-style candidate is
    # specific enough to legitimately match any entity type, including a
    # real Event/Issue/Transaction; a single common English word is not.
    lowercase_candidate_set = set(lowercase_candidates)
    rows_per_candidate = await asyncio.gather(
        *(
            asyncio.to_thread(
                match_entities_by_name, execute_cypher, c, group_ids,
                restrict_to_named_entities=(c in lowercase_candidate_set),
            )
            for c in all_candidates
        )
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
            # can't see -- don't leak existence either way. An id-phrase or
            # lowercase-word candidate that misses is expected (see
            # docstring) and never counts here.
            saw_unresolved = True
            logger.debug("resolve: proper-noun candidate '%s' did not resolve to anything visible", candidate)

    logger.debug(
        "resolve: query resolved to %d distinct entity group(s), saw_unresolved=%s",
        len(groups), saw_unresolved,
    )
    return groups, saw_unresolved
