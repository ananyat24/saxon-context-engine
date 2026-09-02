# Writes a causal-chain recommendation (see app/context/orchestrator.py's
# get_causal_context_packet) as a real, inspectable :Entity:Decision node --
# ontology/core.yaml's Decision type, defined from the start but never
# actually written to the graph until this. Turns a generated recommendation
# into an auditable graph fact a client can query/see provenance for later,
# rather than a throwaway string that only ever existed in one API response.
#
# Saxon never acts on a Decision it records here -- logging it is the whole
# scope. Execution against a recommendation is explicitly out of scope for
# this pivot; see CLAUDE.md.
#
# Also tagged with a second, Saxon-reserved label, :SaxonRecommendation --
# NOT the same thing as the node being ontology-typed :Decision. `Decision`
# (ontology/core.yaml) is a legitimate general-purpose business entity type
# ("a decision, approval, rejection, or recommendation made by a person,
# group, rule, workflow, or AI agent") -- a real client dataset can and does
# contain its own genuine Decision entities (e.g. a CSV literally named
# "decisions.csv", auto-inferring that exact type name). Every "don't
# surface Saxon's own internal audit trail as if it were a business fact"
# exclusion in this codebase used to filter on the ontology label `:Decision`
# directly -- which meant a real client's own Decision records got hidden by
# the same filter, everywhere, permanently (found via testing against real
# ingested data: a genuine $38,400 budget-approval record became
# unreachable by every retrieval path, including a query for it by its own
# literal ID). Filtering on this second, narrower label instead means only
# nodes Saxon itself generated are ever excluded -- a real business Decision
# entity from a client's own data is retrievable like anything else.
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.graph.graph_repository import GraphRepository


def ensure_decision_indexes(repo: Optional[GraphRepository] = None) -> None:
    """Idempotent, safe to call on every startup -- same pattern as
    app/graph/connectors.py's ensure_connector_indexes."""
    repo = repo or GraphRepository()
    repo.execute_cypher(
        "CREATE INDEX decision_group_id IF NOT EXISTS FOR (d:Decision) ON (d.group_id)"
    )
    repo.execute_cypher(
        "CREATE INDEX saxon_recommendation_group_id IF NOT EXISTS FOR (d:SaxonRecommendation) ON (d.group_id)"
    )


def record_decision(
    repo: GraphRepository,
    *,
    group_id: str,
    anchor_uuid: str,
    query: str,
    recommendation_text: str,
    rationale: str,
) -> str:
    """Creates a :Entity:Decision:SaxonRecommendation node carrying the
    recommendation, linked to the entity it's about via an INVOLVES edge
    (Decision extends Event in the ontology, and core.yaml already defines
    Event -INVOLVES-> Entity for exactly this shape). decision_status
    starts 'proposed' since nothing has acted on it -- there's no workflow
    yet to move it to approved/rejected, but the field exists on the
    ontology type for when one does. Returns the new Decision node's uuid.

    The extra :SaxonRecommendation label (see this module's own docstring)
    is what every "hide Saxon's own generated audit trail" query filters
    on -- not the ontology label :Decision itself, which a real client
    dataset can also legitimately use.

    The INVOLVES-target MATCH is scoped by group_id, not just uuid -- the
    only caller today (ContextOrchestrator.get_causal_context_packet)
    always resolves anchor_uuid from a Cypher query already scoped to the
    caller's own group_ids, so this is defensive rather than fixing a live
    bug: it keeps this function safe to call directly with an
    unvalidated/mismatched uuid without silently linking a Decision to
    another tenant's entity.
    """
    decision_uuid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    repo.execute_cypher(
        """
        CREATE (d:Entity:Decision:SaxonRecommendation {
            uuid: $uuid, name: $name, description: $description, rationale: $rationale,
            decision_status: 'proposed', source_system: 'saxon.causal_engine',
            group_id: $group_id, created_at: datetime($now)
        })
        WITH d
        MATCH (n:Entity {uuid: $anchor_uuid, group_id: $group_id})
        CREATE (d)-[:RELATES_TO {
            name: 'INVOLVES', fact: $fact, group_id: $group_id, valid_at: datetime($now)
        }]->(n)
        """,
        {
            "uuid": decision_uuid,
            "name": f"Recommendation for: {query}"[:200],
            "description": recommendation_text,
            "rationale": rationale,
            "group_id": group_id,
            "now": now,
            "anchor_uuid": anchor_uuid,
            "fact": f"Saxon generated this recommendation while analyzing: {query}",
        },
    )
    return decision_uuid
