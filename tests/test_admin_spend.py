# Pure-logic test for GET /api/v1/admin/spend (app/api/admin.py) -- the
# spend-limiter is faked (see test conventions elsewhere: real Neo4j only
# where a test actually needs it) so this doesn't touch the real, persisted
# data/processed/azure_openai_spend.json. Auth itself (require_admin) is a
# FastAPI Depends(), not exercised here -- see test_odata.py for the same
# "call the route function directly" convention this follows.
from app.api import admin


class _FakeLimiter:
    def __init__(self, spent):
        self._spent = spent

    def spent(self, bucket):
        return self._spent.get(bucket, 0.0)


def test_get_spend_reports_both_buckets_from_the_real_limiter(monkeypatch):
    monkeypatch.setattr(admin, "get_limiter", lambda: _FakeLimiter({"query": 1.234567, "ingestion": 0.5}))
    monkeypatch.setattr(admin.settings, "azure_openai_query_budget_usd", 20.0)
    monkeypatch.setattr(admin.settings, "azure_openai_ingestion_budget_usd", 30.0)

    result = admin.get_spend()

    assert result["query"] == {"spent_usd": 1.234567, "budget_usd": 20.0}
    assert result["ingestion"] == {"spent_usd": 0.5, "budget_usd": 30.0}


def test_get_spend_defaults_to_zero_when_nothing_spent_yet(monkeypatch):
    monkeypatch.setattr(admin, "get_limiter", lambda: _FakeLimiter({}))

    result = admin.get_spend()

    assert result["query"]["spent_usd"] == 0.0
    assert result["ingestion"]["spent_usd"] == 0.0
