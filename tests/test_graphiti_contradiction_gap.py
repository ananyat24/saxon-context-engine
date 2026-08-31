# Pins a real, demonstrated upstream gap (see CLAUDE.md's v1 status note,
# "Investigated, root cause identified, not fixed in this codebase"): when a
# re-synced record changes a status-style field, Graphiti's own LLM-driven
# edge dedup/contradiction judgment sometimes treats the new, genuinely
# different sentence as "the same fact" as the old one instead of a
# contradiction that should invalidate it -- so the old, now-false fact never
# gets invalidated. That was found on a real "held for inspection" ->
# "released" shipment status transition; this test reproduces the same shape
# (same entity, one status word changed) directly.
#
# This is Graphiti's own extraction/resolution behavior, not this app's code,
# so there's nothing here to assert "must pass" -- xfail(strict=False) is
# the point: if Graphiti ever gets more reliable at this and the invalidation
# starts happening, this test flips to XPASS, which is a visible signal
# ("the upstream gap CLAUDE.md documents may no longer apply -- worth
# rechecking whether it's still a real limitation") rather than a silent
# nothing. If it stays broken, it stays a quiet, expected xfail.
#
# Needs a real, reachable Neo4j AND a real LLM (extraction + dedup calls cost
# money and can take a while) -- unlike every other test in this suite, which
# either mocks Graphiti/the LLM entirely or only issues raw Cypher. Excluded
# from the default run via pyproject.toml's `-m "not real_llm"` addopts; run
# it explicitly with:
#
#   pytest tests/test_graphiti_contradiction_gap.py -m real_llm
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.graph.graph_repository import GraphRepository
from app.graph.graphiti_adapter import build_graphiti
from app.ingestion.pipeline import IngestionPipeline

pytestmark = pytest.mark.real_llm


@pytest.mark.xfail(
    strict=False,
    reason="known upstream gap in Graphiti's edge dedup/contradiction judgment -- see CLAUDE.md",
)
def test_status_change_on_resync_may_not_invalidate_the_old_fact():
    group_id = f"test_contradiction_gap_{uuid.uuid4().hex[:8]}"
    graphiti = build_graphiti()
    pipeline = IngestionPipeline(graphiti)
    repo = GraphRepository(graphiti_instance=graphiti)
    day1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    day2 = day1 + timedelta(days=1)

    try:
        asyncio.run(pipeline.ingest_episode(
            name="shipments_day1",
            body="Shipment SH-9001 is destined for Plant Contradiction Test and is held for inspection.",
            source_description="shipments.csv (Shipment)",
            group_id=group_id,
            reference_time=day1,
        ))
        asyncio.run(pipeline.ingest_episode(
            name="shipments_day2_update",
            body="Shipment SH-9001 is destined for Plant Contradiction Test and is released.",
            source_description="shipments.csv (Shipment)",
            group_id=group_id,
            reference_time=day2,
        ))

        rows = repo.execute_cypher(
            "MATCH ()-[r:RELATES_TO {group_id: $g}]->() "
            "WHERE toLower(r.fact) CONTAINS 'sh-9001' "
            "RETURN r.fact AS fact, r.invalid_at AS invalid_at, r.expired_at AS expired_at",
            {"g": group_id},
        )
        held_rows = [row for row in rows if "held for inspection" in row["fact"].lower()]
        released_rows = [row for row in rows if "released" in row["fact"].lower()]

        assert released_rows, (
            "the day2 episode should at least produce a 'released' fact -- "
            "if this fails, the gap has moved (no new fact extracted at all), "
            "not just the invalidation step this test targets"
        )
        old_fact_was_invalidated = any(
            row["invalid_at"] is not None or row["expired_at"] is not None for row in held_rows
        )
        assert old_fact_was_invalidated, (
            "known upstream gap (see CLAUDE.md): Graphiti judged the "
            "'held for inspection' and 'released' sentences as the same "
            "fact instead of a contradiction, so the stale fact was never "
            "invalidated. If you're seeing this fail, the gap is still "
            "present -- expected, see this test's xfail marker."
        )
    finally:
        repo.execute_cypher("MATCH (n {group_id: $g}) DETACH DELETE n", {"g": group_id})
        asyncio.run(graphiti.close())
