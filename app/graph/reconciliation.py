# The "Reconcile" stage named in the reference architecture (Connect ->
# Resolve -> Reconcile -> Plan -> Rank -> Assemble -> Deliver -- see
# CLAUDE.md Part 2). Deferred when entity_resolution.py (the Resolve stage)
# was pulled out on its own, because Resolve already did a live, query-time
# version of "is this the same entity mentioned in two connectors" (see
# match_entities_by_name's exact/normalized-name reconciliation) -- this
# module is what that stage was missing: something that actually OWNS the
# decision, runs it once per sync instead of recomputing it on every query,
# writes it down so it's inspectable/auditable, and can catch a match
# stronger name-equality never will (two names that are close but don't
# normalize the same -- a typo, an abbreviation).
#
# Checked what's actually in this project's connector datasets before
# building this (see CLAUDE.md's Reconcile follow-up note): none of the
# bundled/demo connectors carry a real shared external key across each other
# (mock_crm's AccountID never appears in mock_email's free text, nor does an
# email domain map back to an account). Name is the only cross-connector
# signal that actually exists today, so this builds a *stronger* name
# matching tier (fuzzy similarity) rather than an ID-matching path with
# nothing real to match against. If a future connector's data does carry a
# real shared key, that's a strictly stronger tier to add above fuzzy here,
# not a rewrite of this module's shape.
#
# Two tiers, in order of confidence:
#   1. exact/normalized name match across different group_ids (the same
#      equality match_entities_by_name already does live) -- confident
#      enough to auto-merge, now persisted as a :SAME_AS edge instead of
#      recomputed every query.
#   2. fuzzy name match (similar but not normalized-equal) -- NOT confident
#      enough to auto-merge; written as a :ProposedMerge node a human has to
#      approve or reject (see approve_proposal/reject_proposal) before it
#      becomes a :SAME_AS edge.
# expand_same_as lets a query-time caller (GraphRepository) pull in
# whatever this stage has already linked, on top of Resolve's own live
# matching -- see that function's docstring for why one hop is enough.
import difflib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional, TypedDict

from app.graph.entity_resolution import ExecuteCypher, _normalize_entity_name

logger = logging.getLogger(__name__)

# Below this, two normalized names are treated as coincidentally similar,
# not the same entity -- picked by manual judgment (no real fuzzy-collision
# dataset exists yet to calibrate against, see this module's docstring).
# Worth revisiting once real production data surfaces actual near-miss
# pairs to tune against.
_FUZZY_MIN_SIMILARITY = 0.84
# A normalized name shorter than this is too short for ratio-based
# similarity to mean anything (e.g. "Co" vs "Go" scores high on length
# alone) -- skip fuzzy comparison entirely for names this short rather than
# risk a nonsense proposal.
_FUZZY_MIN_NAME_LENGTH = 4


class ReconcileResult(TypedDict):
    same_as_created: int
    proposals_created: int


def ensure_reconciliation_indexes(execute_cypher: ExecuteCypher) -> None:
    """Idempotent, safe to call on every startup -- same pattern as
    app/graph/connectors.py's ensure_connector_indexes."""
    execute_cypher("CREATE INDEX proposed_merge_tenant_id IF NOT EXISTS FOR (p:ProposedMerge) ON (p.tenant_id)", None)


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _existing_pairs(execute_cypher: ExecuteCypher, group_ids: list[str]) -> set[frozenset]:
    """Every uuid-pair already linked (:SAME_AS) or already proposed (any
    status -- including a rejected one, so a rejected proposal doesn't just
    get re-proposed on the next sync) among entities in these group_ids, so
    reconcile_tenant never creates a duplicate edge/proposal for the same
    pair on a repeat run."""
    same_as_rows = execute_cypher(
        "MATCH (a:Entity)-[:SAME_AS]-(b:Entity) WHERE a.group_id IN $group_ids AND b.group_id IN $group_ids "
        "RETURN DISTINCT a.uuid AS a, b.uuid AS b",
        {"group_ids": group_ids},
    )
    proposal_rows = execute_cypher(
        "MATCH (p:ProposedMerge) WHERE p.entity_a_group_id IN $group_ids AND p.entity_b_group_id IN $group_ids "
        "RETURN p.entity_a_uuid AS a, p.entity_b_uuid AS b",
        {"group_ids": group_ids},
    )
    return {frozenset((row["a"], row["b"])) for row in same_as_rows + proposal_rows}


def reconcile_tenant(execute_cypher: ExecuteCypher, tenant_id: str, group_ids: list[str]) -> ReconcileResult:
    """Runs both tiers across every entity in this tenant's group_ids (not
    just whichever connector just synced -- a newly-ingested entity in one
    connector might match an existing one in another, and that only shows
    up by looking at all of them together). Cheap enough to run in full on
    every sync at this project's data scale; would need to narrow to
    "entities touched since the last run" before that stops being true.

    NOT n:Decision, same as every other general entity query in this
    codebase (see entity_resolution.match_entities_by_name's docstring) --
    a :Decision is an internal audit record, not a business entity that
    could ever be "the same real-world thing" as another one.
    """
    rows = execute_cypher(
        "MATCH (n:Entity) WHERE n.group_id IN $group_ids AND NOT n:Decision "
        "RETURN n.uuid AS uuid, n.name AS name, n.group_id AS group_id",
        {"group_ids": group_ids},
    )
    already_paired = _existing_pairs(execute_cypher, group_ids)

    by_normalized: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_normalized.setdefault(_normalize_entity_name(row["name"]), []).append(row)

    same_as_created = 0
    for normalized, members in by_normalized.items():
        distinct_groups = {m["group_id"] for m in members}
        if len(distinct_groups) < 2:
            continue
        canonical = min(members, key=lambda m: m["uuid"])
        for member in members:
            if member["uuid"] == canonical["uuid"] or member["group_id"] == canonical["group_id"]:
                continue
            pair = frozenset((canonical["uuid"], member["uuid"]))
            if pair in already_paired:
                continue
            confidence = "exact" if member["name"].lower() == canonical["name"].lower() else "normalized"
            _create_same_as(execute_cypher, canonical["uuid"], member["uuid"], confidence)
            already_paired.add(pair)
            same_as_created += 1
            logger.debug(
                "reconcile: '%s' (%s) same_as '%s' (%s) via %s",
                member["name"], member["group_id"], canonical["name"], canonical["group_id"], confidence,
            )

    # Fuzzy tier: only across entities whose normalized names didn't already
    # group them together above, and only across different group_ids --
    # within-connector near-duplicates are Graphiti's own extraction/dedup
    # concern (see CLAUDE.md's contradiction-detection note), not this
    # cross-connector stage's job.
    normalized_groups = [(norm, members) for norm, members in by_normalized.items() if len(norm) >= _FUZZY_MIN_NAME_LENGTH]
    proposals_created = 0
    for i, (norm_a, members_a) in enumerate(normalized_groups):
        for norm_b, members_b in normalized_groups[i + 1:]:
            if _similarity(norm_a, norm_b) < _FUZZY_MIN_SIMILARITY:
                continue
            for a in members_a:
                for b in members_b:
                    if a["group_id"] == b["group_id"]:
                        continue
                    pair = frozenset((a["uuid"], b["uuid"]))
                    if pair in already_paired:
                        continue
                    score = _similarity(norm_a, norm_b)
                    _create_proposal(execute_cypher, tenant_id, a, b, score)
                    already_paired.add(pair)
                    proposals_created += 1
                    logger.debug(
                        "reconcile: proposed '%s' (%s) ~ '%s' (%s), similarity=%.2f",
                        a["name"], a["group_id"], b["name"], b["group_id"], score,
                    )

    return {"same_as_created": same_as_created, "proposals_created": proposals_created}


def _create_same_as(execute_cypher: ExecuteCypher, a_uuid: str, b_uuid: str, confidence: str) -> None:
    execute_cypher(
        "MATCH (a:Entity {uuid: $a}), (b:Entity {uuid: $b}) "
        "CREATE (a)-[:SAME_AS {confidence: $confidence, created_at: datetime()}]->(b)",
        {"a": a_uuid, "b": b_uuid, "confidence": confidence},
    )


def _create_proposal(execute_cypher: ExecuteCypher, tenant_id: str, a: dict, b: dict, score: float) -> None:
    execute_cypher(
        "CREATE (p:ProposedMerge {"
        "id: $id, tenant_id: $tenant_id, "
        "entity_a_uuid: $a_uuid, entity_a_name: $a_name, entity_a_group_id: $a_group_id, "
        "entity_b_uuid: $b_uuid, entity_b_name: $b_name, entity_b_group_id: $b_group_id, "
        "similarity: $score, status: 'pending', created_at: datetime(), decided_at: null"
        "})",
        {
            "id": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "a_uuid": a["uuid"], "a_name": a["name"], "a_group_id": a["group_id"],
            "b_uuid": b["uuid"], "b_name": b["name"], "b_group_id": b["group_id"],
            "score": score,
        },
    )


def expand_same_as(
    execute_cypher: ExecuteCypher, rows: list[dict[str, Any]], allowed_group_ids: list[str]
) -> list[dict[str, Any]]:
    """Pulls in whatever this stage has already linked via :SAME_AS on top of
    `rows` (whatever Resolve's own live name-matching already found), so a
    query grounds to an approved fuzzy merge that name-equality alone would
    never find (e.g. an approved "Acme Corp" ~ "Acme Corportion" typo pair).

    `allowed_group_ids` matters because reconcile_tenant links across a
    WHOLE tenant's group_ids, but any one query can be scoped narrower than
    that (a document set naming only some of a tenant's knowledge bases, or
    authorization.visible_uuids restricting further still) -- without this
    filter, expanding through a :SAME_AS edge could surface a fact from a
    group_id/entity this particular query has no business seeing, even
    though nothing here is a real data isolation bug (:SAME_AS itself is
    still scoped to one tenant elsewhere -- see reconcile_tenant). Callers
    already filtering by visible_uuids (see GraphRepository._resolve_named_entities)
    still need to re-apply that filter to whatever this returns.

    One hop is enough: reconcile_tenant always links every group member
    directly to one canonical node (a star, not a chain), and an approved
    proposal likewise always creates one direct edge between the two
    entities a human reviewed -- so any node already in `rows` is at most
    one :SAME_AS hop from every other node it should pull in. Undirected
    ([:SAME_AS] with no arrow) since which side reconcile_tenant happened to
    call "a" vs "b" isn't meaningful to a caller.
    """
    if not rows:
        return rows
    uuids = [r["uuid"] for r in rows]
    extra = execute_cypher(
        "MATCH (n:Entity)-[:SAME_AS]-(m:Entity) WHERE n.uuid IN $uuids AND NOT m.uuid IN $uuids "
        "AND m.group_id IN $allowed_group_ids "
        "RETURN DISTINCT m.uuid AS uuid, m.name AS name, m.summary AS summary, m.group_id AS group_id",
        {"uuids": uuids, "allowed_group_ids": allowed_group_ids},
    )
    if not extra:
        return rows
    logger.debug("reconcile: expanded %d row(s) to %d more via same_as", len(rows), len(extra))
    return rows + extra


def list_proposals(execute_cypher: ExecuteCypher, tenant_id: str, status: Optional[str] = None) -> list[dict[str, Any]]:
    if status:
        rows = execute_cypher(
            "MATCH (p:ProposedMerge {tenant_id: $tenant_id, status: $status}) "
            "RETURN p.id AS id, p.entity_a_uuid AS entity_a_uuid, p.entity_a_name AS entity_a_name, "
            "p.entity_a_group_id AS entity_a_group_id, p.entity_b_uuid AS entity_b_uuid, "
            "p.entity_b_name AS entity_b_name, p.entity_b_group_id AS entity_b_group_id, "
            "p.similarity AS similarity, p.status AS status, p.created_at AS created_at "
            "ORDER BY p.created_at DESC",
            {"tenant_id": tenant_id, "status": status},
        )
    else:
        rows = execute_cypher(
            "MATCH (p:ProposedMerge {tenant_id: $tenant_id}) "
            "RETURN p.id AS id, p.entity_a_uuid AS entity_a_uuid, p.entity_a_name AS entity_a_name, "
            "p.entity_a_group_id AS entity_a_group_id, p.entity_b_uuid AS entity_b_uuid, "
            "p.entity_b_name AS entity_b_name, p.entity_b_group_id AS entity_b_group_id, "
            "p.similarity AS similarity, p.status AS status, p.created_at AS created_at "
            "ORDER BY p.created_at DESC",
            {"tenant_id": tenant_id},
        )
    return rows


def approve_proposal(execute_cypher: ExecuteCypher, tenant_id: str, proposal_id: str) -> bool:
    """Approves a pending proposal: writes the :SAME_AS edge it proposed and
    marks it decided. Returns False (no-op) if the id doesn't belong to this
    tenant or isn't pending -- the same boundary/idempotency shape as
    app/graph/document_sets.py's delete_document_set."""
    rows = execute_cypher(
        "MATCH (p:ProposedMerge {id: $id, tenant_id: $tenant_id, status: 'pending'}) "
        "SET p.status = 'approved', p.decided_at = datetime() "
        "RETURN p.entity_a_uuid AS a, p.entity_b_uuid AS b",
        {"id": proposal_id, "tenant_id": tenant_id},
    )
    if not rows:
        return False
    _create_same_as(execute_cypher, rows[0]["a"], rows[0]["b"], "fuzzy_approved")
    return True


def reject_proposal(execute_cypher: ExecuteCypher, tenant_id: str, proposal_id: str) -> bool:
    rows = execute_cypher(
        "MATCH (p:ProposedMerge {id: $id, tenant_id: $tenant_id, status: 'pending'}) "
        "SET p.status = 'rejected', p.decided_at = datetime() "
        "RETURN p.id AS id",
        {"id": proposal_id, "tenant_id": tenant_id},
    )
    return bool(rows)
