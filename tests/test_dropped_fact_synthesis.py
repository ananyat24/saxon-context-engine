# Regression coverage for a real bug found by testing against real ingested
# data: ContextOrchestrator.get_context_packet's summary-line construction
# only ever included a fact if it was currently valid, OR if _find_transitions
# could pair it (as the "old" side) with whatever fact replaced it: an
# invalidated fact that DIDN'T get paired (its replacement wasn't in this
# particular retrieval batch) was silently dropped: not a plain line, not a
# transition line, just gone. metadata.facts (the raw response) still had it,
# but metadata.summary said "No matching graph context found." even when this
# unpaired fact was the exact, correct answer to the question asked. Fixed by
# _build_answer_lines (app/context/orchestrator.py), which now keeps an
# unpaired invalidated fact, explicitly marked as no longer current rather
# than silently dropped.
#
# No real Neo4j needed: same stub-retriever pattern as
# test_orchestrator_observability.py.
import asyncio

from app.context.orchestrator import ContextOrchestrator


class _StubRetriever:
    def __init__(self, facts: list[dict]):
        self._facts = facts

    async def retrieve(self, query, group_ids=None, visible_uuids=None, num_results=8):
        return self._facts


def _orchestrator_with_facts(facts: list[dict]) -> ContextOrchestrator:
    orchestrator = ContextOrchestrator(graphiti_instance=object())
    orchestrator.retrievers = [_StubRetriever(facts)]
    return orchestrator


def _fact(text: str, **overrides) -> dict:
    base = {
        "fact": text,
        "source_node_uuid": "s1",
        "target_node_uuid": "t1",
        "valid_at": None,
        "invalid_at": None,
        "expired_at": None,
        "is_valid": True,
    }
    base.update(overrides)
    return base


def test_a_lone_unpaired_invalidated_fact_is_kept_not_dropped():
    # The exact real-world shape: only fact retrieved is invalidated, and
    # its replacement (if one even exists) isn't in this batch: used to
    # produce "No matching graph context found." despite this real,
    # relevant fact being available the whole time.
    invalidated = _fact(
        "Plant 1 does not share suppliers with Plant 2 on the relay quality issue.",
        is_valid=False, invalid_at="2026-08-20T11:03:00Z", source_node_uuid="s1",
    )
    orchestrator = _orchestrator_with_facts([invalidated])

    packet = asyncio.run(orchestrator.get_context_packet("Was Plant 1 ever mentioned in connection with the issue?"))

    assert packet.metadata["summary"] != "No matching graph context found."
    assert "Plant 1 does not share suppliers with Plant 2" in packet.metadata["summary"]


def test_a_paired_transition_is_still_described_not_duplicated():
    # The old/paired case must keep working exactly as before: a real
    # replacement in the same batch still produces one clean "this changed"
    # line, not the raw old fact ALSO appearing separately.
    old = _fact("Priya Nadeem manages the account.", is_valid=False, invalid_at="2026-08-22T09:00:00Z", source_node_uuid="s1")
    new = _fact("Diego Alvarez manages the account.", is_valid=True, valid_at="2026-08-22T09:00:00Z", source_node_uuid="s1")
    orchestrator = _orchestrator_with_facts([old, new])

    packet = asyncio.run(orchestrator.get_context_packet("Who manages the account?"))

    assert "this changed" in packet.metadata["summary"].lower()
    assert packet.metadata["summary"].count("Priya Nadeem manages the account.") == 1


def test_an_unpaired_invalid_fact_and_a_current_fact_both_appear():
    current = _fact("Order SO-1 is currently shipped.", is_valid=True, source_node_uuid="s2")
    unpaired_invalid = _fact(
        "Order SO-1 was previously flagged for a quality hold.", is_valid=False,
        invalid_at="2026-08-01T00:00:00Z", source_node_uuid="s1",
    )
    orchestrator = _orchestrator_with_facts([current, unpaired_invalid])

    packet = asyncio.run(orchestrator.get_context_packet("What's the history of Order SO-1?"))

    assert "Order SO-1 is currently shipped." in packet.metadata["summary"]
    assert "Order SO-1 was previously flagged for a quality hold." in packet.metadata["summary"]
