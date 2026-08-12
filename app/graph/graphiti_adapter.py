# Graphiti is the temporal knowledge-graph library this project builds on: you feed
# it plain-text "episodes" (a CRM note, an email, a transcript...) and it uses an LLM
# to extract entities/facts from the text, then stores them in Neo4j with built-in
# support for tracking when a fact became true and when it stopped being true.
# This module has two responsibilities:
#   1. build_graphiti() -- construct a configured Graphiti client (LLM + embedder +
#      reranker + the Neo4j connection details), so every other module that needs
#      Graphiti builds it the same way instead of repeating this setup.
#   2. GraphitiAdapter -- a small helper for writing a plain (id, properties) record
#      straight to Neo4j as an "Episode" node, bypassing Graphiti's LLM extraction.
#      Useful for tests/demos where you want a predictable node without spending an
#      LLM call.
import logging
from typing import Any, Optional

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.gemini_client import GeminiClient

from app.config import settings
from app.graph.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)


def build_graphiti(
    neo4j_uri: Optional[str] = None,
    neo4j_user: Optional[str] = None,
    neo4j_password: Optional[str] = None,
    google_api_key: Optional[str] = None,
) -> Graphiti:
    """Build a Graphiti client wired up to Neo4j and Google's Gemini models (used
    here for entity extraction, embeddings, and reranking search results).

    Every argument is optional and falls back to app/config.py's `settings`, which
    is populated from the .env file -- pass an argument explicitly only when you
    need to point at a different database or key than the one in .env (e.g. in tests).
    """
    uri = neo4j_uri or settings.neo4j_uri
    user = neo4j_user or settings.neo4j_user
    password = neo4j_password or settings.neo4j_password
    api_key = google_api_key or settings.google_api_key

    return Graphiti(
        uri,
        user,
        password,
        llm_client=GeminiClient(
            config=LLMConfig(
                api_key=api_key,
                model=settings.llm_model,
                small_model=settings.small_llm_model,
            )
        ),
        embedder=GeminiEmbedder(
            config=GeminiEmbedderConfig(
                api_key=api_key,
                embedding_model=settings.embedding_model,
            )
        ),
        cross_encoder=GeminiRerankerClient(config=LLMConfig(api_key=api_key)),
    )


class GraphitiAdapter:
    """Writes a plain record straight into Neo4j as an Episode node, without going
    through Graphiti's LLM-based extraction. Mainly useful for tests and demos."""

    def __init__(self, client: Optional[Neo4jClient] = None):
        self.neo4j_client = client or Neo4jClient()

    def ingest_episode(self, episode_id: str, properties: dict[str, Any]) -> bool:
        """Create or update an Episode node with the given id and properties.

        `properties` must contain only flat, scalar values (strings, numbers,
        booleans, or lists of those) -- that's what Neo4j's property storage
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
