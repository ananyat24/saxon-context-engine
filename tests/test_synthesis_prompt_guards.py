# Regression coverage for a real synthesis-quality bug found live:
# ContextOrchestrator._synthesize_answer's system prompt already said
# "never add, infer, or assume anything not explicitly stated," but the
# model still answered "Grace Okafor approved the expedited fix, and it
# cost $38,400" when the only retrieved fact was "Owen Whitfield requests a
# budget exception from Grace Okafor for ... $38,400": a request ADDRESSED
# TO Grace got read as an action BY Grace. The real approval fact (a
# different person entirely) simply wasn't in the retrieved set at all, and
# the model guessed rather than saying so.
#
# An LLM's actual compliance with a prompt can't be asserted deterministically
# in a unit test (that needs the real model): what this DOES protect
# against is someone quietly weakening or removing the specific guardrail
# sentence later without noticing it exists for a reason. Also proves the
# synthesis call is wired correctly (right messages, right fallback).
import asyncio

from app.context.orchestrator import ContextOrchestrator


class _CapturingLLMClient:
    def __init__(self, response):
        self._response = response
        self.captured_messages = None

    async def generate_response(self, messages, response_model=None, max_tokens=None):
        self.captured_messages = messages
        return self._response


class _FakeGraphitiCapturing:
    def __init__(self, llm_response):
        self.llm_client = _CapturingLLMClient(llm_response)


class _StubRetriever:
    def __init__(self, facts: list[dict]):
        self._facts = facts

    async def retrieve(self, query, group_ids=None, visible_uuids=None, num_results=8):
        return self._facts


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


def test_synthesis_system_prompt_guards_against_request_target_conflation():
    orchestrator = ContextOrchestrator(graphiti_instance=_FakeGraphitiCapturing({"answer": "Not stated."}))
    orchestrator.retrievers = [_StubRetriever([
        _fact("Owen Whitfield requests a budget exception from Grace Okafor for $38,400."),
        _fact("Meridian Circuit Supply quoted an expedited qualification lot at $38,400."),
    ])]

    asyncio.run(orchestrator.get_context_packet("Who approved the expedited fix, and what did it cost?"))

    llm_client = orchestrator.graphiti.llm_client
    assert llm_client.captured_messages is not None
    system_content = llm_client.captured_messages[0].content
    assert "not that person's action" in system_content or "conflate" in system_content.lower()
    assert "isn't available" in system_content or "not stated" in system_content.lower()


def test_synthesis_answers_from_a_real_fake_llm_response():
    # Sanity check the wiring itself: with >1 current fact, get_context_packet
    # actually calls through to the (fake) LLM and uses its answer as the
    # summary, rather than silently falling back to a joined fact list.
    orchestrator = ContextOrchestrator(
        graphiti_instance=_FakeGraphitiCapturing({"answer": "The information isn't available in the given facts."})
    )
    orchestrator.retrievers = [_StubRetriever([
        _fact("Owen Whitfield requests a budget exception from Grace Okafor for $38,400."),
        _fact("Meridian Circuit Supply quoted an expedited qualification lot at $38,400."),
    ])]

    packet = asyncio.run(orchestrator.get_context_packet("Who approved the expedited fix, and what did it cost?"))

    assert packet.metadata["summary"] == "The information isn't available in the given facts."
