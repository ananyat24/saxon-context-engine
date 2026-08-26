# Tests app/ingestion/connector_sync.py's run_connector_sync() -- the logic
# shared by the manual "Sync now" route and the background scheduler. No
# real Neo4j, Graphiti, or LLM calls: record_sync_result, build_graphiti,
# and IngestionPipeline are all monkeypatched, so these run free and fast.
import asyncio

import pytest

from app.config import KnowledgeBase, TenantConfig
from app.graph.spend_limiter import SpendLimitExceeded
from app.ingestion.connector_base import ConnectorFetchError, SourceConnector
from app.ingestion.connector_sync import run_connector_sync
from app.ingestion.file_source import SourceRecord


def _tenant() -> TenantConfig:
    return TenantConfig(tenant_id="t1", gemini_api_key="fake", knowledge_bases=[KnowledgeBase(id="kb1", label="KB")])


class _FailingConnector(SourceConnector):
    async def fetch(self):
        raise ConnectorFetchError("boom")

    def content_hash(self, records):
        return ""

    def source_description(self):
        return "failing"


class _StaticConnector(SourceConnector):
    def __init__(self, records, hash_value="same-hash"):
        self._records = records
        self._hash_value = hash_value

    async def fetch(self):
        return self._records

    def content_hash(self, records):
        return self._hash_value

    def source_description(self):
        return "static"


def _no_op_record_sync_result(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.ingestion.connector_sync.connectors.record_sync_result",
        lambda tenant_id, connector_id, *, status, last_error=None, content_hash=None, repo=None: calls.append(status),
    )
    return calls


def test_fetch_failure_is_reported_and_recorded(monkeypatch):
    statuses = _no_op_record_sync_result(monkeypatch)
    tenant = _tenant()
    connector = {"id": "c1", "group_id": "kb1", "content_hash": None}

    result = asyncio.run(run_connector_sync(tenant, connector, lambda c: _FailingConnector(), repo=None))

    assert result == {"synced": False, "skipped_unchanged": False, "error": "boom", "spend_limit_exceeded": False}
    assert statuses == ["error"]


def test_unchanged_content_is_skipped_without_ingesting(monkeypatch):
    statuses = _no_op_record_sync_result(monkeypatch)
    tenant = _tenant()
    connector = {"id": "c1", "group_id": "kb1", "content_hash": "same-hash"}
    records = [SourceRecord(name="a", body="x", source_description="d")]

    result = asyncio.run(run_connector_sync(tenant, connector, lambda c: _StaticConnector(records), repo=None))

    assert result == {"synced": False, "skipped_unchanged": True, "error": None, "spend_limit_exceeded": False}
    assert statuses == ["unchanged"]


class _FakeGraphiti:
    async def close(self):
        pass


def _patch_ingestion(monkeypatch, ingest_episode):
    monkeypatch.setattr("app.ingestion.connector_sync.build_graphiti", lambda **kwargs: _FakeGraphiti())

    class _FakePipeline:
        def __init__(self, *args, **kwargs):
            pass

        async def ingest_episode(self, **kwargs):
            return await ingest_episode(**kwargs)

    monkeypatch.setattr("app.ingestion.connector_sync.IngestionPipeline", _FakePipeline)


def test_spend_limit_exceeded_is_reported_distinctly(monkeypatch):
    statuses = _no_op_record_sync_result(monkeypatch)

    async def raising(**kwargs):
        raise SpendLimitExceeded("over budget")

    _patch_ingestion(monkeypatch, raising)
    tenant = _tenant()
    connector = {"id": "c1", "group_id": "kb1", "content_hash": None}
    records = [SourceRecord(name="a", body="x", source_description="d")]

    result = asyncio.run(run_connector_sync(tenant, connector, lambda c: _StaticConnector(records), repo=None))

    assert result["synced"] is False
    assert result["spend_limit_exceeded"] is True
    assert result["error"] == "over budget"
    assert statuses == ["error"]


def test_unexpected_ingest_error_is_reported_but_not_as_spend_limit(monkeypatch):
    statuses = _no_op_record_sync_result(monkeypatch)

    async def raising(**kwargs):
        raise RuntimeError("weird failure")

    _patch_ingestion(monkeypatch, raising)
    tenant = _tenant()
    connector = {"id": "c1", "group_id": "kb1", "content_hash": None}
    records = [SourceRecord(name="a", body="x", source_description="d")]

    result = asyncio.run(run_connector_sync(tenant, connector, lambda c: _StaticConnector(records), repo=None))

    assert result["error"] == "weird failure"
    assert result["spend_limit_exceeded"] is False
    assert statuses == ["error"]


def test_successful_sync_ingests_every_record_and_records_the_new_hash(monkeypatch):
    statuses = _no_op_record_sync_result(monkeypatch)
    ingested = []

    async def collecting(**kwargs):
        ingested.append(kwargs)

    _patch_ingestion(monkeypatch, collecting)
    tenant = _tenant()
    connector = {"id": "c1", "group_id": "kb1", "content_hash": None}
    records = [
        SourceRecord(name="a", body="x", source_description="d"),
        SourceRecord(name="b", body="y", source_description="d"),
    ]

    result = asyncio.run(
        run_connector_sync(tenant, connector, lambda c: _StaticConnector(records, hash_value="new-hash"), repo=None)
    )

    assert result == {"synced": True, "skipped_unchanged": False, "error": None, "spend_limit_exceeded": False}
    assert statuses == ["synced"]
    assert len(ingested) == 2
    assert {call["group_id"] for call in ingested} == {"kb1"}
