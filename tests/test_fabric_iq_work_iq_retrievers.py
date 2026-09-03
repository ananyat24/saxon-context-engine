# FabricIQOntologyRetriever and WorkIQRetriever: refresh_access_token and
# query_mcp_tool are both monkeypatched (no real Microsoft tenant, no real
# MCP server); this covers each retriever's own fact-shaping and its
# graceful-degradation paths (refresh failure, empty MCP result).
import asyncio

import pytest

from app.retrieval import fabric_iq_ontology_retriever, work_iq_retriever
from app.retrieval.fabric_iq_ontology_retriever import FabricIQOntologyRetriever
from app.retrieval.work_iq_retriever import WorkIQRetriever


def _retriever():
    return FabricIQOntologyRetriever(
        tenant_id="tenant-123", workspace_id="ws-1", ontology_id="ont-1",
        refresh_token="rt", scope="McpServers.FabricIQOntology.All",
    )


def test_fabric_iq_ontology_retriever_builds_the_documented_endpoint():
    r = _retriever()
    assert r.url == (
        "https://agent365.svc.cloud.microsoft/agents/tenants/tenant-123/"
        "servers/mcp_FabricIQOntology/workspaces/ws-1/ontologies/ont-1"
    )


def test_fabric_iq_ontology_retriever_shapes_a_real_result_into_a_saxon_fact(monkeypatch):
    async def fake_refresh(refresh_token, scope):
        assert refresh_token == "rt"
        return "access-token-abc"

    async def fake_query(url, access_token, tool_name, query_text):
        assert access_token == "access-token-abc"
        assert tool_name == "search_ontology"
        return "Contoso places orders through the Northwind team."

    monkeypatch.setattr(fabric_iq_ontology_retriever, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(fabric_iq_ontology_retriever, "query_mcp_tool", fake_query)

    facts = asyncio.run(_retriever().retrieve("who owns the Contoso account?"))

    assert len(facts) == 1
    assert facts[0]["fact"] == "Contoso places orders through the Northwind team."
    assert facts[0]["kind"] == "fabric_iq_ontology"
    assert facts[0]["is_valid"] is True
    assert facts[0]["sources"] == ["Fabric IQ Ontology"]


def test_fabric_iq_ontology_retriever_returns_empty_list_when_refresh_fails(monkeypatch):
    async def fake_refresh(refresh_token, scope):
        raise Exception("refresh token revoked")

    monkeypatch.setattr(fabric_iq_ontology_retriever, "refresh_access_token", fake_refresh)

    facts = asyncio.run(_retriever().retrieve("anything"))
    assert facts == []


def test_fabric_iq_ontology_retriever_returns_empty_list_when_mcp_query_finds_nothing(monkeypatch):
    async def fake_refresh(refresh_token, scope):
        return "access-token-abc"

    async def fake_query(url, access_token, tool_name, query_text):
        return None

    monkeypatch.setattr(fabric_iq_ontology_retriever, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(fabric_iq_ontology_retriever, "query_mcp_tool", fake_query)

    facts = asyncio.run(_retriever().retrieve("anything"))
    assert facts == []


def test_work_iq_retriever_uses_the_universal_endpoint_and_ask_tool(monkeypatch):
    async def fake_refresh(refresh_token, scope):
        assert refresh_token == "rt2"
        return "access-token-xyz"

    async def fake_query(url, access_token, tool_name, query_text):
        assert url == "https://workiq.svc.cloud.microsoft/mcp"
        assert tool_name == "ask"
        assert query_text == "what deals closed this quarter?"
        return "Three deals closed: Contoso, Fabrikam, and Northwind Traders."

    monkeypatch.setattr(work_iq_retriever, "refresh_access_token", fake_refresh)
    monkeypatch.setattr(work_iq_retriever, "query_mcp_tool", fake_query)

    facts = asyncio.run(WorkIQRetriever(refresh_token="rt2", scope="").retrieve("what deals closed this quarter?"))

    assert len(facts) == 1
    assert facts[0]["kind"] == "work_iq"
    assert facts[0]["sources"] == ["Work IQ"]
