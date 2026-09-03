# Tests app/ingestion/connector_sync.py's run_connector_sync(): the logic
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


def test_successful_sync_invalidates_the_response_cache_for_its_group(monkeypatch):
    _no_op_record_sync_result(monkeypatch)

    async def collecting(**kwargs):
        pass

    _patch_ingestion(monkeypatch, collecting)

    invalidated = []
    monkeypatch.setattr(
        "app.ingestion.connector_sync.get_response_cache",
        lambda: type("_Fake", (), {"invalidate_group": staticmethod(lambda t, g: invalidated.append((t, g)))})(),
    )

    tenant = _tenant()
    connector = {"id": "c1", "group_id": "kb1", "content_hash": None}
    records = [SourceRecord(name="a", body="x", source_description="d")]

    asyncio.run(run_connector_sync(tenant, connector, lambda c: _StaticConnector(records, hash_value="new-hash"), repo=None))

    assert invalidated == [("t1", "kb1")]


class _FakeEpisode:
    def __init__(self, uuid):
        self.uuid = uuid


class _FakeIngestResult:
    def __init__(self, uuid):
        self.episode = _FakeEpisode(uuid)


class _RecordingRepo:
    """Only implements execute_cypher: enough to prove which episodes get
    tagged with which connector_id, without needing a real Neo4j."""

    def __init__(self):
        self.calls = []

    def execute_cypher(self, query, params=None):
        self.calls.append((query, params))
        return []


def test_successful_sync_tags_each_episode_with_the_connector_id(monkeypatch):
    # The real bug this pins: app/api/graph.py's connector preview filters
    # entities/facts by which connector's episode produced them (see that
    # module's ?connector_id= handling), so every episode a sync creates
    # has to actually get tagged, or the preview silently shows nothing for
    # a connector that really did sync data.
    _no_op_record_sync_result(monkeypatch)

    async def fake_ingest(**kwargs):
        return _FakeIngestResult(uuid=f"episode-for-{kwargs['name']}")

    _patch_ingestion(monkeypatch, fake_ingest)
    monkeypatch.setattr(
        "app.ingestion.connector_sync.get_response_cache",
        lambda: type("_Fake", (), {"invalidate_group": staticmethod(lambda t, g: None)})(),
    )

    tenant = _tenant()
    connector = {"id": "connector-42", "group_id": "kb1", "content_hash": None}
    records = [
        SourceRecord(name="rec-a", body="x", source_description="d"),
        SourceRecord(name="rec-b", body="y", source_description="d"),
    ]
    repo = _RecordingRepo()

    result = asyncio.run(
        run_connector_sync(tenant, connector, lambda c: _StaticConnector(records, hash_value="new-hash"), repo=repo)
    )

    assert result["synced"] is True
    tag_calls = [(q, p) for q, p in repo.calls if "SET e.connector_id" in q]
    assert len(tag_calls) == 2
    assert {p["uuid"] for _, p in tag_calls} == {"episode-for-rec-a", "episode-for-rec-b"}
    assert all(p["connector_id"] == "connector-42" for _, p in tag_calls)


def test_tagging_retries_and_recovers_from_a_transient_failure(monkeypatch):
    # Pins the actual production incident this retry was added for: a
    # transient Neo4j blip (a few seconds of ServiceUnavailable) hit the
    # tag-write specifically, and because content_hash still gets recorded
    # regardless (the episode really was ingested), an unchanged future
    # sync would skip re-ingestion forever and never get another chance to
    # tag it: a permanent gap from a momentary blip. The retry has to
    # actually succeed on a later attempt, not just not-crash.
    _no_op_record_sync_result(monkeypatch)

    async def _noop_sleep(*a):
        return None

    monkeypatch.setattr("app.ingestion.connector_sync.asyncio.sleep", _noop_sleep)

    async def fake_ingest(**kwargs):
        return _FakeIngestResult(uuid="episode-flaky")

    _patch_ingestion(monkeypatch, fake_ingest)
    monkeypatch.setattr(
        "app.ingestion.connector_sync.get_response_cache",
        lambda: type("_Fake", (), {"invalidate_group": staticmethod(lambda t, g: None)})(),
    )

    class _FlakyThenRecoveringRepo:
        def __init__(self):
            self.attempts = 0
            self.succeeded = False

        def execute_cypher(self, query, params=None):
            if "SET e.connector_id" in query:
                self.attempts += 1
                if self.attempts < 3:
                    raise RuntimeError("transient Neo4j blip")
                self.succeeded = True
            return []

    repo = _FlakyThenRecoveringRepo()
    tenant = _tenant()
    connector = {"id": "c1", "group_id": "kb1", "content_hash": None}
    records = [SourceRecord(name="a", body="x", source_description="d")]

    result = asyncio.run(
        run_connector_sync(tenant, connector, lambda c: _StaticConnector(records, hash_value="new-hash"), repo=repo)
    )

    assert result["synced"] is True
    assert repo.attempts == 3
    assert repo.succeeded is True


def test_a_tagging_failure_does_not_fail_an_otherwise_successful_sync(monkeypatch):
    _no_op_record_sync_result(monkeypatch)

    async def fake_ingest(**kwargs):
        return _FakeIngestResult(uuid="episode-1")

    _patch_ingestion(monkeypatch, fake_ingest)
    monkeypatch.setattr(
        "app.ingestion.connector_sync.get_response_cache",
        lambda: type("_Fake", (), {"invalidate_group": staticmethod(lambda t, g: None)})(),
    )

    class _BrokenRepo:
        def execute_cypher(self, query, params=None):
            raise RuntimeError("Neo4j hiccup")

    tenant = _tenant()
    connector = {"id": "c1", "group_id": "kb1", "content_hash": None}
    records = [SourceRecord(name="a", body="x", source_description="d")]

    result = asyncio.run(
        run_connector_sync(tenant, connector, lambda c: _StaticConnector(records, hash_value="new-hash"), repo=_BrokenRepo())
    )

    assert result == {"synced": True, "skipped_unchanged": False, "error": None, "spend_limit_exceeded": False}


def test_unchanged_sync_does_not_touch_the_response_cache(monkeypatch):
    _no_op_record_sync_result(monkeypatch)

    invalidated = []
    monkeypatch.setattr(
        "app.ingestion.connector_sync.get_response_cache",
        lambda: type("_Fake", (), {"invalidate_group": staticmethod(lambda t, g: invalidated.append((t, g)))})(),
    )

    tenant = _tenant()
    connector = {"id": "c1", "group_id": "kb1", "content_hash": "same-hash"}
    records = [SourceRecord(name="a", body="x", source_description="d")]

    asyncio.run(run_connector_sync(tenant, connector, lambda c: _StaticConnector(records), repo=None))

    assert invalidated == []
