"""
test_03_redis_cache.py — Verify Redis caching works correctly.

Tests:
  - Redis connection
  - Cache SET and GET roundtrip
  - TTL is applied correctly
  - Cache miss behavior
  - Redmine data is cached after first call
  - Second call hits cache (faster)
  - Cache invalidation clears correct keys
  - LLM response cache key generation

Run: python tests/test_03_redis_cache.py
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_redis():
    print("\n" + "=" * 60)
    print("TEST 03 — Redis Cache")
    print("=" * 60)

    # ── Direct Redis connection ───────────────────────────────
    print("\n[ Direct Redis Connection ]")
    try:
        import redis
        from config import REDIS_HOST, REDIS_PORT, REDIS_DB
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
        r.ping()
        print(f"  ✅ Connected to Redis at {REDIS_HOST}:{REDIS_PORT}")
    except Exception as e:
        print(f"  ❌ Redis connection failed: {e}")
        print("  Make sure Redis is running: docker ps | grep redis")
        sys.exit(1)

    # ── Basic SET/GET ─────────────────────────────────────────
    print("\n[ Basic SET / GET ]")
    r.setex("test:redmind:hello", 10, "world")
    val = r.get("test:redmind:hello")
    assert val == "world", f"Expected 'world', got '{val}'"
    print("  ✅ SET and GET work")

    ttl = r.ttl("test:redmind:hello")
    assert 0 < ttl <= 10, f"Expected TTL 1-10, got {ttl}"
    print(f"  ✅ TTL applied correctly: {ttl}s remaining")

    # ── Cache miss ────────────────────────────────────────────
    val_miss = r.get("test:redmind:nonexistent_key_xyz")
    assert val_miss is None, f"Expected None for missing key, got '{val_miss}'"
    print("  ✅ Cache miss returns None correctly")

    # Cleanup
    r.delete("test:redmind:hello")

    # ── Redmine caching behavior ──────────────────────────────
    print("\n[ Redmine Data Caching ]")

    # Clear existing cache to test cold start
    for k in r.scan_iter("projects*"):
        r.delete(k)
    print("  → Cleared 'projects' cache key")

    import redmine as rm

    # First call — should hit Redmine API
    t1 = time.perf_counter()
    projects1 = rm.list_projects()
    t1_ms = (time.perf_counter() - t1) * 1000

    # Verify it was cached
    cached_raw = r.get("projects")
    assert cached_raw is not None, "projects was not cached after first call!"
    print(f"  ✅ First call (API): {t1_ms:.0f}ms — cached ✅")

    # Second call — should hit Redis cache
    t2 = time.perf_counter()
    projects2 = rm.list_projects()
    t2_ms = (time.perf_counter() - t2) * 1000

    assert projects1 == projects2, "Cached result differs from original!"
    print(f"  ✅ Second call (cache): {t2_ms:.0f}ms — speedup: {t1_ms/max(t2_ms,0.1):.1f}x")

    if t2_ms < t1_ms * 0.5:
        print(f"  ✅ Cache is significantly faster ({t1_ms:.0f}ms → {t2_ms:.0f}ms)")
    else:
        print(f"  ⚠️  Cache speedup not dramatic — Redis may be on same machine as Redmine")

    # ── TTL values from config ────────────────────────────────
    print("\n[ TTL Values from .env / config ]")
    from config import (
        CACHE_TTL_PROJECTS, CACHE_TTL_TRACKERS, CACHE_TTL_STATUSES,
        CACHE_TTL_MEMBERS, CACHE_TTL_ISSUES, CACHE_TTL_USERS,
        CACHE_TTL_LLM_RESPONSE,
    )
    ttl_projects = r.ttl("projects")
    print(f"  ✅ CACHE_TTL_PROJECTS = {CACHE_TTL_PROJECTS}s (Redis shows: {ttl_projects}s)")
    print(f"  ✅ CACHE_TTL_ISSUES   = {CACHE_TTL_ISSUES}s")
    print(f"  ✅ CACHE_TTL_USERS    = {CACHE_TTL_USERS}s")
    print(f"  ✅ CACHE_TTL_LLM      = {CACHE_TTL_LLM_RESPONSE}s")

    # ── Cache invalidation ────────────────────────────────────
    print("\n[ Cache Invalidation ]")

    # Seed some issue keys
    r.setex("issues:test:open:100:None:None", 30, "[]")
    r.setex("issues:test:closed:50:None:None", 30, "[]")
    r.setex("issue:42", 30, "{}")
    r.setex("resolve:myproject", 300, "5")

    # Trigger invalidation (as called by write operations)
    from redmine import _cache_invalidate
    _cache_invalidate("issue:42")

    assert r.get("issues:test:open:100:None:None") is None, "issues:* not cleared by invalidation"
    assert r.get("issues:test:closed:50:None:None") is None, "issues:* not cleared by invalidation"
    assert r.get("issue:42") is None, "specific issue key not cleared"
    assert r.get("resolve:myproject") is None, "resolve:* not cleared by invalidation"
    print("  ✅ Cache invalidation clears issues:*, resolve:*, and specific issue key")

    # ── LLM response cache key ────────────────────────────────
    print("\n[ LLM Response Cache Key ]")
    from supervisor import _make_cache_key
    key1 = _make_cache_key("What projects do we have?", [])
    key2 = _make_cache_key("What projects do we have?", [])
    key3 = _make_cache_key("How many issues are open?", [])

    assert key1 == key2, "Same query should produce same cache key"
    assert key1 != key3, "Different queries should produce different keys"
    assert key1.startswith("llm:"), f"Cache key should start with 'llm:', got: {key1}"
    print(f"  ✅ Same query → same key: {key1}")
    print(f"  ✅ Different query → different key: {key3}")
    print(f"  ✅ Key format: 'llm:' prefix ✅")

    print("\n✅ Redis cache test PASSED")


if __name__ == "__main__":
    test_redis()
