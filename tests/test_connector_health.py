# Tests app/api/connectors.py's _connector_health(): pure function, no
# database, no network.
from datetime import datetime, timedelta, timezone

from app.api.connectors import _connector_health


def _connector(status: str, minutes_ago: float | None) -> dict:
    last_synced_at = None
    if minutes_ago is not None:
        last_synced_at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {"status": status, "last_synced_at": last_synced_at}


def test_error_status_is_always_error_health():
    assert _connector_health(_connector("error", minutes_ago=1)) == "error"


def test_never_synced_with_no_timestamp_is_never_synced():
    assert _connector_health(_connector("never_synced", minutes_ago=None)) == "never_synced"


def test_recently_synced_is_ok():
    assert _connector_health(_connector("synced", minutes_ago=5)) == "ok"


def test_synced_long_ago_is_still_ok():
    # No more time-based staleness: see _connector_health's docstring for
    # why (Azure Container Apps scales this deployment to zero replicas
    # when idle, so a long gap since the last sync doesn't mean anything
    # broke).
    assert _connector_health(_connector("synced", minutes_ago=200)) == "ok"


def test_unchanged_status_is_ok_regardless_of_age():
    assert _connector_health(_connector("unchanged", minutes_ago=200)) == "ok"
    assert _connector_health(_connector("unchanged", minutes_ago=5)) == "ok"


def test_queued_status_is_always_queued_health_even_if_the_prior_sync_was_old():
    # A sync was just accepted onto the ingestion queue (app/graph/ingestion_queue.py):
    # mark_sync_queued() sets status="queued" without touching last_synced_at,
    # so the prior (possibly very old) timestamp must not leak through here.
    assert _connector_health(_connector("queued", minutes_ago=9999)) == "queued"
    assert _connector_health(_connector("queued", minutes_ago=None)) == "queued"
