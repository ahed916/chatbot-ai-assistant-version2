"""
agents/dashboard_agent.py

THE CORE PROBLEM THIS FILE SOLVES:
  Free models (steps=2, tools=0) often skip the tool call entirely.
  They read the prompt, understand they need to produce JSON, and write it
  directly as text instead of calling generate_dashboard_json.

  The JSON they produce is valid and correct — but it doesn't have
  "type": "dashboard" at the top level (they write the inner structure).
  So the frontend's isDashboardPayload() check fails.

SOLUTION — _normalize_output():
  After the agent responds, we inspect the output:
  1. If it already has type="dashboard" → pass through unchanged
  2. If it looks like dashboard JSON without the wrapper → add the wrapper
  3. If it's a tool result (comes through AIMessage tool_calls path) → extract it
  4. If none of the above → return as-is (plain text answer is still valid)
"""
import json
import logging
import time as _time
from datetime import datetime, timezone
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage

from config import load_prompt, AGENT_RECURSION_LIMIT
from llm import get_llm
from tools.chart_tools import CHART_TOOLS
from tools.read_tools import READ_TOOLS
from context_builder import inject_context
from audit import log_event

logger = logging.getLogger(__name__)

DASHBOARD_TOOLS = READ_TOOLS + CHART_TOOLS

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        prompt = load_prompt("dashboard_agent")
        _agent = create_react_agent(get_llm(), tools=DASHBOARD_TOOLS, prompt=prompt)
        logger.info("[DASHBOARD AGENT] Initialized")
    return _agent


def _normalize_output(raw: str) -> str:
    """
    Ensure the agent output is always a properly structured dashboard JSON
    with type="dashboard" at the top level.

    Handles 4 cases:
    1. Already correct: {"type": "dashboard", ...}  → return as-is
    2. Already correct: {"type": "quick_stat", ...} → return as-is
    3. Already correct: {"type": "no_data", ...}    → return as-is
    4. Model wrote inner JSON without wrapper:
       {"charts": [...], "kpis": [...], ...}        → wrap it
    5. JSON in code fences: ```json {...} ```        → extract and wrap
    6. Plain text                                    → return as-is
    """
    trimmed = raw.strip()

    # ── Strip code fences if present ─────────────────────────────────────────
    if "```" in trimmed:
        for pattern in ["```json\n", "```json", "```\n", "```"]:
            if pattern in trimmed:
                parts = trimmed.split(pattern)
                for part in parts:
                    candidate = part.split("```")[0].strip()
                    if candidate.startswith("{"):
                        trimmed = candidate
                        break

    # ── Try to parse as JSON ──────────────────────────────────────────────────
    if not trimmed.startswith("{"):
        # Not JSON — check if JSON is embedded in text after some preamble
        for marker in ['{"type":', '{"charts":', '{"kpis":']:
            idx = trimmed.find(marker)
            if idx != -1:
                trimmed = trimmed[idx:]
                break
        else:
            return raw  # genuinely plain text — return unchanged

    try:
        parsed = json.loads(trimmed)
    except json.JSONDecodeError:
        # Try to find the JSON object if there's trailing text
        brace_count = 0
        end_idx = 0
        for i, ch in enumerate(trimmed):
            if ch == "{":
                brace_count += 1
            elif ch == "}":
                brace_count -= 1
                if brace_count == 0:
                    end_idx = i + 1
                    break
        if end_idx > 0:
            try:
                parsed = json.loads(trimmed[:end_idx])
            except json.JSONDecodeError:
                return raw  # can't parse — return as-is
        else:
            return raw

    # ── Already has the right type field ─────────────────────────────────────
    if parsed.get("type") in ("dashboard", "quick_stat", "no_data"):
        # Re-serialize without indent (consistent format for frontend)
        return json.dumps(parsed, ensure_ascii=False)

    # ── Has charts/kpis but missing type wrapper — add it ────────────────────
    if "charts" in parsed or "kpis" in parsed:
        wrapped = {
            "type": "dashboard",
            "title": parsed.get("title", "Project Dashboard"),
            "generated_at": parsed.get("generated_at",
                                       datetime.now(timezone.utc).isoformat()),
            "summary": parsed.get("summary", ""),
            "kpis": parsed.get("kpis", []),
            "charts": parsed.get("charts", []),
        }
        logger.info("[DASHBOARD AGENT] Wrapped raw JSON into dashboard payload")
        return json.dumps(wrapped, ensure_ascii=False)

    # ── Has label/value — likely a quick_stat ─────────────────────────────────
    if "label" in parsed and "value" in parsed:
        wrapped = {
            "type": "quick_stat",
            "label": parsed.get("label", ""),
            "value": parsed.get("value", ""),
            "context": parsed.get("context", ""),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("[DASHBOARD AGENT] Wrapped raw JSON into quick_stat payload")
        return json.dumps(wrapped, ensure_ascii=False)

    # ── Unknown JSON shape — return as-is ────────────────────────────────────
    return raw


def run_dashboard_agent(query: str, project_identifier: str = None) -> str:
    """
    Run the dashboard agent.

    Flow:
      1. Build Redmine context in parallel (cached)
      2. Inject into query
      3. Agent invokes — either calls generate_dashboard_json (ideal)
         or writes JSON directly (handled by _normalize_output)
      4. Normalize output → always returns {"type":"dashboard",...} JSON
      5. Frontend detects type="dashboard" and renders DashboardCard
    """
    try:
        from metrics import MetricsCollector
    except ImportError:
        MetricsCollector = None

    agent = _get_agent()

    t_ctx = _time.perf_counter()
    enriched_query = inject_context(query, project_identifier)
    ctx_ms = (_time.perf_counter() - t_ctx) * 1000
    logger.info(f"[DASHBOARD AGENT] Context built in {ctx_ms:.0f}ms ({len(enriched_query)} chars)")

    # Reinforce the instruction at the END of the message (models read end more carefully)
    enriched_query += (
        "\n\n---\n"
        "REMINDER: Your response must be the JSON output of generate_dashboard_json. "
        "Either call the tool, OR write a JSON object starting with "
        '{"type": "dashboard", "title": ..., "kpis": [...], "charts": [...], "summary": "..."}. '
        "Do not write anything else."
    )

    ctx = MetricsCollector("dashboard_agent", query) if MetricsCollector else None
    if ctx:
        ctx.__enter__()
        ctx.record_context_build(ctx_ms, len(enriched_query))

    try:
        result = agent.invoke(
            {"messages": [HumanMessage(content=enriched_query)]},
            config={"recursion_limit": AGENT_RECURSION_LIMIT},
        )
        messages = result.get("messages", [])
        raw_output = messages[-1].content if messages else "Dashboard agent produced no output."

        # ── Normalize: handle both tool-call path and direct-JSON path ────────
        final = _normalize_output(raw_output)

        logger.info(
            f"[DASHBOARD AGENT] tools={sum(1 for m in messages if hasattr(m, 'tool_calls') and m.tool_calls)} "
            f"steps={len(messages)} "
            f"is_dashboard={'\"type\": \"dashboard\"' in final or '\"type\":\"dashboard\"' in final}"
        )

        if ctx:
            ctx.record_output(final)
            ctx.record_tool_calls(messages)
            ctx.__exit__(None, None, None)

        log_event("agent_response", agent="dashboard_agent",
                  user_input=query, tool_result=final[:200])
        return final

    except Exception as e:
        if ctx:
            ctx.__exit__(type(e), e, None)
        logger.error(f"[DASHBOARD AGENT] Error: {e}")
        return f"Dashboard agent error: {e}"
