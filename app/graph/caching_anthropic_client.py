# A thin subclass of graphiti_core's AnthropicClient that turns on Anthropic
# prompt caching for the system prompt on every call.
#
# Why this needs to exist at all: graphiti_core's own AnthropicClient sends
# `system=system_message.content` as a *plain string* to `messages.create()`
# (see _generate_response below). Anthropic's prompt caching only activates
# on a `system` block that carries an explicit `cache_control` marker, which
# a plain string can never do -- caching is opt-in per content block, not
# automatic. graphiti_core has no config knob for this, so the only way to
# enable it without a PR upstream is to override the one method that builds
# the request.
#
# What actually gets cached: the ontology-derived system prompt Graphiti
# builds for extraction, and this app's own short synthesis system prompt
# (see app/context/orchestrator.py's _synthesize_answer) -- both are large-ish
# and byte-for-byte identical across many calls in the same session/ingest
# batch. Anthropic's ephemeral cache lasts ~5 minutes and is refreshed on
# each hit, so back-to-back calls within a session (a connector sync
# ingesting several records, or a few queries in a row) get cache reads
# (~10% of base input cost) instead of paying full price for the same
# prompt every time. A single isolated call, or calls spread further apart
# than the cache TTL, sees no benefit -- this is a real but usage-pattern-
# dependent saving, not a guaranteed discount on every call.
#
# Maintenance note: this duplicates graphiti_core's _generate_response body
# (see graphiti_core/llm_client/anthropic_client.py) rather than wrapping
# it, because the system-prompt construction happens partway through that
# method, not at a point graphiti_core exposes a hook for. If graphiti_core
# changes that method's internals on a future upgrade, this override needs
# to be re-checked against it -- same kind of version-sensitivity already
# flagged for the `anthropic<1.0.0` pin elsewhere in this codebase.
import json
import typing

import anthropic
from anthropic.types import MessageParam
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.config import ModelSize
from graphiti_core.llm_client.errors import RateLimitError, RefusalError
from graphiti_core.prompts.models import Message
from pydantic import BaseModel


class CachingAnthropicClient(AnthropicClient):
    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int | None = None,
        model_size: ModelSize = ModelSize.medium,
    ) -> tuple[dict[str, typing.Any], int, int]:
        system_message = messages[0]
        user_messages = [{"role": m.role, "content": m.content} for m in messages[1:]]
        user_messages_cast = typing.cast("list[MessageParam]", user_messages)

        max_creation_tokens: int = self._resolve_max_tokens(max_tokens, self.model)

        try:
            tools, tool_choice = self._create_tool(response_model)
            # The one real change from graphiti_core's own implementation:
            # a list of one text block with cache_control, instead of a
            # plain string, is what actually turns caching on for this call.
            cached_system = [
                {
                    "type": "text",
                    "text": system_message.content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            result = await self.client.messages.create(
                system=cached_system,
                max_tokens=max_creation_tokens,
                temperature=self.temperature,
                messages=user_messages_cast,
                model=self.model,
                tools=tools,
                tool_choice=tool_choice,
            )

            input_tokens = 0
            output_tokens = 0
            if hasattr(result, "usage") and result.usage:
                input_tokens = getattr(result.usage, "input_tokens", 0) or 0
                output_tokens = getattr(result.usage, "output_tokens", 0) or 0

            for content_item in result.content:
                if content_item.type == "tool_use":
                    if isinstance(content_item.input, dict):
                        tool_args: dict[str, typing.Any] = content_item.input
                    else:
                        tool_args = json.loads(str(content_item.input))
                    return tool_args, input_tokens, output_tokens

            for content_item in result.content:
                if content_item.type == "text":
                    return (
                        self._extract_json_from_text(content_item.text),
                        input_tokens,
                        output_tokens,
                    )
                else:
                    raise ValueError(f"Could not extract structured data from model response: {result.content}")

            raise ValueError(f"Could not extract structured data from model response: {result.content}")

        except anthropic.RateLimitError as e:
            raise RateLimitError(f"Rate limit exceeded. Please try again later. Error: {e}") from e
        except anthropic.APIError as e:
            if "refused to respond" in str(e).lower():
                raise RefusalError(str(e)) from e
            raise e
