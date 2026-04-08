"""
agents/risk_agent.py

Risk Monitor Agent — proactively scans Redmine for risks.

Key fix from v1:
  - inject_context() pre-loads all issue/member/workload data before the
    agent starts, so it can reason about risks immediately without making
    5+ read tool calls first.
  - AGENT_RECURSION_LIMIT (iterations × 2) prevents "need more steps".
  - proactive_risk_check() still available for scheduled background scans.
"""
import json
import logging
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage

from config import load_prompt, AGENT_RECURSION_LIMIT
from llm import get_llm
from tools.read_tools import READ_TOOLS
from tools.slack_tools import SLACK_TOOLS
from context_builder import inject_context
from audit import log_event, TimedAudit

import hashlib  # ← ADD THIS
from datetime import datetime, timezone  # ← ADD timezone


logger = logging.getLogger(__name__)

RISK_TOOLS = READ_TOOLS + SLACK_TOOLS

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        prompt = load_prompt("risk_agent")
        _agent = create_react_agent(get_llm(), tools=RISK_TOOLS, prompt=prompt)
        logger.info("[RISK AGENT] Initialized")
    return _agent


def run_risk_agent(query: str, project_identifier: str = None) -> str:
    from metrics import MetricsCollector
    import time as _time

    agent = _get_agent()

    t_ctx = _time.perf_counter()
    enriched_query = inject_context(query, project_identifier)
    ctx_ms = (_time.perf_counter() - t_ctx) * 1000

    with MetricsCollector("risk_agent", query) as mc:
        mc.record_context_build(ctx_ms, len(enriched_query))
        try:
            result = agent.invoke(
                {"messages": [HumanMessage(content=enriched_query)]},
                config={"recursion_limit": AGENT_RECURSION_LIMIT},
            )
            messages = result.get("messages", [])
            final = messages[-1].content if messages else "Risk agent produced no output."

            # Strip the trailing JSON block — it's for proactive mode only,
            # not meant to be shown to the user in interactive chat
            final = _strip_trailing_json(final)

            mc.record_output(final)
            mc.record_tool_calls(messages)
            log_event("agent_response", agent="risk_agent", user_input=query)
            return final
        except Exception as e:
            logger.error(f"[RISK AGENT] Error: {e}")
            return f"Risk agent error: {e}"


def _strip_trailing_json(text: str) -> str:
    """
    Remove the trailing JSON metadata block the model appends for proactive mode.
    Keeps all the human-readable risk analysis, strips the raw JSON at the end.
    """
    import re

    # Remove ```json ... ``` code block at the end
    text = re.sub(r"\n*```json\s*\{.*?\}\s*```\s*$", "", text, flags=re.DOTALL).strip()

    # Remove bare { ... } JSON object at the very end (no code fences)
    text = re.sub(r"\n*\{[\s\S]*\"overall_health\"[\s\S]*\}\s*$", "", text).strip()

    return text


def _add_checked_at(parsed: dict) -> dict:
    """
    Generate a STABLE identifier for deduplication.
    Identical risk states → identical checked_at, regardless of time.
    """
    import hashlib
    from datetime import datetime, timezone

    # 🔍 DEBUG LOG
    logger.info(
        f"[DEBUG _add_checked_at] Input: critical_count={parsed.get('critical_count')}, health={parsed.get('overall_health')}")

    try:
        # Hash only stable fields (ignore timestamps, messages, etc.)
        content_hash = hashlib.md5(
            f"{parsed.get('critical_count', 0)}{parsed.get('overall_health', 'Unknown')}".encode()
        ).hexdigest()[:12]

        # Stable prefix + hash. Changes ONLY when risk state changes.
        parsed["checked_at"] = f"risk_{content_hash}"

        # 🔍 DEBUG LOG
        logger.info(f"[DEBUG _add_checked_at] ✅ Set checked_at={parsed['checked_at']}")

    except Exception as e:
        # 🔍 DEBUG LOG
        logger.error(f"[DEBUG _add_checked_at] ❌ Failed: {e}")
        # Fallback: still set something so frontend doesn't get null
        parsed["checked_at"] = "risk_fallback_000000"

    return parsed


def proactive_risk_check(project_id: str = "") -> dict:
    """Proactive background scan — called by the APScheduler every N hours."""
    from metrics import MetricsCollector
    import time as _time

    agent = _get_agent()

    base_query = (
        "Perform a full proactive risk scan. "
        "Analyze the context above: look at overdue issues, workload imbalance, "
        "unassigned high-priority issues, and stalled work. "
        "Identify every risk. If critical_count > 0, send a Slack notification. "
        "Return structured JSON with: risks (list), critical_count (int), "
        "overall_health (string), proactive_message (string), recommendations (list)."
    )

    t_ctx = _time.perf_counter()
    enriched_query = inject_context(base_query, project_id or None)
    ctx_ms = (_time.perf_counter() - t_ctx) * 1000

    with MetricsCollector("risk_agent", base_query) as mc:
        mc.record_context_build(ctx_ms, len(enriched_query))
        try:
            result = agent.invoke(
                {"messages": [HumanMessage(content=enriched_query)]},
                config={"recursion_limit": AGENT_RECURSION_LIMIT},
            )
            messages = result.get("messages", [])
            raw = messages[-1].content if messages else ""
            parsed = _parse_risk_response(raw)

            # ✅ ADD: Always set checked_at BEFORE returning
            parsed = _add_checked_at(parsed)

            mc.record_output(raw)
            mc.record_tool_calls(messages)
            mc.record_risk_result(parsed)

            log_event(
                "proactive_risk_complete", agent="risk_agent",
                extra={
                    "critical_count": parsed["critical_count"],
                    "slack_sent": parsed["slack_sent"],
                    "overall_health": parsed.get("overall_health", "unknown"),
                    "checked_at": parsed["checked_at"],  # ← Log for tracing
                },
            )
            return parsed  # ✅ Now includes checked_at

        except Exception as e:
            logger.error(f"[RISK AGENT] Proactive check failed: {e}")
            # ✅ ADD: checked_at even in error case
            error_result = {
                "critical_count": 0,
                "proactive_message": f"Risk scan encountered an error: {e}",
                "slack_sent": False,
                "raw_analysis": str(e),
                "overall_health": "Unknown",
            }
            return _add_checked_at(error_result)  # ✅ Ensure checked_at is set


def _parse_risk_response(raw: str) -> dict:
    """Parse agent response — tries JSON first, falls back to text analysis."""
    try:
        cleaned = raw
        if "```json" in raw:
            cleaned = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            cleaned = raw.split("```")[1].split("```")[0].strip()
        data = json.loads(cleaned)
        return {
            "critical_count": data.get("critical_count", 0),
            "proactive_message": data.get("proactive_message", raw[:500]),
            "slack_sent": data.get("slack_sent", False),
            "raw_analysis": raw,
            "overall_health": data.get("overall_health", "Unknown"),
            "risks": data.get("risks", []),
            "recommendations": data.get("recommendations", []),
        }
    except (json.JSONDecodeError, IndexError, KeyError):
        pass

    # Fallback: infer criticality from text keywords
    keywords = ["critical", "overdue", "urgent", "overloaded", "risk", "danger", "blocker"]
    count = sum(1 for kw in keywords if kw in raw.lower())
    return {
        "critical_count": min(count, 10),
        "proactive_message": raw[:1000],
        "slack_sent": False,
        "raw_analysis": raw,
        "overall_health": "At Risk" if count > 0 else "Healthy",
        "risks": [],
        "recommendations": [],
    }
