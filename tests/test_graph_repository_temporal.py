# Pure-function tests for _not_yet_invalidated(): no database needed. Covers
# the bug found while demoing the v1 connectors: a CRM row's "renewal date"
# column got extracted as an edge's own invalid_at, and since that date was
# still in the future, treating any invalid_at as already-invalid made an
# account's current facts disappear from query results well before the
# renewal date actually arrived.
from datetime import datetime, timedelta, timezone

from app.graph.graph_repository import _not_yet_invalidated


def test_none_is_not_yet_invalidated():
    assert _not_yet_invalidated(None) is True


def test_future_datetime_is_not_yet_invalidated():
    future = datetime.now(timezone.utc) + timedelta(days=200)
    assert _not_yet_invalidated(future) is True


def test_past_datetime_is_invalidated():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert _not_yet_invalidated(past) is False


def test_future_iso_string_is_not_yet_invalidated():
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert _not_yet_invalidated(future.isoformat().replace("+00:00", "Z")) is True


def test_past_iso_string_is_invalidated():
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert _not_yet_invalidated(past.isoformat().replace("+00:00", "Z")) is False


def test_naive_datetime_is_treated_as_utc():
    # Graphiti/neo4j datetimes are normally tz-aware, but a naive one
    # shouldn't crash the comparison: it's treated as UTC rather than
    # raising on offset-naive vs. offset-aware comparison.
    future_naive = (datetime.now(timezone.utc) + timedelta(days=1)).replace(tzinfo=None)
    assert _not_yet_invalidated(future_naive) is True


def test_unparseable_string_falls_back_to_invalidated():
    # Conservative default: if it can't be parsed, don't risk surfacing a
    # genuinely-superseded fact as current.
    assert _not_yet_invalidated("not a date") is False


def test_unexpected_type_falls_back_to_invalidated():
    assert _not_yet_invalidated(12345) is False
