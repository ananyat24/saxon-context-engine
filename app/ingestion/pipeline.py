import logging
from datetime import datetime
from typing import Any, Dict, Optional
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
        logger.info(f"Ingesting episode '{name}' into group '{group_id}'")
        return await self.graphiti.add_episode(
            name=name,
            episode_body=body,
            source=EpisodeType.text,
            source_description=source_description,
            reference_time=reference_time,
            group_id=group_id,
        )
