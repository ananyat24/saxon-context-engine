# Placeholder for fetching data that shouldn't be stored in the graph at all because
# it's only valid at query time -- e.g. "what's this machine's sensor reading right
# now", pulled live from an external API rather than from a fact ingested earlier.
# Not implemented yet; no live data source is configured in this project at this stage.
from typing import Any


class LiveDataRetriever:
    """Retriever fetching real-time external telemetry or transactional API data."""

    async def fetch_live_data(self, entity_id: str) -> dict[str, Any]:
        return {}
