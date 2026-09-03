# Tests app/graph/connector_scheduler.py: no real APScheduler timers fire
# (start_connector_scheduler is only checked for the enabled/disabled
# decision), and _sync_all_connectors's own dependencies (connectors.list_
# connectors, the connector-type dispatch table, run_connector_sync) are all
# monkeypatched, so this never touches Neo4j, Graphiti, or an LLM.
import asyncio
from datetime import datetime, timedelta, timezone

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

    # c2's type isn't in the (patched) dispatch table: skipped, not crashed.
    assert calls == [("t1", "c1")]


def test_sync_all_connectors_also_syncs_dynamically_created_tenants(monkeypatch):
    # A tenant onboarded via the admin API (app/api/admin.py) lives only in
    # Neo4j, not settings.tenant_api_keys: the scheduler has to reach it
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

    # Doesn't raise: c1's failure is logged and swallowed, c2 still runs.
    asyncio.run(connector_scheduler._sync_all_connectors(neo4j_client=None))

    assert calls == ["c1", "c2"]


# --- Push subscription renewal (app/ingestion/graph_subscriptions.py) ------


def test_renew_skips_a_connector_not_close_to_expiring(monkeypatch):
    far_future = datetime.now(timezone.utc) + timedelta(days=2)
    connector = {
        "id": "c1", "tenant_id": "t1", "push_subscription_id": "sub-1",
        "push_client_state": "secret", "push_expires_at": far_future,
    }
    monkeypatch.setattr(
        "app.graph.connectors.list_connectors_with_push_subscriptions", lambda repo=None: [connector]
    )

    async def _fail_if_called(subscription_id):
        raise AssertionError("renew_subscription should not have been called")

    monkeypatch.setattr("app.ingestion.graph_subscriptions.renew_subscription", _fail_if_called)

    asyncio.run(connector_scheduler._renew_expiring_push_subscriptions(neo4j_client=None))


def test_renew_extends_a_connector_close_to_expiring(monkeypatch):
    from app.ingestion.graph_subscriptions import RENEW_WHEN_WITHIN

    soon = datetime.now(timezone.utc) + (RENEW_WHEN_WITHIN - timedelta(hours=1))
    connector = {
        "id": "c1", "tenant_id": "t1", "push_subscription_id": "sub-1",
        "push_client_state": "secret", "push_expires_at": soon,
    }
    monkeypatch.setattr(
        "app.graph.connectors.list_connectors_with_push_subscriptions", lambda repo=None: [connector]
    )

    new_expiry = datetime.now(timezone.utc) + timedelta(days=3)

    async def fake_renew(subscription_id):
        assert subscription_id == "sub-1"
        return new_expiry

    monkeypatch.setattr("app.ingestion.graph_subscriptions.renew_subscription", fake_renew)

    set_calls = []
    monkeypatch.setattr(
        "app.graph.connectors.set_push_subscription",
        lambda tenant_id, connector_id, *, subscription_id, client_state, expires_at, repo=None: set_calls.append(
            (tenant_id, connector_id, subscription_id, expires_at)
        ),
    )

    asyncio.run(connector_scheduler._renew_expiring_push_subscriptions(neo4j_client=None))

    assert set_calls == [("t1", "c1", "sub-1", new_expiry)]


# --- Multi-replica lock (app/graph/scheduler_lock.py) ----------------------


def test_tick_skips_real_work_when_the_lock_is_not_acquired(monkeypatch):
    monkeypatch.setattr("app.graph.connector_scheduler.try_acquire_lock", lambda *a, **kw: False)

    async def _fail_if_called(neo4j_client):
        raise AssertionError("should not have run -- another replica holds the lock")

    monkeypatch.setattr(connector_scheduler, "_sync_all_connectors", _fail_if_called)
    monkeypatch.setattr(connector_scheduler, "_renew_expiring_push_subscriptions", _fail_if_called)

    asyncio.run(connector_scheduler._tick(neo4j_client=None))


def test_tick_runs_real_work_when_the_lock_is_acquired(monkeypatch):
    monkeypatch.setattr("app.graph.connector_scheduler.try_acquire_lock", lambda *a, **kw: True)

    calls = []
    async def fake_sync(neo4j_client):
        calls.append("sync")
    async def fake_renew(neo4j_client):
        calls.append("renew")

    monkeypatch.setattr(connector_scheduler, "_sync_all_connectors", fake_sync)
    monkeypatch.setattr(connector_scheduler, "_renew_expiring_push_subscriptions", fake_renew)

    asyncio.run(connector_scheduler._tick(neo4j_client=None))

    assert calls == ["sync", "renew"]


def test_renew_clears_the_subscription_when_graph_rejects_the_renewal(monkeypatch):
    from app.ingestion.connector_base import ConnectorFetchError
    from app.ingestion.graph_subscriptions import RENEW_WHEN_WITHIN

    soon = datetime.now(timezone.utc) + (RENEW_WHEN_WITHIN - timedelta(hours=1))
    connector = {
        "id": "c1", "tenant_id": "t1", "push_subscription_id": "sub-1",
        "push_client_state": "secret", "push_expires_at": soon,
    }
    monkeypatch.setattr(
        "app.graph.connectors.list_connectors_with_push_subscriptions", lambda repo=None: [connector]
    )

    async def fake_renew(subscription_id):
        raise ConnectorFetchError("Microsoft Graph refused the renewal (HTTP 404).")

    monkeypatch.setattr("app.ingestion.graph_subscriptions.renew_subscription", fake_renew)

    cleared = []
    monkeypatch.setattr(
        "app.graph.connectors.clear_push_subscription",
        lambda tenant_id, connector_id, repo=None: cleared.append((tenant_id, connector_id)),
    )

    asyncio.run(connector_scheduler._renew_expiring_push_subscriptions(neo4j_client=None))

    assert cleared == [("t1", "c1")]
