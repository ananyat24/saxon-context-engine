# A live, query-time retriever against a Fabric IQ Ontology, queried
# directly (its own MCP endpoint), not through Foundry IQ. See
# app/retrieval/foundry_iq_retriever.py's module docstring for the more
# common path (Fabric IQ as a knowledge source Foundry IQ orchestrates).
# This module exists for the case where a tenant wants Fabric IQ Ontology
# grounding without standing up a Foundry IQ knowledge base at all.
#
# Genuinely different auth model from every other retriever/connector in
# this codebase: Fabric IQ Ontology's MCP endpoint requires delegated user
# authentication (Microsoft's own docs, verified 2026-09: "a BYO Entra
# app"), not a service credential. See app/config.py's
# microsoft_oauth_* settings and app/ingestion/microsoft_oauth.py for the
# consent flow this depends on. A query against this retriever answers
# grounded in whichever person connected it, the same way a query against
# Saxon's own graph answers grounded in the connectors a tenant has set
# up. It is NOT per-asking-user-scoped the way app/graph/authorization.py's
# as_user visibility is; see this retriever's own retrieve() docstring.
import logging
from typing import Any, Optional

from app.ingestion.microsoft_oauth import refresh_access_token
from app.retrieval.mcp_query_helper import query_mcp_tool

logger = logging.getLogger(__name__)

_TOOL_NAME = "search_ontology"
_ENDPOINT_TEMPLATE = (
    "https://agent365.svc.cloud.microsoft/agents/tenants/{tenant_id}/"
    "servers/mcp_FabricIQOntology/workspaces/{workspace_id}/ontologies/{ontology_id}"
)


class FabricIQOntologyRetriever:
    def __init__(self, tenant_id: str, workspace_id: str, ontology_id: str, refresh_token: str, scope: str):
        self.url = _ENDPOINT_TEMPLATE.format(tenant_id=tenant_id, workspace_id=workspace_id, ontology_id=ontology_id)
        self.refresh_token = refresh_token
        self.scope = scope

    async def retrieve(
        self,
        query: str,
        group_ids: Optional[list[str]] = None,
        visible_uuids: Optional[set[str]] = None,
        num_results: int = 8,
    ) -> list[dict[str, Any]]:
        """Ignores group_ids/visible_uuids, same reasoning as
        FoundryIQRetriever's own retrieve() docstring, sharpened here: this
        doesn't just have a separate permission model, it runs as one
        specific Microsoft identity (whoever completed the "Connect Fabric
        IQ Ontology" consent flow) regardless of which Saxon user asked the
        question. Fine for a single-tenant/demo setup or a service-style
        "connect as a shared account" pattern; not yet something that
        should be trusted to answer differently per Saxon as_user."""
        try:
            access_token = await refresh_access_token(self.refresh_token, self.scope)
        except Exception as e:
            logger.warning(f"Could not refresh Fabric IQ Ontology access token: {e}")
            return []
        text = await query_mcp_tool(self.url, access_token, _TOOL_NAME, query)
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
            "sources": ["Fabric IQ Ontology"],
            "is_valid": True,
            "kind": "fabric_iq_ontology",
        }]
