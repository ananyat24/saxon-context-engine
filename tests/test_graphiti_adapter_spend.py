# Tests app/graph/graphiti_adapter.py's _apply_spend_limit_anthropic() --
# specifically that it accounts for prompt-cache token usage
# (cache_creation_input_tokens/cache_read_input_tokens), which Anthropic
# bills as separate line items the plain input_tokens count doesn't
# include. Now that app/graph/caching_anthropic_client.py turns caching on
# for every Anthropic call, ignoring these would silently under-count real
# spend: this test exists to catch exactly that regression.
import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.graph import graphiti_adapter


def _fake_response(input_tokens=0, output_tokens=0, cache_creation_tokens=0, cache_read_tokens=0):
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.cache_creation_input_tokens = cache_creation_tokens
    usage.cache_read_input_tokens = cache_read_tokens
    response = MagicMock()
    response.usage = usage
    return response


class _FakeLimiter:
    def __init__(self):
        self.recorded = []

    def ensure_room(self, bucket):
        pass

    def record(self, bucket, cost_usd):
        self.recorded.append((bucket, cost_usd))


def test_plain_call_with_no_caching_is_unaffected(monkeypatch):
    fake_limiter = _FakeLimiter()
    monkeypatch.setattr(graphiti_adapter, "get_limiter", lambda: fake_limiter)

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=_fake_response(input_tokens=1000, output_tokens=200))

    graphiti_adapter._apply_spend_limit_anthropic(fake_client, "query", input_price=1.0, output_price=5.0)
    asyncio.run(fake_client.messages.create())

    bucket, cost = fake_limiter.recorded[0]
    assert bucket == "query"
    assert cost == 1000 / 1_000_000 * 1.0 + 200 / 1_000_000 * 5.0


def test_cache_creation_and_read_tokens_are_counted_toward_spend(monkeypatch):
    fake_limiter = _FakeLimiter()
    monkeypatch.setattr(graphiti_adapter, "get_limiter", lambda: fake_limiter)

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        return_value=_fake_response(
            input_tokens=100, output_tokens=50, cache_creation_tokens=5000, cache_read_tokens=20000
        )
    )

    graphiti_adapter._apply_spend_limit_anthropic(fake_client, "ingestion", input_price=1.0, output_price=5.0)
    asyncio.run(fake_client.messages.create())

    bucket, cost = fake_limiter.recorded[0]
    assert bucket == "ingestion"
    expected = (
        100 / 1_000_000 * 1.0
        + 50 / 1_000_000 * 5.0
        + 5000 / 1_000_000 * 1.0 * 1.25
        + 20000 / 1_000_000 * 1.0 * 0.1
    )
    assert cost == expected
    # Without accounting for cache tokens, cost would only be ~0.00035 --
    # the cache-aware total should be substantially larger.
    assert cost > (100 / 1_000_000 * 1.0 + 50 / 1_000_000 * 5.0) * 10
