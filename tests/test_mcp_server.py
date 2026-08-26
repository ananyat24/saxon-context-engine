# Tests app/mcp/server.py's own auth boundary and HTTPException->ToolError
# translation -- no live MCP transport, no network. See tests/test_connector_sync.py
# etc. for the equivalent HTTP-route-level pattern this mirrors for the MCP surface.
import asyncio

import pytest
from fastapi import HTTPException, status
from mcp.server.mcpserver.exceptions import ToolError

from app.config import KnowledgeBase, TenantConfig, settings
from app.mcp import server as mcp_server_module


class _FakeContext:
    """Stands in for mcp.server.mcpserver.Context -- _authenticate only ever
    reads .headers off it, so a minimal stub is enough."""

    def __init__(self, headers: dict | None):
        self.headers = headers


def _fake_tenant() -> TenantConfig:
    return TenantConfig(
        tenant_id="test_tenant",
        gemini_api_key="fake-key",
        knowledge_bases=[KnowledgeBase(id="kb1", label="KB One")],
    )


def test_authenticate_missing_header_raises_tool_error():
    with pytest.raises(ToolError, match="Missing X-API-Key"):
        mcp_server_module._authenticate(_FakeContext(headers={}))


def test_authenticate_no_headers_at_all_raises_tool_error():
    with pytest.raises(ToolError, match="Missing X-API-Key"):
        mcp_server_module._authenticate(_FakeContext(headers=None))


def test_authenticate_unknown_key_raises_tool_error():
    with pytest.raises(ToolError, match="Invalid X-API-Key"):
        mcp_server_module._authenticate(_FakeContext(headers={"x-api-key": "not-a-real-key"}))


def test_authenticate_valid_key_returns_the_matching_tenant(monkeypatch):
    tenant = _fake_tenant()
    monkeypatch.setattr(settings, "tenant_api_keys", {"real-key": tenant})
    result = mcp_server_module._authenticate(_FakeContext(headers={"x-api-key": "real-key"}))
    assert result is tenant


def test_query_context_graph_translates_http_exception_into_tool_error(monkeypatch):
    tenant = _fake_tenant()
    monkeypatch.setattr(settings, "tenant_api_keys", {"real-key": tenant})
    mcp_server_module.configure(neo4j_client=object(), graphiti_pool=object())

    async def _raise_unknown_scope(**kwargs):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown document set 'bogus' for this tenant.")

    monkeypatch.setattr(mcp_server_module, "execute_context_query", _raise_unknown_scope)

    with pytest.raises(ToolError, match="Unknown document set 'bogus'"):
        asyncio.run(
            mcp_server_module.query_context_graph(
                query="anything", ctx=_FakeContext(headers={"x-api-key": "real-key"}), document_set="bogus"
            )
        )
