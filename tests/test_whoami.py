# GET /api/v1/context/whoami (app/api/context.py) only reads the
# TenantConfig require_tenant already resolved -- no database, no Neo4j
# needed. Calls the route function directly with the dependency's result
# passed in by hand, same pattern test_odata.py/test_webhooks.py use for
# routes that would otherwise need a real Request/DB.
import asyncio

import pytest

from app.api.context import whoami
from app.config import KnowledgeBase, TenantConfig


@pytest.fixture
def tenant():
    return TenantConfig(
        tenant_id="acme",
        gemini_api_key="fake-key",
        knowledge_bases=[
            KnowledgeBase(id="acme_demo", label="Demo"),
            KnowledgeBase(id="northwind", label="Northwind"),
        ],
    )


def test_whoami_reports_the_resolved_tenant_and_its_knowledge_bases(tenant):
    # whoami is async (matching the rest of app/api/context.py) but does no
    # I/O of its own -- asyncio.run() here avoids adding a pytest-asyncio
    # dependency for the one route in this file that happens to be async.
    result = asyncio.run(whoami(tenant=tenant))
    assert result == {
        "tenant_id": "acme",
        "knowledge_bases": [
            {"id": "acme_demo", "label": "Demo"},
            {"id": "northwind", "label": "Northwind"},
        ],
    }
