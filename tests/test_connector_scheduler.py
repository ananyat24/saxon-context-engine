# Tests app/graph/connector_scheduler.py -- no real APScheduler timers fire
# (start_connector_scheduler is only checked for the enabled/disabled
# decision), and _sync_all_connectors's own dependencies (connectors.list_
# connectors, the connector-type dispatch table, run_connector_sync) are all
# monkeypatched, so this never touches Neo4j, Graphiti, or an LLM.
import asyncio

from app.config import KnowledgeBase, TenantConfig, settings
from app.graph import connector_scheduler


def test_start_connector_scheduler_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "connector_sync_enabled", False)
    assert connector_scheduler.start_connector_scheduler(neo4j_client=None) is None


def test_start_connector_scheduler_starts_when_enabled(monkeypatch):
    # AsyncIOScheduler.start() requires a running event loop (it's meant to
    # be called from within the app's own lifespan, not plain sync code).
    monkeypatch.setattr(settings, "connector_sync_enabled", True)
    monkeypatch.setattr(settings, "connector_sync_interval_minutes", 15)

    async def _run():
        scheduler = connector_scheduler.start_connector_scheduler(neo4j_client=None)
        try:
            assert scheduler is not None
            assert scheduler.get_job(connector_scheduler._JOB_ID) is not None
        finally:
            scheduler.shutdown(wait=False)

    asyncio.run(_run())


def test_sync_all_connectors_calls_run_connector_sync_for_every_tenant_connector(monkeypatch):
    tenant = TenantConfig(tenant_id="t1", gemini_api_key="fake", knowledge_bases=[KnowledgeBase(id="kb1", label="KB")])
    monkeypatch.setattr(settings, "tenant_api_keys", {"key": tenant})
    monkeypatch.setattr("app.graph.tenants.list_tenant_configs", lambda repo=None: [])

    fake_connectors = [
        {"id": "c1", "type": "web", "group_id": "kb1", "content_hash": None},
        {"id": "c2", "type": "unknown_type", "group_id": "kb1", "content_hash": None},
    ]
    monkeypatch.setattr(
        "app.graph.connector_scheduler.connectors.list_connectors",
        lambda tenant_id, repo=None: fake_connectors,
    )
    monkeypatch.setattr("app.api.connectors._CONNECTOR_FACTORIES", {"web": lambda c: object()})

    calls = []

    async def fake_run(tenant_arg, connector_arg, factory_arg, *, repo):
        calls.append((tenant_arg.tenant_id, connector_arg["id"]))
        return {"synced": True, "skipped_unchanged": False, "error": None, "spend_limit_exceeded": False}

    monkeypatch.setattr("app.ingestion.connector_sync.run_connector_sync", fake_run)

    asyncio.run(connector_scheduler._sync_all_connectors(neo4j_client=None))

    # c2's type isn't in the (patched) dispatch table -- skipped, not crashed.
    assert calls == [("t1", "c1")]


def test_sync_all_connectors_also_syncs_dynamically_created_tenants(monkeypatch):
    # A tenant onboarded via the admin API (app/api/admin.py) lives only in
    # Neo4j, not settings.tenant_api_keys -- the scheduler has to reach it
    # too, or "add a connector" for that tenant would silently never
    # auto-sync in the background.
    monkeypatch.setattr(settings, "tenant_api_keys", {})
    dynamic_tenant = TenantConfig(
        tenant_id="dynamic1", gemini_api_key="fake", knowledge_bases=[KnowledgeBase(id="kb1", label="KB")]
    )
    monkeypatch.setattr(
        "app.graph.tenants.list_tenant_configs", lambda repo=None: [dynamic_tenant]
    )

    fake_connectors = [{"id": "c1", "type": "web", "group_id": "kb1", "content_hash": None}]
    monkeypatch.setattr(
        "app.graph.connector_scheduler.connectors.list_connectors",
        lambda tenant_id, repo=None: fake_connectors,
    )
    monkeypatch.setattr("app.api.connectors._CONNECTOR_FACTORIES", {"web": lambda c: object()})

    calls = []

    async def fake_run(tenant_arg, connector_arg, factory_arg, *, repo):
        calls.append((tenant_arg.tenant_id, connector_arg["id"]))
        return {"synced": True, "skipped_unchanged": False, "error": None, "spend_limit_exceeded": False}

    monkeypatch.setattr("app.ingestion.connector_sync.run_connector_sync", fake_run)

    asyncio.run(connector_scheduler._sync_all_connectors(neo4j_client=None))

    assert calls == [("dynamic1", "c1")]


def test_sync_all_connectors_survives_one_connector_raising(monkeypatch):
    tenant = TenantConfig(tenant_id="t1", gemini_api_key="fake", knowledge_bases=[KnowledgeBase(id="kb1", label="KB")])
    monkeypatch.setattr(settings, "tenant_api_keys", {"key": tenant})
    monkeypatch.setattr("app.graph.tenants.list_tenant_configs", lambda repo=None: [])

    fake_connectors = [
        {"id": "c1", "type": "web", "group_id": "kb1", "content_hash": None},
        {"id": "c2", "type": "web", "group_id": "kb1", "content_hash": None},
    ]
    monkeypatch.setattr(
        "app.graph.connector_scheduler.connectors.list_connectors",
        lambda tenant_id, repo=None: fake_connectors,
    )
    monkeypatch.setattr("app.api.connectors._CONNECTOR_FACTORIES", {"web": lambda c: object()})

    calls = []

    async def fake_run(tenant_arg, connector_arg, factory_arg, *, repo):
        calls.append(connector_arg["id"])
        if connector_arg["id"] == "c1":
            raise RuntimeError("boom")
        return {"synced": True, "skipped_unchanged": False, "error": None, "spend_limit_exceeded": False}

    monkeypatch.setattr("app.ingestion.connector_sync.run_connector_sync", fake_run)

    # Doesn't raise -- c1's failure is logged and swallowed, c2 still runs.
    asyncio.run(connector_scheduler._sync_all_connectors(neo4j_client=None))

    assert calls == ["c1", "c2"]
