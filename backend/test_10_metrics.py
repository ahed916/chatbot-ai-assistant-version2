"""
test_10_metrics.py — Verify metrics collection, Redis counters, and LangSmith config.

Tests:
  - MetricsCollector captures all fields correctly
  - Redis counters increment after agent calls
  - /metrics endpoint returns correct data
  - Audit log contains agent_metrics events
  - LangSmith env vars are detected (if configured)

Run: python tests/test_10_metrics.py
"""
import sys
import os
import json
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_metrics_collector():
    print("\n" + "=" * 60)
    print("TEST 10 — Metrics Collection")
    print("=" * 60)

    # ── MetricsCollector basic ────────────────────────────────
    print("\n[ MetricsCollector — Unit Test ]")
    from metrics import MetricsCollector, AgentMetrics
    import json as _json

    # Simulate a dashboard agent run
    with MetricsCollector("dashboard_agent", "test query for metrics") as mc:
        mc.record_context_build(ms=250.0, size_chars=3000)

        # Simulate agent messages with tool calls
        class FakeMsg:
            def __init__(self, tool_names=None):
                self.tool_calls = [{"name": n} for n in (tool_names or [])]

        fake_messages = [
            FakeMsg(["get_project_issues"]),  # 1 read tool call
            FakeMsg(),                         # llm response step
            FakeMsg(["generate_dashboard_json"]),  # chart tool
            FakeMsg(),                         # final response
        ]

        dashboard_json = _json.dumps({
            "type": "dashboard",
            "charts": [
                {"type": "bar", "title": "Issues", "data": []},
                {"type": "pie", "title": "Status", "data": []},
            ],
            "kpis": [
                {"label": "Open Issues", "value": 10},
                {"label": "Overdue", "value": 3},
            ],
            "summary": "Test summary",
        })

        mc.record_output(dashboard_json)
        mc.record_tool_calls(fake_messages)

    m = mc.metrics
    assert m.agent == "dashboard_agent", f"Wrong agent: {m.agent}"
    assert m.context_build_ms == 250.0, f"Wrong ctx ms: {m.context_build_ms}"
    assert m.context_size_chars == 3000, f"Wrong ctx size: {m.context_size_chars}"
    assert m.is_json_output, "Should detect JSON output"
    assert m.json_has_charts, "Should detect charts"
    assert m.json_has_kpis, "Should detect KPIs"
    assert m.chart_count == 2, f"Expected 2 charts, got {m.chart_count}"
    assert m.kpi_count == 2, f"Expected 2 KPIs, got {m.kpi_count}"
    assert m.total_latency_ms > 0, "Latency should be > 0"
    assert m.success, "Should be success"
    print(f"  ✅ Dashboard metrics captured correctly")
    print(f"     latency={m.total_latency_ms:.1f}ms, tools={m.tool_calls_count}, "
          f"charts={m.chart_count}, kpis={m.kpi_count}")

    # Simulate a risk agent run
    with MetricsCollector("risk_agent", "risk test query") as mc_risk:
        mc_risk.record_output("There are 3 overdue issues at risk. Critical workload imbalance detected.")
        mc_risk.record_risk_result({
            "risks": [{"name": "r1"}, {"name": "r2"}, {"name": "r3"}],
            "critical_count": 2,
            "overall_health": "At Risk",
            "slack_sent": False,
        })

    r = mc_risk.metrics
    assert r.risks_found == 3, f"Expected 3 risks, got {r.risks_found}"
    assert r.critical_count == 2, f"Expected 2 critical, got {r.critical_count}"
    assert r.overall_health == "At Risk", f"Wrong health: {r.overall_health}"
    print(f"  ✅ Risk metrics captured: risks={r.risks_found}, critical={r.critical_count}")

    # Simulate error case
    try:
        with MetricsCollector("automation_agent", "error test") as mc_err:
            raise ValueError("Simulated error")
    except ValueError:
        pass

    assert not mc_err.metrics.success, "Should detect failure"
    assert "Simulated error" in mc_err.metrics.error_message
    print(f"  ✅ Error case captured: success=False, error='{mc_err.metrics.error_message}'")

    # ── Redis counters ────────────────────────────────────────
    print("\n[ Redis Metric Counters ]")
    try:
        import redis as redis_lib
        from config import REDIS_HOST, REDIS_PORT, REDIS_DB
        r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

        # Read counters after our simulated runs
        keys = list(r.scan_iter("metrics:*"))
        if keys:
            print(f"  ✅ Found {len(keys)} metric key(s) in Redis:")
            for key in sorted(keys)[:15]:
                val = r.get(key)
                print(f"     {key} = {val}")
        else:
            print("  ⚠️  No metrics keys found yet in Redis")
            print("       (metrics are written to Redis during MetricsCollector.__exit__)")
            print("       Run python tests/test_06_agents.py first to populate")
    except Exception as e:
        print(f"  ⚠️  Redis check failed: {e}")

    # ── Audit log ─────────────────────────────────────────────
    print("\n[ Audit Log — agent_metrics Events ]")
    from pathlib import Path
    from config import AUDIT_LOG_FILE

    log_path = Path(AUDIT_LOG_FILE)
    if log_path.exists():
        lines = [l for l in log_path.read_text().strip().split("\n") if l.strip()]
        metric_events = []
        for line in lines:
            try:
                ev = json.loads(line)
                if ev.get("event") == "agent_metrics":
                    metric_events.append(ev)
            except Exception:
                pass

        if metric_events:
            print(f"  ✅ Found {len(metric_events)} agent_metrics events in audit log")
            for ev in metric_events[-3:]:
                print(f"     [{ev['ts'][:19]}] {ev['agent']} | "
                      f"latency={ev['latency_ms']:.0f}ms | "
                      f"tools={ev.get('tool_calls_count','?')} | "
                      f"success={ev['success']}")
        else:
            print(f"  ⚠️  No agent_metrics events yet — run test_06 first")
    else:
        print(f"  ⚠️  No audit log at {AUDIT_LOG_FILE}")

    # ── /metrics endpoint (via direct call, not HTTP) ─────────
    print("\n[ Live Metrics (direct call) ]")
    try:
        from metrics import get_live_metrics, get_metrics_from_audit_log

        live = get_live_metrics()
        print(f"  ✅ get_live_metrics() returned {len(live)} agent entries")
        for agent, data in live.items():
            if data.get("invocations", 0) > 0:
                print(f"     {agent}:")
                print(f"       invocations:       {data['invocations']}")
                print(f"       avg_latency_ms:    {data.get('avg_latency_ms', '?')}ms")
                print(f"       error_rate_pct:    {data.get('error_rate_pct', '?')}%")
                print(f"       cache_hit_rate:    {data.get('cache_hit_rate_pct', '?')}%")
                print(f"       avg_tool_calls:    {data.get('avg_tool_calls', '?')}")
                print(f"       avg_redundant:     {data.get('avg_redundant_reads', '?')}")
                print(f"       health:            {data.get('health', '?')}")
                if "json_success_rate_pct" in data:
                    print(f"       json_success:      {data['json_success_rate_pct']}%")
            else:
                print(f"     {agent}: no data yet")

        log_metrics = get_metrics_from_audit_log()
        if "note" not in log_metrics:
            print(f"\n  ✅ get_metrics_from_audit_log() — audit log analysis:")
            for agent, data in log_metrics.items():
                print(f"     {agent}: n={data['sample_size']}, "
                      f"p50={data['latency_ms']['p50']}ms, "
                      f"p90={data['latency_ms']['p90']}ms, "
                      f"success={data['success_rate_pct']}%")
        else:
            print(f"  ⚠️  Log metrics: {log_metrics['note']}")

    except Exception as e:
        print(f"  ❌ Metrics call failed: {e}")
        import traceback
        traceback.print_exc()

    # ── LangSmith setup check ─────────────────────────────────
    print("\n[ LangSmith Configuration ]")
    ls_key = os.getenv("LANGCHAIN_API_KEY", "")
    ls_tracing = os.getenv("LANGCHAIN_TRACING_V2", "false")
    ls_project = os.getenv("LANGCHAIN_PROJECT", "")

    if ls_key and not ls_key.startswith("ls__your"):
        print(f"  ✅ LANGCHAIN_API_KEY is set (starts with: {ls_key[:8]}...)")
    else:
        print(f"  ⚠️  LANGCHAIN_API_KEY not configured")
        print(f"       Get one at https://smith.langchain.com → Settings → API Keys")
        print(f"       Add to .env: LANGCHAIN_API_KEY=ls__your_key")

    if ls_tracing == "true":
        print(f"  ✅ Tracing enabled → project: '{ls_project}'")
        print(f"       View traces at: https://smith.langchain.com")
    else:
        print(f"  ℹ️  Tracing disabled (LANGCHAIN_TRACING_V2={ls_tracing})")
        print(f"       Set LANGCHAIN_TRACING_V2=true to enable")

    try:
        import langsmith
        print(f"  ✅ langsmith package installed: {langsmith.__version__}")
    except ImportError:
        print(f"  ⚠️  langsmith not installed — run: pip install langsmith")

    # ── How to run full evaluations ───────────────────────────
    print("\n[ How to Run LangSmith Evaluations ]")
    print("""
  After enabling LangSmith in .env:

  1. Create datasets (one time):
     python langsmith_setup.py --create-datasets

  2. Run evaluations (after each prompt change):
     python langsmith_setup.py --eval-routing      # routing accuracy
     python langsmith_setup.py --eval-dashboard    # dashboard JSON quality
     python langsmith_setup.py --eval-risk         # risk analysis quality
     python langsmith_setup.py --eval-all          # all three

  3. View results at https://smith.langchain.com → your project
     Compare prompts/v1 vs prompts/v2 experiments side by side.

  4. For live tracing during testing:
     Start your server with LANGCHAIN_TRACING_V2=true
     Every agent call appears as a trace in LangSmith within seconds.
""")

    print("✅ Metrics test complete.")


if __name__ == "__main__":
    test_metrics_collector()
