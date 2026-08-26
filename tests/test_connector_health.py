# Tests app/api/connectors.py's _connector_health() -- pure function, no
# database, no network.
from datetime import datetime, timedelta, timezone

from app.api.connectors import _connector_health
from app.config import settings


def _connector(status: str, minutes_ago: float | None) -> dict:
    last_synced_at = None
    if minutes_ago is not None:
        last_synced_at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {"status": status, "last_synced_at": last_synced_at}


def test_error_status_is_always_error_health():
    assert _connector_health(_connector("error", minutes_ago=1)) == "error"


def test_never_synced_with_no_timestamp_is_never_synced():
    assert _connector_health(_connector("never_synced", minutes_ago=None)) == "never_synced"


def test_recently_synced_is_ok(monkeypatch):
    monkeypatch.setattr(settings, "connector_sync_interval_minutes", 15)
    assert _connector_health(_connector("synced", minutes_ago=5)) == "ok"


def test_synced_well_within_the_stale_threshold_is_ok(monkeypatch):
    monkeypatch.setattr(settings, "connector_sync_interval_minutes", 15)
    # 3x15 = 45 min threshold -- 40 minutes ago is still under it.
    assert _connector_health(_connector("synced", minutes_ago=40)) == "ok"


def test_synced_long_past_the_stale_threshold_is_stale(monkeypatch):
    monkeypatch.setattr(settings, "connector_sync_interval_minutes", 15)
    # 3x15 = 45 min threshold -- 200 minutes ago is well past it.
    assert _connector_health(_connector("synced", minutes_ago=200)) == "stale"


def test_unchanged_status_uses_the_same_staleness_rule_as_synced(monkeypatch):
    monkeypatch.setattr(settings, "connector_sync_interval_minutes", 15)
    assert _connector_health(_connector("unchanged", minutes_ago=200)) == "stale"
    assert _connector_health(_connector("unchanged", minutes_ago=5)) == "ok"


def test_queued_status_is_always_queued_health_even_if_the_prior_sync_was_stale():
    # A sync was just accepted onto the ingestion queue (app/graph/ingestion_queue.py)
    # -- mark_sync_queued() sets status="queued" without touching last_synced_at,
    # so the prior (possibly very old) timestamp must not leak "stale" through here.
    assert _connector_health(_connector("queued", minutes_ago=9999)) == "queued"
    assert _connector_health(_connector("queued", minutes_ago=None)) == "queued"
