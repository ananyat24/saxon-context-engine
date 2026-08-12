# Thin wrapper around Graphiti's `add_episode` call -- the entry point for getting
# any piece of text (a CRM note, an email, a transcript) into the graph. Graphiti
# handles the actual entity/fact extraction via an LLM; this class just gives that
# call a consistent, logged interface for the rest of the app to use.
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """Orchestrates structured and unstructured context ingestion into Graphiti."""

    def __init__(self, graphiti_instance: Graphiti):
        self.graphiti = graphiti_instance

    async def ingest_episode(
        self,
        name: str,
        body: str,
        source_description: str = "Context Engine Ingest",
        group_id: Optional[str] = None,
        reference_time: Optional[datetime] = None,
    ) -> Any:
        """Send one "episode" of text to Graphiti for extraction and storage.

        `body` is plain text -- Graphiti's LLM reads it and figures out what
        entities and facts it contains, then writes them into Neo4j. `group_id`
        is optional data isolation (e.g. one tenant's data vs. another's);
        `reference_time` is when the episode's content actually happened/was
        true, which matters for Graphiti's time-aware fact tracking.

        Graphiti's underlying add_episode() requires a reference_time and has no
        default for it, so if the caller doesn't supply one we fall back to "now"
        here rather than passing None through and letting it fail deeper in the call.
        """
        logger.info(f"Ingesting episode '{name}' into group '{group_id}'")
        return await self.graphiti.add_episode(
            name=name,
            episode_body=body,
            source=EpisodeType.text,
            source_description=source_description,
            reference_time=reference_time or datetime.now(timezone.utc),
            group_id=group_id,
        )
