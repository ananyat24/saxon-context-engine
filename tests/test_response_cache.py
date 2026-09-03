# Tests app/context/response_cache.py: pure in-process logic, no database,
# no network. Uses its own ResponseCache instances (not the shared
# get_response_cache() singleton) so tests can't interfere with each other.
import time

from app.context.response_cache import ResponseCache


def test_cache_hit_returns_the_same_value():
    cache = ResponseCache(ttl_seconds=60)
    key = cache.make_key("tenant-a", ["kb1"], None, "What do we know about Acme?", 8)
    cache.set(key, {"summary": "cached answer"})

    assert cache.get(key) == {"summary": "cached answer"}


def test_cache_miss_returns_none():
    cache = ResponseCache(ttl_seconds=60)
    key = cache.make_key("tenant-a", ["kb1"], None, "never asked", 8)
    assert cache.get(key) is None


def test_make_key_normalizes_whitespace_and_case():
    cache = ResponseCache(ttl_seconds=60)
    key_a = cache.make_key("tenant-a", ["kb1"], None, "  What About Acme?  ", 8)
    key_b = cache.make_key("tenant-a", ["kb1"], None, "what about acme?", 8)
    assert key_a == key_b


def test_make_key_sorts_group_ids_so_order_does_not_matter():
    cache = ResponseCache(ttl_seconds=60)
    key_a = cache.make_key("tenant-a", ["kb2", "kb1"], None, "q", 8)
    key_b = cache.make_key("tenant-a", ["kb1", "kb2"], None, "q", 8)
    assert key_a == key_b


def test_different_tenants_never_share_a_cache_entry():
    cache = ResponseCache(ttl_seconds=60)
    key_a = cache.make_key("tenant-a", ["kb1"], None, "q", 8)
    key_b = cache.make_key("tenant-b", ["kb1"], None, "q", 8)
    cache.set(key_a, "answer for tenant a")
    assert cache.get(key_b) is None


def test_different_as_user_never_share_a_cache_entry():
    # Role-based visibility means the same question can have a different
    # answer depending on who's asking: these must never collide.
    cache = ResponseCache(ttl_seconds=60)
    key_rep = cache.make_key("tenant-a", ["kb1"], "user-rep", "q", 8)
    key_exec = cache.make_key("tenant-a", ["kb1"], "user-exec", "q", 8)
    cache.set(key_rep, "rep's view")
    assert cache.get(key_exec) is None


def test_entry_expires_after_ttl():
    cache = ResponseCache(ttl_seconds=0.05)
    key = cache.make_key("tenant-a", ["kb1"], None, "q", 8)
    cache.set(key, "will expire")
    assert cache.get(key) == "will expire"
    time.sleep(0.1)
    assert cache.get(key) is None


def test_invalidate_group_drops_only_matching_entries():
    cache = ResponseCache(ttl_seconds=60)
    key_kb1 = cache.make_key("tenant-a", ["kb1"], None, "q1", 8)
    key_kb2 = cache.make_key("tenant-a", ["kb2"], None, "q2", 8)
    key_other_tenant = cache.make_key("tenant-b", ["kb1"], None, "q1", 8)
    cache.set(key_kb1, "a")
    cache.set(key_kb2, "b")
    cache.set(key_other_tenant, "c")

    cache.invalidate_group("tenant-a", "kb1")

    assert cache.get(key_kb1) is None
    assert cache.get(key_kb2) == "b"
    assert cache.get(key_other_tenant) == "c"


def test_invalidate_group_matches_a_document_set_spanning_the_group():
    # A document-set-scoped query's key holds multiple group_ids: syncing
    # any one of that set's connectors should invalidate it.
    cache = ResponseCache(ttl_seconds=60)
    key = cache.make_key("tenant-a", ["kb1", "kb2", "kb3"], None, "q", 8)
    cache.set(key, "multi-source answer")

    cache.invalidate_group("tenant-a", "kb2")

    assert cache.get(key) is None


def test_max_entries_evicts_oldest_first():
    cache = ResponseCache(ttl_seconds=60, max_entries=2)
    key_1 = cache.make_key("tenant-a", ["kb1"], None, "q1", 8)
    key_2 = cache.make_key("tenant-a", ["kb1"], None, "q2", 8)
    key_3 = cache.make_key("tenant-a", ["kb1"], None, "q3", 8)
    cache.set(key_1, "1")
    cache.set(key_2, "2")
    cache.set(key_3, "3")  # should evict key_1

    assert cache.get(key_1) is None
    assert cache.get(key_2) == "2"
    assert cache.get(key_3) == "3"
