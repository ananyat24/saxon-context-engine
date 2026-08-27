# app/graph/connectors.py's push-subscription storage (set/clear/lookup) --
# needs a real, reachable Neo4j, same caveat as test_entity_reconciliation.py.
# Creates and cleans up its own throwaway :Connector node under a randomly-
# suffixed tenant_id, so this never touches a real connector.
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.graph import connectors
from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    return GraphRepository()


@pytest.fixture
def connector(repo):
    tenant_id = f"test_push_{uuid.uuid4().hex[:8]}"
    created = connectors.create_connector(
        tenant_id, "Test Outlook", "outlook_mail", "kb1", "alerts@contoso.com", repo=repo
    )
    yield tenant_id, created["id"]
    connectors.delete_connector(tenant_id, created["id"], repo=repo)


def test_new_connector_has_no_push_subscription(repo, connector):
    tenant_id, connector_id = connector
    fetched = connectors.get_connector(tenant_id, connector_id, repo=repo)
    assert fetched["push_subscription_id"] is None
    assert fetched.get("push_client_state") is None
    assert fetched.get("push_expires_at") is None


def test_set_then_get_push_subscription(repo, connector):
    tenant_id, connector_id = connector
    expires_at = datetime.now(timezone.utc) + timedelta(days=2)

    connectors.set_push_subscription(
        tenant_id, connector_id, subscription_id="sub-abc", client_state="secret-123",
        expires_at=expires_at, repo=repo,
    )

    fetched = connectors.get_connector(tenant_id, connector_id, repo=repo)
    assert fetched["push_subscription_id"] == "sub-abc"
    assert fetched["push_client_state"] == "secret-123"
    assert fetched["push_expires_at"] is not None


def test_get_connector_by_subscription_id_finds_it_across_tenants(repo, connector):
    tenant_id, connector_id = connector
    expires_at = datetime.now(timezone.utc) + timedelta(days=2)
    connectors.set_push_subscription(
        tenant_id, connector_id, subscription_id="sub-lookup-me", client_state="secret",
        expires_at=expires_at, repo=repo,
    )

    found = connectors.get_connector_by_subscription_id("sub-lookup-me", repo=repo)
    assert found is not None
    assert found["id"] == connector_id
    assert found["tenant_id"] == tenant_id


def test_get_connector_by_subscription_id_returns_none_for_unknown(repo):
    assert connectors.get_connector_by_subscription_id("no-such-subscription", repo=repo) is None


def test_clear_push_subscription_removes_it(repo, connector):
    tenant_id, connector_id = connector
    connectors.set_push_subscription(
        tenant_id, connector_id, subscription_id="sub-to-clear", client_state="secret",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1), repo=repo,
    )

    connectors.clear_push_subscription(tenant_id, connector_id, repo=repo)

    fetched = connectors.get_connector(tenant_id, connector_id, repo=repo)
    assert fetched["push_subscription_id"] is None
    assert connectors.get_connector_by_subscription_id("sub-to-clear", repo=repo) is None


def test_list_connectors_with_push_subscriptions_only_includes_ones_with_a_subscription(repo, connector):
    tenant_id, connector_id = connector
    # No subscription yet -- shouldn't show up.
    ids_before = {c["id"] for c in connectors.list_connectors_with_push_subscriptions(repo=repo)}
    assert connector_id not in ids_before

    connectors.set_push_subscription(
        tenant_id, connector_id, subscription_id="sub-listed", client_state="secret",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1), repo=repo,
    )

    ids_after = {c["id"] for c in connectors.list_connectors_with_push_subscriptions(repo=repo)}
    assert connector_id in ids_after
