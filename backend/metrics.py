"""
metrics.py — Per-agent performance metrics collector.

Tracks everything that matters for evaluating agent quality and speed.
Metrics are stored in Redis (live counters) and in the JSONL audit log
(full history). A /metrics endpoint in main.py exposes them as JSON.

═══════════════════════════════════════════════════════════════
WHAT WE MEASURE AND WHY
═══════════════════════════════════════════════════════════════

┌─────────────────────────┬──────────────────────────────────────┐
│ Metric                  │ Why it matters                       │
├─────────────────────────┼──────────────────────────────────────┤
│ latency_ms              │ UX — how long does the user wait?    │
│ tool_calls_count        │ Efficiency — fewer = better/cheaper  │
│ tool_calls_list         │ Debug — which tools are overused?    │
│ llm_steps               │ Recursion usage — near limit = risk  │
│ output_length           │ Quality signal — too short = bad     │
│ is_json_output          │ Dashboard agent: did it produce JSON?│
│ json_has_charts         │ Dashboard: did charts come out?      │
│ json_has_kpis           │ Dashboard: did KPIs come out?        │
│ risk_count_found        │ Risk agent: how many risks detected? │
│ overall_health          │ Risk agent: what health rating?      │
│ redmine_writes          │ Automation: how many writes made?    │
│ error_rate              │ Reliability — how often does it fail?│
│ cache_hit               │ Was this served from LLM cache?      │
│ context_size_chars      │ Context injection size               │
│ success                 │ Did the agent complete without error?│
└─────────────────────────┴──────────────────────────────────────┘

PERFORMANCE THRESHOLDS (what "good" looks like):
  latency_ms:       < 30,000ms  (free model) / < 5,000ms (paid model)
  tool_calls_count: < 3         (context injection should eliminate most)
  llm_steps:        < 20        (out of AGENT_RECURSION_LIMIT=30)
  error_rate:       < 5%
  json_output rate: > 90%       (dashboard agent)
  risk detection:   > 0         (when there are actual overdue issues)
"""

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

import redis as redis_lib

from audit import log_event
from config import REDIS_HOST, REDIS_PORT, REDIS_DB

logger = logging.getLogger(__name__)

# ── Redis for live counters ───────────────────────────────────────────────────
try:
    _redis = redis_lib.Redis(
        host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
        decode_responses=True,
        socket_connect_timeout=2, socket_timeout=2,
    )
    _redis.ping()
except Exception:
    _redis = None


# ── Metric data structure ─────────────────────────────────────────────────────

@dataclass
class AgentMetrics:
    """All metrics captured for a single agent invocation."""

    # Identity
    agent: str = ""
    query: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # ── Latency ──────────────────────────────────────────────────────────────
    total_latency_ms: float = 0.0   # wall-clock time for entire agent run
    context_build_ms: float = 0.0   # time to fetch + build context block
    llm_first_token_ms: float = 0.0   # routing decision speed (supervisor)

    # ── LLM Usage Efficiency ─────────────────────────────────────────────────
    tool_calls_count: int = 0           # total tool calls made during this run
    tool_calls_list: list = field(default_factory=list)  # names of tools called
    llm_steps: int = 0           # graph steps used (out of recursion_limit)
    redundant_reads: int = 0           # read tools called despite data in context

    # ── Output Quality ────────────────────────────────────────────────────────
    output_length: int = 0           # character count of final response
    success: bool = True
    error_message: str = ""
    cache_hit: bool = False      # served from LLM Redis cache?
    context_size_chars: int = 0          # size of injected context block

    # ── Dashboard Agent Specific ──────────────────────────────────────────────
    is_json_output: bool = False      # did agent return valid JSON?
    json_has_charts: bool = False      # does JSON have charts array?
    json_has_kpis: bool = False      # does JSON have kpis array?
    chart_count: int = 0          # number of charts generated
    kpi_count: int = 0          # number of KPIs generated

    # ── Risk Agent Specific ───────────────────────────────────────────────────
    risks_found: int = 0           # number of risks identified
    critical_count: int = 0           # critical + high risks
    overall_health: str = ""          # "Healthy" / "At Risk" / "Critical"
    slack_sent: bool = False      # was Slack notification sent?

    # ── Automation Agent Specific ─────────────────────────────────────────────
    redmine_writes: int = 0           # write operations performed
    write_operations: list = field(default_factory=list)  # list of operations done

    # ── Routing (Supervisor) ──────────────────────────────────────────────────
    route_decision: str = ""          # direct / dashboard_agent / etc.
    routing_latency_ms: float = 0.0


# ── Metrics Collector (context manager) ──────────────────────────────────────

class MetricsCollector:
    """
    Context manager that wraps an agent run and collects all metrics.

    Usage:
        with MetricsCollector("dashboard_agent", query) as mc:
            result = agent.invoke(...)
            mc.record_output(result)
            mc.record_tool_calls(messages)
        # metrics are automatically saved when exiting the context
    """

    def __init__(self, agent_name: str, query: str):
        self.metrics = AgentMetrics(agent=agent_name, query=query[:200])
        self._start = 0.0

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.metrics.total_latency_ms = (time.perf_counter() - self._start) * 1000
        if exc_val:
            self.metrics.success = False
            self.metrics.error_message = str(exc_val)[:300]
        self._save()
        return False  # don't suppress exceptions

    def record_context_build(self, ms: float, size_chars: int):
        """Call after inject_context() to record how long context building took."""
        self.metrics.context_build_ms = ms
        self.metrics.context_size_chars = size_chars

    def record_output(self, output: str):
        """Analyze the agent's final output for quality signals."""
        self.metrics.output_length = len(output)
        self._analyze_output(output)

    def record_tool_calls(self, messages: list):
        """
        Extract tool call info from LangGraph message history.
        messages = result["messages"] from agent.invoke()
        """
        tool_names = []
        steps = 0
        redundant_reads = 0

        READ_TOOL_NAMES = {
            "get_all_projects", "get_project_issues", "get_all_issues_across_projects",
            "get_project_members", "get_available_statuses", "get_available_trackers",
            "get_all_users", "get_allowed_status_transitions", "get_workload_by_member",
            "get_issue_details",
        }

        for msg in messages:
            steps += 1
            # LangChain tool call messages have tool_calls attribute
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                    if name:
                        tool_names.append(name)
                        # Detect redundant read calls (data was already in context)
                        if name in READ_TOOL_NAMES:
                            redundant_reads += 1

        self.metrics.tool_calls_count = len(tool_names)
        self.metrics.tool_calls_list = tool_names
        self.metrics.llm_steps = steps
        self.metrics.redundant_reads = max(0, redundant_reads - 1)  # first call is fine

    def record_risk_result(self, parsed: dict):
        """Record risk-specific metrics from proactive_risk_check result."""
        self.metrics.risks_found = len(parsed.get("risks", []))
        self.metrics.critical_count = parsed.get("critical_count", 0)
        self.metrics.overall_health = parsed.get("overall_health", "")
        self.metrics.slack_sent = parsed.get("slack_sent", False)

    def record_write_operation(self, operation: str):
        """Call each time a Redmine write is performed."""
        self.metrics.redmine_writes += 1
        self.metrics.write_operations.append(operation)

    def record_routing(self, route: str, latency_ms: float):
        """Record supervisor routing decision."""
        self.metrics.route_decision = route
        self.metrics.routing_latency_ms = latency_ms

    def record_cache_hit(self):
        self.metrics.cache_hit = True

    def _analyze_output(self, output: str):
        """Analyze output string for quality signals."""
        stripped = output.strip()

        # Try JSON parse for dashboard detection
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
                self.metrics.is_json_output = True
                charts = data.get("charts", [])
                kpis = data.get("kpis", [])
                self.metrics.json_has_charts = len(charts) > 0
                self.metrics.json_has_kpis = len(kpis) > 0
                self.metrics.chart_count = len(charts)
                self.metrics.kpi_count = len(kpis)
            except json.JSONDecodeError:
                self.metrics.is_json_output = False
        else:
            self.metrics.is_json_output = False

    def _save(self):
        """Persist metrics to audit log and Redis counters."""
        m = self.metrics
        d = asdict(m)

        # ── Audit log (full detail) ───────────────────────────────────────────
        log_event(
            "agent_metrics",
            agent=m.agent,
            user_input=m.query,
            latency_ms=m.total_latency_ms,
            success=m.success,
            error=m.error_message,
            extra={
                "tool_calls_count": m.tool_calls_count,
                "tool_calls_list": m.tool_calls_list,
                "llm_steps": m.llm_steps,
                "redundant_reads": m.redundant_reads,
                "output_length": m.output_length,
                "cache_hit": m.cache_hit,
                "context_size_chars": m.context_size_chars,
                "context_build_ms": m.context_build_ms,
                # Agent-specific
                "is_json_output": m.is_json_output,
                "chart_count": m.chart_count,
                "kpi_count": m.kpi_count,
                "risks_found": m.risks_found,
                "critical_count": m.critical_count,
                "overall_health": m.overall_health,
                "redmine_writes": m.redmine_writes,
                "route_decision": m.route_decision,
                "routing_latency_ms": m.routing_latency_ms,
            },
        )

        # ── Redis live counters ───────────────────────────────────────────────
        if _redis:
            try:
                pipe = _redis.pipeline()
                prefix = f"metrics:{m.agent}"

                pipe.incr(f"{prefix}:invocations")
                pipe.incrbyfloat(f"{prefix}:total_latency_ms", m.total_latency_ms)
                pipe.incr(f"{prefix}:tool_calls_total", m.tool_calls_count)
                pipe.incr(f"{prefix}:llm_steps_total", m.llm_steps)
                pipe.incr(f"{prefix}:redundant_reads_total", m.redundant_reads)

                if not m.success:
                    pipe.incr(f"{prefix}:errors")
                if m.cache_hit:
                    pipe.incr(f"{prefix}:cache_hits")

                # Agent-specific counters
                if m.agent == "dashboard_agent":
                    if m.is_json_output:
                        pipe.incr(f"{prefix}:json_success")
                    if m.json_has_charts:
                        pipe.incr(f"{prefix}:charts_produced")
                    if m.json_has_kpis:
                        pipe.incr(f"{prefix}:kpis_produced")

                if m.agent == "risk_agent":
                    pipe.incr(f"{prefix}:risks_found_total", max(m.risks_found, 0))
                    pipe.incr(f"{prefix}:critical_total", max(m.critical_count, 0))

                if m.agent == "automation_agent":
                    pipe.incr(f"{prefix}:writes_total", max(m.redmine_writes, 0))

                # Latency buckets (for percentile approximation)
                lat = m.total_latency_ms
                bucket = "fast" if lat < 10000 else "medium" if lat < 30000 else "slow"
                pipe.incr(f"{prefix}:latency_bucket:{bucket}")

                pipe.execute()
            except Exception as e:
                logger.warning(f"[METRICS] Redis write failed: {e}")

        logger.info(
            f"[METRICS] {m.agent} | {m.total_latency_ms:.0f}ms | "
            f"tools={m.tool_calls_count} | steps={m.llm_steps} | "
            f"success={m.success} | cache={m.cache_hit}"
        )


# ── Metrics Reporter ──────────────────────────────────────────────────────────

def get_live_metrics() -> dict:
    """
    Read all live metrics from Redis counters.
    Called by the /metrics endpoint in main.py.
    Returns a dict with per-agent stats and computed rates.
    """
    if not _redis:
        return {"error": "Redis unavailable — metrics require Redis"}

    agents = ["dashboard_agent", "risk_agent", "automation_agent", "supervisor"]
    result = {}

    for agent in agents:
        prefix = f"metrics:{agent}"
        try:
            invocations = int(_redis.get(f"{prefix}:invocations") or 0)
            if invocations == 0:
                result[agent] = {"invocations": 0, "note": "No data yet"}
                continue

            total_latency = float(_redis.get(f"{prefix}:total_latency_ms") or 0)
            errors = int(_redis.get(f"{prefix}:errors") or 0)
            cache_hits = int(_redis.get(f"{prefix}:cache_hits") or 0)
            tool_calls = int(_redis.get(f"{prefix}:tool_calls_total") or 0)
            llm_steps = int(_redis.get(f"{prefix}:llm_steps_total") or 0)
            redundant_reads = int(_redis.get(f"{prefix}:redundant_reads_total") or 0)

            fast = int(_redis.get(f"{prefix}:latency_bucket:fast") or 0)
            medium = int(_redis.get(f"{prefix}:latency_bucket:medium") or 0)
            slow = int(_redis.get(f"{prefix}:latency_bucket:slow") or 0)

            stats = {
                "invocations": invocations,
                "avg_latency_ms": round(total_latency / invocations, 0),
                "error_rate_pct": round(100 * errors / invocations, 1),
                "cache_hit_rate_pct": round(100 * cache_hits / invocations, 1),
                "avg_tool_calls": round(tool_calls / invocations, 1),
                "avg_llm_steps": round(llm_steps / invocations, 1),
                "avg_redundant_reads": round(redundant_reads / invocations, 1),
                "latency_distribution": {
                    "fast_pct": round(100 * fast / invocations, 1),
                    "medium_pct": round(100 * medium / invocations, 1),
                    "slow_pct": round(100 * slow / invocations, 1),
                },
                # Thresholds — "is this agent performing well?"
                "health": _compute_health(
                    avg_latency_ms=total_latency / invocations,
                    error_rate=errors / invocations,
                    avg_tool_calls=tool_calls / invocations,
                    avg_redundant=redundant_reads / invocations,
                ),
            }

            # Agent-specific metrics
            if agent == "dashboard_agent":
                json_success = int(_redis.get(f"{prefix}:json_success") or 0)
                charts_prod = int(_redis.get(f"{prefix}:charts_produced") or 0)
                stats["json_success_rate_pct"] = round(100 * json_success / invocations, 1)
                stats["charts_produced_rate_pct"] = round(100 * charts_prod / invocations, 1)

            if agent == "risk_agent":
                risks_total = int(_redis.get(f"{prefix}:risks_found_total") or 0)
                critical_total = int(_redis.get(f"{prefix}:critical_total") or 0)
                stats["avg_risks_per_scan"] = round(risks_total / invocations, 1)
                stats["avg_critical_per_scan"] = round(critical_total / invocations, 1)

            if agent == "automation_agent":
                writes = int(_redis.get(f"{prefix}:writes_total") or 0)
                stats["total_redmine_writes"] = writes
                stats["avg_writes_per_call"] = round(writes / invocations, 1)

            result[agent] = stats

        except Exception as e:
            result[agent] = {"error": str(e)}

    return result


def get_metrics_from_audit_log(limit: int = 100) -> dict:
    """
    Parse the JSONL audit log and compute metrics from raw events.
    Use this for deeper analysis and to compare with Redis live counters.
    """
    from pathlib import Path
    from config import AUDIT_LOG_FILE

    log_path = Path(AUDIT_LOG_FILE)
    if not log_path.exists():
        return {"error": "No audit log found"}

    events = []
    for line in log_path.read_text(encoding="utf-8").strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            pass

    # Filter to agent_metrics events only
    metric_events = [e for e in events if e.get("event") == "agent_metrics"][-limit:]

    if not metric_events:
        return {"note": "No agent_metrics events yet. Run some queries first."}

    by_agent: dict[str, list] = {}
    for ev in metric_events:
        name = ev.get("agent", "unknown")
        by_agent.setdefault(name, []).append(ev)

    report = {}
    for agent, evs in by_agent.items():
        latencies = [e.get("latency_ms", 0) for e in evs]
        tool_counts = [e.get("tool_calls_count", 0) for e in evs]
        successes = [e.get("success", True) for e in evs]
        output_lens = [e.get("output_length", 0) for e in evs]

        latencies_sorted = sorted(latencies)
        n = len(latencies_sorted)

        report[agent] = {
            "sample_size": len(evs),
            "latency_ms": {
                "min": round(min(latencies), 0),
                "max": round(max(latencies), 0),
                "mean": round(sum(latencies) / n, 0),
                "p50": round(latencies_sorted[n // 2], 0),
                "p90": round(latencies_sorted[int(n * 0.9)], 0),
                "p95": round(latencies_sorted[int(n * 0.95)], 0),
            },
            "avg_tool_calls": round(sum(tool_counts) / n, 1),
            "success_rate_pct": round(100 * sum(successes) / n, 1),
            "avg_output_chars": round(sum(output_lens) / n, 0),
            "total_invocations": n,
        }

    return report


def _compute_health(
    avg_latency_ms: float,
    error_rate: float,
    avg_tool_calls: float,
    avg_redundant: float,
) -> str:
    """
    Compute an overall health rating for an agent based on its metrics.
    Returns: "good" | "warning" | "degraded"
    """
    issues = []
    if avg_latency_ms > 60000:
        issues.append("very slow")
    elif avg_latency_ms > 30000:
        issues.append("slow")
    if error_rate > 0.1:
        issues.append("high error rate")
    if avg_tool_calls > 5:
        issues.append("too many tool calls")
    if avg_redundant > 1:
        issues.append("redundant reads (context injection not working)")

    if not issues:
        return "good"
    elif len(issues) == 1 and issues[0] == "slow":
        return "warning"
    else:
        return f"degraded: {', '.join(issues)}"
