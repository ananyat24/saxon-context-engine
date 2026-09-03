# app/graph/connectors.py's foundry_iq-specific storage (create_foundry_iq_connector,
# find_foundry_iq_config_for_group, get_foundry_iq_credential): needs a
# real, reachable Neo4j, same pattern as test_purge_connector_data.py.
import uuid

import pytest

from app.graph import connectors
from app.graph.graph_repository import GraphRepository


@pytest.fixture
def repo():
    return GraphRepository()


def _ids():
    return (
        f"test_tenant_{uuid.uuid4().hex[:8]}",
        f"test_group_{uuid.uuid4().hex[:8]}",
    )


def test_create_and_fetch_by_id_round_trips_the_encrypted_credential(repo):
    tenant_id, group_id = _ids()
    created = connectors.create_foundry_iq_connector(
        tenant_id, "Contoso Foundry IQ", group_id,
        "https://contoso.search.windows.net", "contoso-kb", "enc-blob-123",
        repo=repo,
    )
    assert created["type"] == "foundry_iq"
    assert created["url"] == "https://contoso.search.windows.net"
    assert created["foundry_iq_knowledge_base"] == "contoso-kb"

    credential = connectors.get_foundry_iq_credential(tenant_id, created["id"], repo=repo)
    assert credential == {
        "search_endpoint": "https://contoso.search.windows.net",
        "knowledge_base": "contoso-kb",
        "api_key_enc": "enc-blob-123",
    }


def test_get_foundry_iq_credential_returns_none_for_a_non_foundry_iq_connector(repo):
    tenant_id, group_id = _ids()
    created = connectors.create_connector(tenant_id, "A web page", "web", group_id, "https://example.com", repo=repo)
    assert connectors.get_foundry_iq_credential(tenant_id, created["id"], repo=repo) is None


def test_get_foundry_iq_credential_scoped_to_the_owning_tenant(repo):
    tenant_id, group_id = _ids()
    other_tenant_id = f"other_tenant_{uuid.uuid4().hex[:8]}"
    created = connectors.create_foundry_iq_connector(
        tenant_id, "Contoso Foundry IQ", group_id, "https://x.search.windows.net", "kb", "enc", repo=repo,
    )
    assert connectors.get_foundry_iq_credential(other_tenant_id, created["id"], repo=repo) is None


def test_find_foundry_iq_config_for_group_matches_by_group_id(repo):
    tenant_id, group_id = _ids()
    other_group_id = f"other_group_{uuid.uuid4().hex[:8]}"
    connectors.create_foundry_iq_connector(
        tenant_id, "Wrong KB", other_group_id, "https://wrong", "wrong-kb", "enc-wrong", repo=repo,
    )
    connectors.create_foundry_iq_connector(
        tenant_id, "Right KB", group_id, "https://right.search.windows.net", "right-kb", "enc-right", repo=repo,
    )

    config = connectors.find_foundry_iq_config_for_group(tenant_id, group_id, repo=repo)
    assert config["search_endpoint"] == "https://right.search.windows.net"
    assert config["knowledge_base"] == "right-kb"


def test_find_foundry_iq_config_for_group_returns_none_when_no_connector_exists(repo):
    tenant_id, group_id = _ids()
    assert connectors.find_foundry_iq_config_for_group(tenant_id, group_id, repo=repo) is None


def test_find_foundry_iq_config_for_group_prefers_the_most_recently_created(repo):
    tenant_id, group_id = _ids()
    connectors.create_foundry_iq_connector(
        tenant_id, "Older", group_id, "https://older", "older-kb", "enc-older", repo=repo,
    )
    connectors.create_foundry_iq_connector(
        tenant_id, "Newer", group_id, "https://newer", "newer-kb", "enc-newer", repo=repo,
    )

    config = connectors.find_foundry_iq_config_for_group(tenant_id, group_id, repo=repo)
    assert config["knowledge_base"] == "newer-kb"


def test_serialize_never_leaks_the_encrypted_api_key():
    from app.api.connectors import _serialize

    created = {
        "id": "c1", "tenant_id": "t1", "name": "Contoso Foundry IQ", "type": "foundry_iq",
        "group_id": "g1", "url": "https://x", "status": "never_synced", "last_synced_at": None,
        "last_error": None, "content_hash": None, "source_authority": 0,
        "foundry_iq_knowledge_base": "kb1", "foundry_iq_api_key_enc": "super-secret-encrypted-blob",
    }
    serialized = _serialize(created)
    assert "foundry_iq_api_key_enc" not in serialized
    assert "super-secret-encrypted-blob" not in str(serialized)
