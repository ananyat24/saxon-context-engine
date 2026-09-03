# app/graph/connectors.py's fabric_iq_ontology/work_iq storage
# (create_fabric_iq_ontology_connector, create_work_iq_connector,
# find_microsoft_iq_config_for_group, get_microsoft_iq_credential) -- needs
# a real, reachable Neo4j, same pattern as test_foundry_iq_connector_storage.py.
import uuid

import pytest

from app.graph import connectors
from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    return GraphRepository()


def _ids():
    return f"test_tenant_{uuid.uuid4().hex[:8]}", f"test_group_{uuid.uuid4().hex[:8]}"


def test_create_fabric_iq_ontology_connector_round_trips_by_id(repo):
    tenant_id, group_id = _ids()
    created = connectors.create_fabric_iq_ontology_connector(
        tenant_id, "My Ontology", group_id, "ws-1", "ont-1", "enc-refresh-token", repo=repo,
    )
    assert created["type"] == "fabric_iq_ontology"
    assert created["fabric_iq_workspace_id"] == "ws-1"

    credential = connectors.get_microsoft_iq_credential(tenant_id, created["id"], "fabric_iq_ontology", repo=repo)
    assert credential == {
        "oauth_refresh_token_enc": "enc-refresh-token",
        "fabric_iq_workspace_id": "ws-1",
        "fabric_iq_ontology_id": "ont-1",
    }


def test_create_work_iq_connector_round_trips_by_id(repo):
    tenant_id, group_id = _ids()
    created = connectors.create_work_iq_connector(tenant_id, "My Work IQ", group_id, "enc-refresh-token-2", repo=repo)
    assert created["type"] == "work_iq"

    credential = connectors.get_microsoft_iq_credential(tenant_id, created["id"], "work_iq", repo=repo)
    assert credential["oauth_refresh_token_enc"] == "enc-refresh-token-2"
    assert credential["fabric_iq_workspace_id"] is None


def test_get_microsoft_iq_credential_is_scoped_to_the_requested_type(repo):
    tenant_id, group_id = _ids()
    created = connectors.create_work_iq_connector(tenant_id, "My Work IQ", group_id, "enc", repo=repo)
    # Asking for the wrong type at the same id must not return this
    # connector's credential -- type is part of the match, not just id/tenant.
    assert connectors.get_microsoft_iq_credential(tenant_id, created["id"], "fabric_iq_ontology", repo=repo) is None


def test_find_microsoft_iq_config_for_group_matches_by_group_and_type(repo):
    tenant_id, group_id = _ids()
    other_group_id = f"other_group_{uuid.uuid4().hex[:8]}"
    connectors.create_work_iq_connector(tenant_id, "Wrong group", other_group_id, "enc-wrong", repo=repo)
    connectors.create_fabric_iq_ontology_connector(
        tenant_id, "Right group, wrong type", group_id, "ws", "ont", "enc-fabric", repo=repo,
    )
    connectors.create_work_iq_connector(tenant_id, "Right group, right type", group_id, "enc-right", repo=repo)

    config = connectors.find_microsoft_iq_config_for_group(tenant_id, group_id, "work_iq", repo=repo)
    assert config["oauth_refresh_token_enc"] == "enc-right"


def test_find_microsoft_iq_config_for_group_returns_none_when_nothing_matches(repo):
    tenant_id, group_id = _ids()
    assert connectors.find_microsoft_iq_config_for_group(tenant_id, group_id, "work_iq", repo=repo) is None


def test_serialize_never_leaks_the_oauth_refresh_token():
    from app.api.connectors import _serialize

    created = {
        "id": "c1", "tenant_id": "t1", "name": "My Ontology", "type": "fabric_iq_ontology",
        "group_id": "g1", "url": None, "status": "never_synced", "last_synced_at": None,
        "last_error": None, "content_hash": None, "source_authority": 0,
        "fabric_iq_workspace_id": "ws-1", "fabric_iq_ontology_id": "ont-1",
        "oauth_refresh_token_enc": "super-secret-refresh-token",
    }
    serialized = _serialize(created)
    assert "oauth_refresh_token_enc" not in serialized
    assert "super-secret-refresh-token" not in str(serialized)
