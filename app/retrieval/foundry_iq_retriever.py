# A live, query-time retriever against a Microsoft Foundry IQ knowledge base.
# NOT an ingestion connector (see app/config.py's foundry_iq_* settings
# for why: Foundry IQ's own retrieve API is query-in/grounded-answer-out,
# Azure AI Search's agentic retrieval, with no "list everything" bulk
# endpoint a connector's fetch() could pull from and dedup on content_hash
# the way sharepoint_source.py/google_drive_source.py do).
#
# Implements the same TextRetriever protocol (app/retrieval/base.py)
# GraphRetriever does, so it plugs into ContextOrchestrator's existing
# `extra_retrievers` list: one more line where the orchestrator is
# constructed, no change to get_context_packet/get_causal_context_packet
# themselves. Facts this retriever returns flow through the exact same
# dedup/transition/authority-tie-break machinery every other fact does.
#
# One Foundry IQ knowledge base can itself be configured, on the Azure AI
# Search side (not in this codebase), to span Fabric IQ (`fabricOntology`/
# `fabricDataAgent` knowledge sources) and Work IQ (`workIQ` knowledge
# source). Foundry IQ is Microsoft's own single orchestration point over
# both, so this one retriever is genuinely "connect to Fabric IQ, Foundry
# IQ, and Work IQ," not three separate integrations. See CLAUDE.md's v7
# section for the full architecture this maps onto.
#
# Uses the GA (2026-04-01) extractive-only retrieve API deliberately, not
# the newer answerSynthesis preview mode. Saxon does its own synthesis
# (ContextOrchestrator._synthesize_answer) over a uniform fact list already;
# asking Foundry IQ to also synthesize an answer would produce a second,
# redundant LLM-generated sentence this codebase has no way to reconcile
# against its own.
import logging
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_API_VERSION = "2026-04-01"
_TIMEOUT_SECONDS = 10.0


def foundry_iq_configured() -> bool:
    """All three settings are required together: a partially-configured
    Foundry IQ retriever would fail every query it's asked to help with
    rather than just not being added to the retriever list. Mirrors how
    create_connector rejects a connector type whose required settings
    aren't all present (see app/api/connectors.py)."""
    return bool(settings.foundry_iq_search_endpoint and settings.foundry_iq_api_key and settings.foundry_iq_knowledge_base)


class FoundryIQRetriever:
    """Queries one Foundry IQ knowledge base live, per query. See this
    module's own docstring for why that's a retriever and not a connector."""

    def __init__(
        self,
        search_endpoint: Optional[str] = None,
        api_key: Optional[str] = None,
        knowledge_base: Optional[str] = None,
    ):
        self.search_endpoint = (search_endpoint or settings.foundry_iq_search_endpoint).rstrip("/")
        self.api_key = api_key or settings.foundry_iq_api_key
        self.knowledge_base = knowledge_base or settings.foundry_iq_knowledge_base

    async def retrieve(
        self,
        query: str,
        group_ids: Optional[list[str]] = None,
        visible_uuids: Optional[set[str]] = None,
        num_results: int = 8,
    ) -> list[dict[str, Any]]:
        """Ignores group_ids/visible_uuids: Foundry IQ's own knowledge
        base is a Microsoft-tenant-scoped resource with its own permission
        model (x-ms-query-source-authorization, document-level ACLs synced
        from SharePoint/Fabric/Work IQ), a genuinely separate authorization
        boundary from this app's group_id/role-based visibility. Not
        threading those through here is deliberate, not an oversight: a
        real integration must decide how (or whether) to map a Saxon
        as_user onto a Foundry IQ user-assertion token before this can be
        trusted with role-scoped queries, flagged explicitly in CLAUDE.md
        rather than silently ignored."""
        url = f"{self.search_endpoint}/knowledgebases/{self.knowledge_base}/retrieve"
        body = {
            "intents": [{"type": "semantic", "search": query}],
            "knowledgeSourceParams": [],
        }
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    url,
                    params={"api-version": _API_VERSION},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body,
                )
        except httpx.HTTPError as e:
            # A live external dependency being unreachable degrades this one
            # retriever to "found nothing," not a failed query: the same
            # never-break-the-whole-query principle every fetch()-based
            # connector's ConnectorFetchError already follows for ingestion.
            logger.warning(f"Foundry IQ retrieval failed for '{query}': {e}")
            return []
        if resp.status_code >= 400:
            logger.warning(f"Foundry IQ retrieval returned HTTP {resp.status_code} for '{query}': {resp.text[:300]}")
            return []
        return self._to_facts(resp.json(), num_results)

    def _to_facts(self, payload: dict, num_results: int) -> list[dict[str, Any]]:
        references = (payload.get("references") or [])[:num_results]
        facts = []
        for ref in references:
            text = ref.get("sourceData") or ref.get("docKey") or ""
            if not text:
                continue
            citation = ref.get("citationUrl")
            facts.append({
                "fact": text,
                # No real Neo4j uuids for an external result. Blank
                # source/target is the existing convention path-lookup
                # facts already use to opt out of the authority tie-break's
                # (source, target, relationship_type) grouping (see
                # ContextOrchestrator._apply_authority_tie_break).
                "source_node_uuid": "",
                "target_node_uuid": "",
                "valid_at": None,
                "invalid_at": None,
                "expired_at": None,
                "group_id": None,
                "sources": [f"Foundry IQ ({citation})" if citation else "Foundry IQ"],
                "is_valid": True,
                "kind": "foundry_iq",
            })
        return facts
