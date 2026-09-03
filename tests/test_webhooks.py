# app/api/webhooks.py's own logic: app.state.ingestion_queue and the
# connector/tenant lookups it depends on are all monkeypatched or stubbed,
# so this never touches Neo4j or a real ingestion queue. No FastAPI
# TestClient/real HTTP: _handle_one (the per-notification logic) and the
# route function's validation-handshake branch are each exercised directly,
# same "call the module's own functions" convention as the rest of this
# test suite.
import asyncio

from app.api import webhooks
from app.graph.graph_repository import GraphRepository


class _FakeQueryParams(dict):
    pass


class _FakeRequest:
    def __init__(self, *, query_params=None, json_body=None, app_state=None):
        self.query_params = _FakeQueryParams(query_params or {})
        self._json_body = json_body
        self.app = _FakeApp(app_state or {})

    async def json(self):
        return self._json_body


class _FakeAppState:
    def __init__(self, values: dict):
        for k, v in values.items():
            setattr(self, k, v)


class _FakeApp:
    def __init__(self, values: dict):
        self.state = _FakeAppState(values)


class _FakeQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue(self, job):
        self.jobs.append(job)


def test_validation_handshake_echoes_the_token_back():
    request = _FakeRequest(query_params={"validationToken": "abc123"})
    resp = asyncio.run(webhooks.graph_webhook(request))
    assert resp.status_code == 200
    assert resp.body == b"abc123"
    assert resp.media_type == "text/plain"


def test_malformed_body_still_acks(monkeypatch):
    request = _FakeRequest(app_state={"neo4j_client": None})

    async def _raise_json():
        raise ValueError("not json")

    request.json = _raise_json
    resp = asyncio.run(webhooks.graph_webhook(request))
    assert resp.status_code == 202


def test_handle_one_ignores_a_notification_for_an_unknown_subscription(monkeypatch):
    monkeypatch.setattr(
        "app.graph.connectors.get_connector_by_subscription_id", lambda sub_id, repo=None: None
    )
    queue = _FakeQueue()
    request = _FakeRequest(app_state={"neo4j_client": None, "ingestion_queue": queue})

    asyncio.run(webhooks._handle_one({"subscriptionId": "sub-unknown"}, request, GraphRepository()))

    assert queue.jobs == []


def test_handle_one_ignores_a_client_state_mismatch(monkeypatch):
    connector = {
        "id": "c1", "tenant_id": "t1", "type": "outlook_mail", "group_id": "kb1",
        "push_client_state": "the-real-secret",
    }
    monkeypatch.setattr(
        "app.graph.connectors.get_connector_by_subscription_id", lambda sub_id, repo=None: connector
    )
    queue = _FakeQueue()
    request = _FakeRequest(app_state={"neo4j_client": None, "ingestion_queue": queue})

    asyncio.run(
        webhooks._handle_one(
            {"subscriptionId": "sub-1", "clientState": "an-attackers-guess"}, request, GraphRepository()
        )
    )

    assert queue.jobs == []


def test_handle_one_enqueues_a_sync_for_a_matching_notification(monkeypatch):
    from app.config import KnowledgeBase, TenantConfig

    connector = {
        "id": "c1", "tenant_id": "t1", "type": "outlook_mail", "group_id": "kb1",
        "push_client_state": "the-real-secret",
    }
    tenant = TenantConfig(tenant_id="t1", gemini_api_key="fake", knowledge_bases=[KnowledgeBase(id="kb1", label="KB")])
    monkeypatch.setattr(
        "app.graph.connectors.get_connector_by_subscription_id", lambda sub_id, repo=None: connector
    )
    monkeypatch.setattr("app.graph.connectors.mark_sync_queued", lambda tenant_id, connector_id, repo=None: None)
    monkeypatch.setattr("app.graph.tenants.find_tenant_by_tenant_id", lambda tenant_id, repo=None: tenant)
    monkeypatch.setattr("app.api.connectors._CONNECTOR_FACTORIES", {"outlook_mail": lambda c: object()})

    queue = _FakeQueue()
    request = _FakeRequest(app_state={"neo4j_client": None, "ingestion_queue": queue})

    asyncio.run(
        webhooks._handle_one(
            {"subscriptionId": "sub-1", "clientState": "the-real-secret"}, request, GraphRepository()
        )
    )

    assert len(queue.jobs) == 1


def test_handle_one_skips_when_tenant_no_longer_exists(monkeypatch):
    connector = {
        "id": "c1", "tenant_id": "t1", "type": "outlook_mail", "group_id": "kb1",
        "push_client_state": "the-real-secret",
    }
    monkeypatch.setattr(
        "app.graph.connectors.get_connector_by_subscription_id", lambda sub_id, repo=None: connector
    )
    monkeypatch.setattr("app.graph.tenants.find_tenant_by_tenant_id", lambda tenant_id, repo=None: None)

    queue = _FakeQueue()
    request = _FakeRequest(app_state={"neo4j_client": None, "ingestion_queue": queue})

    asyncio.run(
        webhooks._handle_one(
            {"subscriptionId": "sub-1", "clientState": "the-real-secret"}, request, GraphRepository()
        )
    )

    assert queue.jobs == []
