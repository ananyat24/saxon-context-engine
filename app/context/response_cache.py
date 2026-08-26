# In-process cache of (tenant, scope, query) -> an already-computed
# ContextPacket, so two people asking near-identical questions in a row
# don't each pay for retrieval + synthesis from scratch.
#
# Deliberately simple: an in-memory dict with both a TTL (a safety net,
# since exact change-tracking invalidation is real, separate scope -- see
# CLAUDE.md's v3 status note) and explicit invalidation whenever a connector
# sync actually changes data for a tenant/group (see
# app/ingestion/connector_sync.py's call to invalidate_group()), which
# covers the common case -- a synced connector -- without needing full
# change-tracking. Lost on process restart, and never shared across
# instances, which is fine: this is a performance optimization, not a
# source of truth, and a cache miss just means paying full price once.
import threading
import time
from typing import Any, Optional

from app.config import settings

# Shorter than the default connector sync interval (15 min, see
# app/graph/connector_scheduler.py) so a cached answer never meaningfully
# outlives what a background sync would have refreshed by anyway.
_DEFAULT_TTL_SECONDS = 300.0
# Bounds memory regardless of how many distinct queries come in -- oldest
# entries evicted first once exceeded, same simple bound spend_limiter.py
# and other in-process state in this codebase already accept.
_MAX_ENTRIES = 500

CacheKey = tuple[str, tuple[str, ...], str, str, int]


class ResponseCache:
    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS, max_entries: int = _MAX_ENTRIES):
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[CacheKey, tuple[float, Any]] = {}
        self._order: list[CacheKey] = []  # insertion order, for oldest-first eviction

    @staticmethod
    def make_key(
        tenant_id: str, group_ids: list[str], as_user: Optional[str], query: str, num_results: int
    ) -> CacheKey:
        # Normalizing whitespace/case means "Fenwick & Cole Legal" and
        # "  fenwick & cole legal  " share a cache entry rather than each
        # paying full price for what's really the same question.
        normalized_query = " ".join(query.strip().lower().split())
        return (tenant_id, tuple(sorted(group_ids)), as_user or "", normalized_query, num_results)

    def get(self, key: CacheKey) -> Optional[Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            cached_at, value = entry
            if time.monotonic() - cached_at > self._ttl:
                self._drop(key)
                return None
            return value

    def set(self, key: CacheKey, value: Any) -> None:
        with self._lock:
            if key not in self._entries and len(self._entries) >= self._max_entries:
                self._drop(self._order[0])
            self._entries[key] = (time.monotonic(), value)
            if key not in self._order:
                self._order.append(key)

    def invalidate_group(self, tenant_id: str, group_id: str) -> None:
        """Called after a connector sync actually changes data for one
        group_id -- drops every cached entry whose scope could include that
        group, rather than waiting out the TTL and risking a stale "no
        information found" right after new data was just ingested."""
        with self._lock:
            stale = [key for key in self._entries if key[0] == tenant_id and group_id in key[1]]
            for key in stale:
                self._drop(key)

    def _drop(self, key: CacheKey) -> None:
        self._entries.pop(key, None)
        if key in self._order:
            self._order.remove(key)


# One process-wide cache (like app/graph/spend_limiter.py's _limiter), so
# every request shares the same cache rather than each holding its own.
_cache = ResponseCache(ttl_seconds=settings.response_cache_ttl_seconds)


def get_response_cache() -> ResponseCache:
    return _cache
