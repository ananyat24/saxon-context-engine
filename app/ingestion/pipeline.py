# Thin wrapper around Graphiti's `add_episode` call -- the entry point for getting
# any piece of text (a CRM note, an email, a transcript) into the graph. Graphiti
# handles the actual entity/fact extraction via an LLM; this class gives that call
# a consistent, logged interface and, importantly, passes the ontology schema into
# the extraction so the LLM picks from types the ontology defines rather than
# inventing its own (see app/ontology/graphiti_types.py for why that matters).
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class IngestionPipeline:
    """Orchestrates structured and unstructured context ingestion into Graphiti."""

    def __init__(
        self,
        graphiti_instance: Graphiti,
        entity_types: Optional[dict[str, type[BaseModel]]] = None,
        edge_types: Optional[dict[str, type[BaseModel]]] = None,
        edge_type_map: Optional[dict[tuple[str, str], list[str]]] = None,
    ):
        """`entity_types`/`edge_types`/`edge_type_map` come from
        app.ontology.graphiti_types.build_graphiti_schema(). Leaving them None
        falls back to Graphiti's unconstrained extraction, where it invents
        type names -- fine for a quick demo, not for a consistent graph.
        """
        self.graphiti = graphiti_instance
        self.entity_types = entity_types
        self.edge_types = edge_types
        self.edge_type_map = edge_type_map

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
            entity_types=self.entity_types,
            edge_types=self.edge_types,
            edge_type_map=self.edge_type_map,
        )
