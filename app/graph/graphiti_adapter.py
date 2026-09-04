# Graphiti is the temporal knowledge-graph library this project builds on: you feed
# it plain-text "episodes" (a CRM note, an email, a transcript...) and it uses an LLM
# to extract entities/facts from the text, then stores them in Neo4j with built-in
# support for tracking when a fact became true and when it stopped being true.
# This module has two responsibilities:
#   1. build_graphiti(): construct a configured Graphiti client (LLM + embedder +
#      reranker + the Neo4j connection details), so every other module that needs
#      Graphiti builds it the same way instead of repeating this setup.
#   2. GraphitiAdapter: a small helper for writing a plain (id, properties) record
#      straight to Neo4j as an "Episode" node, bypassing Graphiti's LLM extraction.
#      Useful for tests/demos where you want a predictable node without spending an
#      LLM call.
import logging
from typing import Any, Optional

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
from graphiti_core.embedder.azure_openai import AzureOpenAIEmbedderClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.gemini_client import GeminiClient
from graphiti_core.llm_client.azure_openai_client import AzureOpenAILLMClient
from openai import AsyncAzureOpenAI
from anthropic import AsyncAnthropic, AsyncAnthropicFoundry

from app.config import settings
from app.graph.caching_anthropic_client import CachingAnthropicClient
from app.graph.neo4j_client import Neo4jClient
from app.graph.spend_limiter import estimate_cost_usd, get_limiter

logger = logging.getLogger(__name__)


def _apply_spend_limit_openai(
    azure_client: AsyncAzureOpenAI, bucket: str, input_price: float, output_price: float, embedding_price: float
) -> None:
    """Wraps azure_client's chat/embeddings calls so every request this client
    makes (via the LLM client, embedder, and reranker below, which all share
    this one azure_client instance) is checked against, and counted toward,
    the local budget for `bucket`. See app/graph/spend_limiter.py."""
    limiter = get_limiter()
    original_chat_create = azure_client.chat.completions.create
    original_chat_parse = azure_client.beta.chat.completions.parse
    original_embeddings_create = azure_client.embeddings.create

    async def limited_chat_create(*args, **kwargs):
        limiter.ensure_room(bucket)
        response = await original_chat_create(*args, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            limiter.record(bucket, estimate_cost_usd(usage.prompt_tokens, usage.completion_tokens, input_price, output_price))
        return response

    async def limited_chat_parse(*args, **kwargs):
        # Azure's structured-output path (JSON-schema-constrained responses,
        # e.g. graphiti_core's extraction calls and our own answer-synthesis
        # call in app/context/orchestrator.py) goes through .parse(), not
        # .create(), so it needs its own wrapper.
        limiter.ensure_room(bucket)
        response = await original_chat_parse(*args, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            limiter.record(bucket, estimate_cost_usd(usage.prompt_tokens, usage.completion_tokens, input_price, output_price))
        return response

    async def limited_embeddings_create(*args, **kwargs):
        limiter.ensure_room(bucket)
        response = await original_embeddings_create(*args, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            limiter.record(bucket, estimate_cost_usd(usage.prompt_tokens, 0, embedding_price))
        return response

    azure_client.chat.completions.create = limited_chat_create
    azure_client.beta.chat.completions.parse = limited_chat_parse
    azure_client.embeddings.create = limited_embeddings_create


def _apply_spend_limit_anthropic(
    anthropic_client: "AsyncAnthropic | AsyncAnthropicFoundry", bucket: str, input_price: float, output_price: float
) -> None:
    """Same idea as _apply_spend_limit_openai above, but for Anthropic's SDK
    shape: one call (messages.create) instead of three, and usage fields
    named input_tokens/output_tokens rather than prompt_tokens/completion_tokens."""
    limiter = get_limiter()
    original_create = anthropic_client.messages.create

    async def limited_create(*args, **kwargs):
        limiter.ensure_room(bucket)
        response = await original_create(*args, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            cost = estimate_cost_usd(usage.input_tokens, usage.output_tokens, input_price, output_price)
            # Prompt caching (see app/graph/caching_anthropic_client.py) bills
            # a cache write/read as separate token counts that the plain
            # input_tokens figure above doesn't include at all. Ignoring
            # them here would silently under-count real spend the moment
            # caching is active, exactly the kind of gap the spend limiter
            # exists to not have. Per Anthropic's published pricing, a cache
            # write costs about 1.25x a normal input token, a cache read
            # about 0.1x.
            cache_creation_tokens = getattr(usage, "cache_creation_input_tokens", 0) or 0
            cache_read_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
            cost += cache_creation_tokens / 1_000_000 * input_price * 1.25
            cost += cache_read_tokens / 1_000_000 * input_price * 0.1
            limiter.record(bucket, cost)
        return response

    anthropic_client.messages.create = limited_create


def _build_gemini_embedder_and_reranker(api_key: str):
    """Split out from _build_gemini_clients below so "anthropic" mode can
    reuse this for embeddings/reranking (Claude has no embeddings API of its
    own, see app/config.py's llm_provider docstring) without also getting
    Gemini's chat/extraction client."""
    embedder = GeminiEmbedder(config=GeminiEmbedderConfig(api_key=api_key, embedding_model=settings.embedding_model))
    cross_encoder = GeminiRerankerClient(config=LLMConfig(api_key=api_key))
    return embedder, cross_encoder


def _build_gemini_clients(api_key: str):
    """Every tenant brings their own Gemini key (see app/config.py's TenantConfig),
    which is why this path takes an api_key argument: one Graphiti client per
    tenant, each billed to that tenant's own Gemini account."""
    llm_client = GeminiClient(
        config=LLMConfig(api_key=api_key, model=settings.llm_model, small_model=settings.small_llm_model)
    )
    embedder, cross_encoder = _build_gemini_embedder_and_reranker(api_key)
    return llm_client, embedder, cross_encoder


def _build_azure_openai_clients(bucket: str):
    """Azure OpenAI is an operator-wide resource, not a per-tenant one.
    Unlike Gemini, there's no separate key per client here; every tenant's
    Graphiti client is built from the same enterprise Azure deployment.
    Reach for this when Gemini's free-tier rate limit is the actual
    bottleneck (see scripts/ingest_samples.py's rate-limit backoff) rather
    than per-tenant billing isolation, which Azure OpenAI doesn't provide
    on its own.

    `bucket` selects which local spend budget (see app/graph/spend_limiter.py)
    this client's calls count against: "ingestion" or "query".
    """
    missing = [
        name
        for name, value in (
            ("azure_openai_endpoint", settings.azure_openai_endpoint),
            ("azure_openai_api_key", settings.azure_openai_api_key),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"llm_provider is 'azure_openai' but {', '.join(missing)} is not set. "
            f"See .env.example."
        )

    azure_client = AsyncAzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )
    _apply_spend_limit_openai(
        azure_client,
        bucket,
        settings.azure_openai_input_price_per_1m,
        settings.azure_openai_output_price_per_1m,
        settings.azure_openai_embedding_price_per_1m,
    )
    llm_client = AzureOpenAILLMClient(
        azure_client=azure_client,
        config=LLMConfig(model=settings.azure_openai_llm_deployment, small_model=settings.azure_openai_llm_deployment),
    )
    embedder = AzureOpenAIEmbedderClient(azure_client=azure_client, model=settings.azure_openai_embedding_deployment)
    cross_encoder = OpenAIRerankerClient(client=azure_client, config=LLMConfig(model=settings.azure_openai_llm_deployment))
    return llm_client, embedder, cross_encoder


def _build_azure_openai_embedder(bucket: str) -> AzureOpenAIEmbedderClient:
    """Just the embeddings half of what _build_azure_openai_clients builds,
    for reuse as anthropic mode's embedder (see _build_anthropic_clients
    below) when Gemini's free-tier embedding quota is the actual
    bottleneck -- that quota (generativelanguage.googleapis.com's
    embed_content_free_tier_requests) is a real, low, per-day cap (1000
    requests/day per project, found live: a Solandra CSV re-ingest of a
    few hundred rows exhausted it more than once in the same day, even
    across a freshly-issued Gemini key, since the cap is per Google Cloud
    project, not per key), independent of Anthropic's own chat quota,
    which isn't affected by it at all. Shares the same Azure OpenAI
    settings/spend-tracking _build_azure_openai_clients uses, scoped to
    just this one call path since anthropic mode's chat client is Claude,
    not this."""
    azure_client = AsyncAzureOpenAI(
        azure_endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key,
        api_version=settings.azure_openai_api_version,
    )
    _apply_spend_limit_openai(
        azure_client,
        bucket,
        settings.azure_openai_input_price_per_1m,
        settings.azure_openai_output_price_per_1m,
        settings.azure_openai_embedding_price_per_1m,
    )
    return AzureOpenAIEmbedderClient(azure_client=azure_client, model=settings.azure_openai_embedding_deployment)


def _build_anthropic_clients(bucket: str, gemini_api_key: str):
    """Anthropic is an operator-wide resource, same reasoning as Azure OpenAI
    above: one shared Claude key, not a key per tenant.

    Claude has no embeddings API (see app/config.py's llm_provider
    docstring), so embeddings come from elsewhere. By default that's
    Gemini, using `gemini_api_key` (the tenant's own key,
    TenantConfig.gemini_api_key, the same field "gemini" mode uses). If
    AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY are also set, embeddings
    move to Azure OpenAI instead (see _build_azure_openai_embedder and
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT), which is the escape hatch for when
    Gemini's free-tier embedding quota is the actual bottleneck. Reranking
    always stays on Gemini regardless: Gemini's reranker is a chat-style
    call against a separate quota from embed_content, so it isn't affected
    by that cap and there's no reason to move it too.
    """
    if not settings.anthropic_api_key:
        raise RuntimeError("llm_provider is 'anthropic' but anthropic_api_key is not set. See .env.example.")

    # A key issued through Microsoft Foundry (same place as an Azure OpenAI
    # resource) won't authenticate against the direct Anthropic API; it
    # needs AsyncAnthropicFoundry, pointed at the Foundry resource, instead.
    if settings.anthropic_foundry_resource:
        anthropic_client = AsyncAnthropicFoundry(
            resource=settings.anthropic_foundry_resource, api_key=settings.anthropic_api_key
        )
    else:
        anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    _apply_spend_limit_anthropic(
        anthropic_client, bucket, settings.anthropic_input_price_per_1m, settings.anthropic_output_price_per_1m
    )
    # CachingAnthropicClient, not the bare AnthropicClient graphiti_core
    # ships: see app/graph/caching_anthropic_client.py for why turning on
    # Anthropic prompt caching needs a subclass rather than a config flag.
    llm_client = CachingAnthropicClient(client=anthropic_client, config=LLMConfig(model=settings.anthropic_model))
    gemini_embedder, cross_encoder = _build_gemini_embedder_and_reranker(gemini_api_key)
    embedder = (
        _build_azure_openai_embedder(bucket)
        if settings.azure_openai_endpoint and settings.azure_openai_api_key
        else gemini_embedder
    )
    return llm_client, embedder, cross_encoder


def build_graphiti(
    neo4j_uri: Optional[str] = None,
    neo4j_user: Optional[str] = None,
    neo4j_password: Optional[str] = None,
    google_api_key: Optional[str] = None,
    bucket: str = "ingestion",
) -> Graphiti:
    """Build a Graphiti client wired up to Neo4j and an LLM provider for entity
    extraction, embeddings, and reranking search results.

    Which provider is controlled by settings.llm_provider ("gemini",
    "azure_openai", or "anthropic"), not by any argument here: it's an
    operator-wide choice, not a per-call one. `google_api_key` is used in
    "gemini" mode (where each tenant's own key comes in, via
    TenantGraphitiPool) and in "anthropic" mode (for embeddings/reranking,
    since Claude has no embeddings API of its own, see the llm_provider
    setting's docstring); it's ignored entirely in "azure_openai" mode, since
    that provider is one shared enterprise resource rather than a key per
    tenant.

    `bucket` only matters in "azure_openai"/"anthropic" mode: it's which
    local spend budget this client's calls count against (see
    app/graph/spend_limiter.py). Defaults to "ingestion" since every direct
    caller of this function (scripts/ingest_samples.py, scripts/seed_core_graph.py)
    is a data-loading script; TenantGraphitiPool passes "query" explicitly for
    the live API path.

    neo4j_uri/user/password fall back to settings regardless of provider;
    pass them explicitly only to point at a different database than .env
    (e.g. in tests).
    """
    uri = neo4j_uri or settings.neo4j_uri
    user = neo4j_user or settings.neo4j_user
    password = neo4j_password or settings.neo4j_password

    if settings.llm_provider == "azure_openai":
        llm_client, embedder, cross_encoder = _build_azure_openai_clients(bucket)
    elif settings.llm_provider == "anthropic":
        api_key = google_api_key or settings.google_api_key
        llm_client, embedder, cross_encoder = _build_anthropic_clients(bucket, api_key)
    elif settings.llm_provider == "gemini":
        api_key = google_api_key or settings.google_api_key
        llm_client, embedder, cross_encoder = _build_gemini_clients(api_key)
    else:
        raise ValueError(
            f"Unknown llm_provider: {settings.llm_provider!r} (expected 'gemini', 'azure_openai', or 'anthropic')"
        )

    # graphiti_core's own Neo4jDriver defaults to database="neo4j" unless told
    # otherwise. Explicit here so this works against an Aura instance whose
    # database is named after the instance id instead (see
    # settings.neo4j_database).
    driver = Neo4jDriver(uri, user, password, database=settings.neo4j_database)
    return Graphiti(graph_driver=driver, llm_client=llm_client, embedder=embedder, cross_encoder=cross_encoder)


class GraphitiAdapter:
    """Writes a plain record straight into Neo4j as an Episode node, without going
    through Graphiti's LLM-based extraction. Mainly useful for tests and demos."""

    def __init__(self, client: Optional[Neo4jClient] = None):
        self.neo4j_client = client or Neo4jClient()

    def ingest_episode(self, episode_id: str, properties: dict[str, Any]) -> bool:
        """Create or update an Episode node with the given id and properties.

        `properties` must contain only flat, scalar values (strings, numbers,
        booleans, or lists of those): that's what Neo4j's property storage
        supports; nested dicts or arbitrary objects will fail at write time.
        """
        if not episode_id:
            logger.error("Episode id is required.")
            return False

        # MERGE finds-or-creates the node by id, so calling this twice with the same
        # id updates the existing node instead of creating a duplicate.
        query = """
        MERGE (e:Episode {id: $id})
        SET e += $props
        """
        try:
            with self.neo4j_client.driver.session() as session:
                session.run(query, {"id": episode_id, "props": properties})
            logger.info(f"Episode {episode_id} stored in Neo4j.")
            return True
        except Exception as e:
            logger.error(f"Failed to store episode {episode_id}: {e}")
            return False

    def close(self) -> None:
        self.neo4j_client.close()


def demo_ingest() -> None:
    """Quick manual smoke test: `python -m app.graph.graphiti_adapter` writes one
    Episode node to whatever Neo4j database is configured in .env."""
    adapter = GraphitiAdapter()
    adapter.ingest_episode(
        "demo-001",
        {"title": "Demo Episode", "content": "Sample content for testing."},
    )
    adapter.close()


if __name__ == "__main__":
    demo_ingest()
