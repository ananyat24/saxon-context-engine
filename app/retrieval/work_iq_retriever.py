# A live, query-time retriever against Work IQ's universal MCP endpoint --
# see app/retrieval/fabric_iq_ontology_retriever.py's module docstring for
# the shared reasoning (direct MCP connection, delegated-user auth, one
# fixed identity regardless of which Saxon user asked). Uses Work IQ's
# `ask` tool (see CLAUDE.md's v7 section) -- "invoke Microsoft 365 Copilot
# for natural-language reasoning" over that connected person's own mail,
# calendar, chats, and files, the closest fit to a "query in, grounded
# answer out" retriever among Work IQ's 10 fixed tools (the other 9 are
# CRUD/schema-discovery tools this retriever has no use for).
import logging
from typing import Any, Optional

from app.ingestion.microsoft_oauth import refresh_access_token
from app.retrieval.mcp_query_helper import query_mcp_tool

logger = logging.getLogger(__name__)

_TOOL_NAME = "ask"
_ENDPOINT = "https://workiq.svc.cloud.microsoft/mcp"


class WorkIQRetriever:
    def __init__(self, refresh_token: str, scope: str):
        self.refresh_token = refresh_token
        self.scope = scope

    async def retrieve(
        self,
        query: str,
        group_ids: Optional[list[str]] = None,
        visible_uuids: Optional[set[str]] = None,
        num_results: int = 8,
    ) -> list[dict[str, Any]]:
        """Ignores group_ids/visible_uuids -- see
        FabricIQOntologyRetriever.retrieve's docstring for why."""
        try:
            access_token = await refresh_access_token(self.refresh_token, self.scope)
        except Exception as e:
            logger.warning(f"Could not refresh Work IQ access token: {e}")
            return []
        text = await query_mcp_tool(_ENDPOINT, access_token, _TOOL_NAME, query)
        if not text:
            return []
        return [{
            "fact": text,
            "source_node_uuid": "",
            "target_node_uuid": "",
            "valid_at": None,
            "invalid_at": None,
            "expired_at": None,
            "group_id": None,
            "sources": ["Work IQ"],
            "is_valid": True,
            "kind": "work_iq",
        }]
