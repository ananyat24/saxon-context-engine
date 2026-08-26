# Tests app/graph/caching_anthropic_client.py -- no real network, the
# underlying anthropic client is a mock. Confirms the one real behavioral
# difference from graphiti_core's own AnthropicClient: the system prompt is
# sent as a cache_control-marked block, not a plain string, which is what
# actually turns Anthropic prompt caching on for that call.
import asyncio
from unittest.mock import AsyncMock, MagicMock

from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.prompts.models import Message
from pydantic import BaseModel

from app.graph.caching_anthropic_client import CachingAnthropicClient


class _Answer(BaseModel):
    answer: str


def _fake_result(input_tokens=100, output_tokens=20):
    content_item = MagicMock()
    content_item.type = "tool_use"
    content_item.input = {"answer": "hi"}
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    result = MagicMock()
    result.content = [content_item]
    result.usage = usage
    return result


def test_generate_response_sends_cache_control_on_system_prompt():
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_fake_result())

    client = CachingAnthropicClient(client=fake_client, config=LLMConfig(model="claude-haiku-4-5", api_key="fake"))
    messages = [
        Message(role="system", content="You are a helpful system prompt."),
        Message(role="user", content="hi"),
    ]

    result, input_tokens, output_tokens = asyncio.run(
        client._generate_response(messages, response_model=_Answer)
    )

    assert result == {"answer": "hi"}
    assert input_tokens == 100
    assert output_tokens == 20

    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert call_kwargs["system"] == [
        {
            "type": "text",
            "text": "You are a helpful system prompt.",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_generate_response_still_extracts_from_plain_text_fallback():
    # Same "no tool_use content" fallback path graphiti_core's own client
    # has -- confirms the override didn't drop that behavior.
    text_item = MagicMock()
    text_item.type = "text"
    text_item.text = '{"answer": "from text"}'
    usage = MagicMock()
    usage.input_tokens = 5
    usage.output_tokens = 5
    result = MagicMock()
    result.content = [text_item]
    result.usage = usage

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=result)

    client = CachingAnthropicClient(client=fake_client, config=LLMConfig(model="claude-haiku-4-5", api_key="fake"))
    messages = [Message(role="system", content="sys"), Message(role="user", content="hi")]

    parsed, _, _ = asyncio.run(client._generate_response(messages, response_model=_Answer))
    assert parsed == {"answer": "from text"}
