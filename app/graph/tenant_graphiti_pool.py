# Each tenant supplies their own Gemini API key (see app/config.py's TenantConfig
# and app/security.py), which means a single shared Graphiti client -- the pattern
# used before this existed -- no longer works, since build_graphiti() bakes one
# Gemini key into the client at construction time. Building a fresh Graphiti
# client (LLM + embedder + reranker setup) on every single request would work,
# but it's real overhead to repeat per-request -- the same problem this project
# already fixed once for the single-tenant case (see app/main.py's lifespan
# handler). This pool keeps that fix while allowing one client per tenant instead
# of one for the whole process: each tenant's client is built once, on their
# first request, and reused after that.
import asyncio
import logging

from graphiti_core import Graphiti

from app.config import TenantConfig
from app.graph.graphiti_adapter import build_graphiti

logger = logging.getLogger(__name__)


class TenantGraphitiPool:
    """Builds and caches one Graphiti client per tenant, keyed by group_id."""

    def __init__(self) -> None:
        self._clients: dict[str, Graphiti] = {}
        # Guards the check-then-build step below so two concurrent first-requests
        # for the same tenant can't each build (and leak) their own client.
        self._lock = asyncio.Lock()

    async def get_or_create(self, tenant: TenantConfig) -> Graphiti:
        existing = self._clients.get(tenant.group_id)
        if existing is not None:
            return existing

        async with self._lock:
            # Re-check: another request may have built it while we waited for the lock.
            existing = self._clients.get(tenant.group_id)
            if existing is not None:
                return existing

            logger.info(f"Building Graphiti client for tenant '{tenant.group_id}'")
            client = build_graphiti(google_api_key=tenant.gemini_api_key)
            self._clients[tenant.group_id] = client
            return client

    async def close_all(self) -> None:
        for group_id, client in self._clients.items():
            try:
                await client.close()
            except Exception as e:
                logger.error(f"Error closing Graphiti client for tenant '{group_id}': {e}")
        self._clients.clear()
