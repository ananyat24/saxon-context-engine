# FoundryIQRetriever tests -- no real Azure AI Search resource. httpx's
# AsyncClient is monkeypatched, same spirit as test_sharepoint_connector.py.
import asyncio

import httpx
import pytest

from app.retrieval.foundry_iq_retriever import FoundryIQRetriever, foundry_iq_configured


def test_foundry_iq_configured_requires_all_three_settings(monkeypatch):
    monkeypatch.setattr("app.retrieval.foundry_iq_retriever.settings.foundry_iq_search_endpoint", "")
    monkeypatch.setattr("app.retrieval.foundry_iq_retriever.settings.foundry_iq_api_key", "")
    monkeypatch.setattr("app.retrieval.foundry_iq_retriever.settings.foundry_iq_knowledge_base", "")
    assert foundry_iq_configured() is False

    monkeypatch.setattr("app.retrieval.foundry_iq_retriever.settings.foundry_iq_search_endpoint", "https://x.search.windows.net")
    monkeypatch.setattr("app.retrieval.foundry_iq_retriever.settings.foundry_iq_api_key", "key")
    assert foundry_iq_configured() is False  # knowledge_base still blank

    monkeypatch.setattr("app.retrieval.foundry_iq_retriever.settings.foundry_iq_knowledge_base", "kb1")
    assert foundry_iq_configured() is True


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text

    def json(self):
        return self._json_data


class _FakeClient:
    def __init__(self, response, captured):
        self._response = response
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, params=None, headers=None, json=None):
        self._captured["url"] = url
        self._captured["params"] = params
        self._captured["headers"] = headers
        self._captured["json"] = json
        return self._response


def _retriever():
    return FoundryIQRetriever(
        search_endpoint="https://contoso.search.windows.net/",
        api_key="admin-key-123",
        knowledge_base="contoso-kb",
    )


def test_retrieve_builds_the_documented_request_shape(monkeypatch):
    captured = {}
    response = _FakeResponse(200, {"references": []})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(response, captured))

    asyncio.run(_retriever().retrieve("who owns the Contoso account?"))

    assert captured["url"] == "https://contoso.search.windows.net/knowledgebases/contoso-kb/retrieve"
    assert captured["params"] == {"api-version": "2026-04-01"}
    assert captured["headers"]["Authorization"] == "Bearer admin-key-123"
    assert captured["json"]["intents"] == [{"type": "semantic", "search": "who owns the Contoso account?"}]


def test_retrieve_converts_references_into_saxon_fact_shape(monkeypatch):
    response = _FakeResponse(200, {
        "references": [
            {"sourceData": "Contoso's account is owned by the Northwind team.", "citationUrl": "https://sharepoint/doc1"},
            {"sourceData": "Renewal is due in Q1.", "citationUrl": None},
        ],
    })
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(response, {}))

    facts = asyncio.run(_retriever().retrieve("who owns the Contoso account?"))

    assert len(facts) == 2
    first = facts[0]
    assert first["fact"] == "Contoso's account is owned by the Northwind team."
    assert first["kind"] == "foundry_iq"
    assert first["is_valid"] is True
    assert first["source_node_uuid"] == ""
    assert first["target_node_uuid"] == ""
    assert first["sources"] == ["Foundry IQ (https://sharepoint/doc1)"]
    assert facts[1]["sources"] == ["Foundry IQ"]  # no citationUrl


def test_retrieve_respects_num_results_cap(monkeypatch):
    response = _FakeResponse(200, {
        "references": [{"sourceData": f"fact {i}"} for i in range(10)],
    })
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(response, {}))

    facts = asyncio.run(_retriever().retrieve("broad question", num_results=3))
    assert len(facts) == 3


def test_retrieve_returns_empty_list_on_http_error_status(monkeypatch):
    response = _FakeResponse(403, text="Forbidden")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(response, {}))

    facts = asyncio.run(_retriever().retrieve("anything"))
    assert facts == []


def test_retrieve_returns_empty_list_when_the_service_is_unreachable(monkeypatch):
    class _RaisingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _RaisingClient())

    facts = asyncio.run(_retriever().retrieve("anything"))
    assert facts == []


def test_retrieve_skips_references_with_no_extractable_text(monkeypatch):
    response = _FakeResponse(200, {"references": [{"citationUrl": "https://x"}, {"sourceData": "real fact"}]})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _FakeClient(response, {}))

    facts = asyncio.run(_retriever().retrieve("q"))
    assert len(facts) == 1
    assert facts[0]["fact"] == "real fact"
