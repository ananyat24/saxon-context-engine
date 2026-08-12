import os
from typing import Optional, Any
from dotenv import load_dotenv
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.gemini_reranker_client import GeminiRerankerClient
from graphiti_core.embedder.gemini import GeminiEmbedder, GeminiEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.gemini_client import GeminiClient
import logging
from app.graph.neo4j_client import Neo4jClient
logger = logging.getLogger(__name__)
from app.config import settings
load_dotenv()


def build_graphiti(
    neo4j_uri: Optional[str] = None,
    neo4j_user: Optional[str] = None,
    neo4j_password: Optional[str] = None,
    google_api_key: Optional[str] = None,
) -> Graphiti:
    """Build and configure a Graphiti instance connected to Neo4j and Gemini."""
    uri = neo4j_uri or settings.neo4j_uri or os.environ.get("NEO4J_URI", "")
    user = neo4j_user or settings.neo4j_user or os.environ.get("NEO4J_USER", "")
    password = neo4j_password or settings.neo4j_password or os.environ.get("NEO4J_PASSWORD", "")
    api_key = google_api_key or settings.google_api_key or os.environ.get("GOOGLE_API_KEY", "")

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
    """Adapter to bridge Graphiti episodes with Neo4j storage.

    Provides methods to ingest episodes into Neo4j and query them.
    """

    def __init__(self, client: Neo4jClient | None = None):
        self.neo4j_client = client or Neo4jClient()

    def ingest_episode(self, episode_id: str, properties: dict[str, Any]) -> bool:
        """Persist a Graphiti episode to Neo4j as an ``Episode`` node.

        ``properties`` should contain only flat, scalar values Neo4j can store directly.
        """
        if not episode_id:
            logger.error("Episode id is required.")
            return False
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


def demo_ingest():
    """Simple demonstration script showing how to use GraphitiAdapter."""
    adapter = GraphitiAdapter()
    adapter.ingest_episode(
        "demo-001",
        {"title": "Demo Episode", "content": "Sample content for testing."},
    )
    adapter.close()


if __name__ == "__main__":
    demo_ingest()
