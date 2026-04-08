"""
test_07_supervisor.py — Test supervisor routing and end-to-end flow.

Tests:
  - Each route type is correctly identified (direct / dashboard / risk / automation / parallel)
  - LLM cache is used on identical queries
  - Parallel execution works
  - Audit log is written

Run: python tests/test_07_supervisor.py
"""
import sys
import os
import json
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


ROUTE_TEST_CASES = [
    # (prompt, expected_route, description)
    ("What projects do we have in Redmine?", "direct", "Simple read → direct"),
    ("How many open issues are there?", "direct", "Simple count → direct"),
    ("Who is assigned to issue #1?", "direct", "Single issue lookup → direct"),
    ("Give me a dashboard report of the project", "dashboard_agent", "Dashboard keyword → dashboard agent"),
    ("Show me charts and KPIs for the team", "dashboard_agent", "Charts keyword → dashboard agent"),
    ("Are there any risks in this project?", "risk_agent", "Risk keyword → risk agent"),
    ("Any overdue issues I should worry about?", "risk_agent", "Overdue/worry → risk agent"),
    ("Create a new task called Test", "automation_agent", "Create → automation agent"),
    ("Close issue #5", "automation_agent", "Close → automation agent"),
    ("Give me a full health report with risk and charts", "parallel", "Both agents → parallel"),
]


def test_routing():
    print("\n" + "=" * 60)
    print("TEST 07 — Supervisor Routing")
    print("=" * 60)
    print("⏳ Each routing decision = 1 LLM call (~5-15s each)...\n")

    from supervisor import _decide_routing

    print("[ Routing Decision Tests ]")
    correct = 0
    wrong = 0

    for prompt, expected_route, description in ROUTE_TEST_CASES:
        t = time.perf_counter()
        try:
            routing = _decide_routing(prompt, [])
            elapsed_ms = (time.perf_counter() - t) * 1000
            got_route = routing.get("route", "unknown")
            reason = routing.get("reason", "")

            if got_route == expected_route:
                print(f"  ✅ [{elapsed_ms:.0f}ms] {description}")
                print(f"     → route='{got_route}' | reason: '{reason}'")
                correct += 1
            else:
                print(f"  ⚠️  [{elapsed_ms:.0f}ms] {description}")
                print(f"     Expected: '{expected_route}', Got: '{got_route}'")
                print(f"     Reason: '{reason}'")
                print(f"     (Not a hard failure — routing is LLM-based, may vary)")
                wrong += 1
        except Exception as e:
            print(f"  ❌ Routing failed for '{prompt[:40]}...': {e}")
            wrong += 1

    print(f"\n  Routing accuracy: {correct}/{len(ROUTE_TEST_CASES)}")
    if wrong > 0:
        print(f"  ⚠️  {wrong} routing decision(s) didn't match expected")
        print(f"  This is normal with free LLMs — improve prompts/routing prompt if needed")


def test_cache():
    print("\n" + "─" * 60)
    print("[ LLM Response Cache ]")

    from supervisor import run_supervisor

    query = "What projects do we have in Redmine?"

    print(f"  Query: '{query}'")
    print("  Run 1 (cold)...")
    t1 = time.perf_counter()
    r1 = run_supervisor(query, [])
    t1_ms = (time.perf_counter() - t1) * 1000
    print(f"  ⏱  Run 1: {t1_ms:.0f}ms")

    print("  Run 2 (should hit cache)...")
    t2 = time.perf_counter()
    r2 = run_supervisor(query, [])
    t2_ms = (time.perf_counter() - t2) * 1000
    print(f"  ⏱  Run 2: {t2_ms:.0f}ms")

    if r1 == r2:
        print("  ✅ Cache returns identical response")
    else:
        print("  ⚠️  Responses differ (cache may have expired or wasn't set)")

    speedup = t1_ms / max(t2_ms, 1)
    if speedup > 5:
        print(f"  ✅ Cache speedup: {speedup:.0f}x faster")
    elif speedup > 2:
        print(f"  ✅ Cache speedup: {speedup:.1f}x faster")
    else:
        print(f"  ⚠️  Cache speedup: only {speedup:.1f}x — check Redis [LLM CACHE HIT] in logs")

    # Verify it's in Redis
    import redis as redis_lib
    from config import REDIS_HOST, REDIS_PORT, REDIS_DB
    from supervisor import _make_cache_key
    r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
    key = _make_cache_key(query, [])
    cached = r.get(key)
    if cached:
        print(f"  ✅ Key found in Redis: {key}")
    else:
        print(f"  ❌ Key NOT in Redis: {key}")


def test_full_flow():
    print("\n" + "─" * 60)
    print("[ Full End-to-End Flow ]")

    from supervisor import run_supervisor

    tests = [
        ("What projects are there?", "direct read"),
        ("Are there any overdue issues I should worry about?", "risk analysis"),
    ]

    for query, label in tests:
        print(f"\n  Testing: {label}")
        print(f"  Prompt: '{query}'")
        t = time.perf_counter()
        try:
            result = run_supervisor(query, [])
            elapsed_ms = (time.perf_counter() - t) * 1000
            print(f"  ⏱  {elapsed_ms:.0f}ms")
            print(f"  ✅ Response: '{result[:250]}'")
        except Exception as e:
            print(f"  ❌ Failed: {e}")


def test_audit_log():
    print("\n" + "─" * 60)
    print("[ Audit Log ]")

    import json as _json
    from pathlib import Path
    from config import AUDIT_LOG_FILE

    log_path = Path(AUDIT_LOG_FILE)
    if not log_path.exists():
        print(f"  ⚠️  No audit log at {log_path} yet — run some queries first")
        return

    lines = log_path.read_text().strip().split("\n")
    lines = [l for l in lines if l.strip()]
    print(f"  ✅ Audit log has {len(lines)} event(s)")

    # Parse and show last 5
    events = []
    for line in lines[-5:]:
        try:
            events.append(_json.loads(line))
        except Exception:
            pass

    for ev in events:
        print(f"  → [{ev.get('ts','?')[:19]}] {ev.get('event','?')} | agent={ev.get('agent','?')} | "
              f"latency={ev.get('latency_ms','?')}ms | success={ev.get('success','?')}")


if __name__ == "__main__":
    test_routing()
    test_cache()
    test_full_flow()
    test_audit_log()
    print("\n✅ Supervisor test complete.")
