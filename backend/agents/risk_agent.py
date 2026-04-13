"""
agents/risk_agent.py
"""
import json
import logging
import hashlib
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage, AIMessage

from config import load_prompt, AGENT_RECURSION_LIMIT
from llm import get_llm
from tools.read_tools import READ_TOOLS
from tools.slack_tools import SLACK_TOOLS
from context_builder import inject_context
from audit import log_event

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


def run_risk_agent(query: str, history: list = None, session_id: str = "default") -> str:
    history = history or []

    # ── Fast path: known query → direct tool call + one LLM summary ──────────
    direct = _try_direct_dispatch(query)
    if direct is not None:
        log_event("agent_response", agent="risk_agent", user_input=query)
        return direct

    # ── Slow path: full ReAct loop (only for complex/ambiguous queries) ───────
    agent = _get_agent()
    project_identifier = None
    enriched_query = inject_context(query, project_identifier)

    messages = []
    for msg in history[-4:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        messages.append(
            HumanMessage(content=content) if role == "user" else AIMessage(content=content)
        )
    messages.append(HumanMessage(content=enriched_query))

    try:
        result = agent.invoke(
            {"messages": messages},
            config={"recursion_limit": AGENT_RECURSION_LIMIT},
        )
        result_messages = result.get("messages", [])
        final = result_messages[-1].content if result_messages else "Risk agent produced no output."
        final = _strip_trailing_json(final)
        log_event("agent_response", agent="risk_agent", user_input=query)
        return final
    except Exception as e:
        logger.error(f"[RISK AGENT] Error: {e}")
        return f"Risk agent error: {e}"


def _strip_trailing_json(text: str) -> str:
    import re
    text = re.sub(r"\n*```json\s*\{.*?\}\s*```\s*$", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"\n*\{[\s\S]*\"overall_health\"[\s\S]*\}\s*$", "", text).strip()
    return text


def _add_checked_at(parsed: dict) -> dict:
    try:
        content_hash = hashlib.md5(
            f"{parsed.get('critical_count', 0)}{parsed.get('overall_health', 'Unknown')}".encode()
        ).hexdigest()[:12]
        parsed["checked_at"] = f"risk_{content_hash}"
        logger.info(f"[RISK AGENT] checked_at={parsed['checked_at']}")
    except Exception as e:
        logger.error(f"[RISK AGENT] _add_checked_at failed: {e}")
        parsed["checked_at"] = "risk_fallback_000000"
    return parsed


def proactive_risk_check(project_id: str = "") -> dict:
    """
    Proactive background scan — called by the APScheduler every N hours.
    Calls risk tools directly (no agent loop) then makes ONE LLM call to summarize.
    This avoids the multi-step agent timeout on OpenRouter.
    """
    import time as _time
    from config import SLACK_BOT_TOKEN, SLACK_CHANNEL_ID
    from tools.risk_tools import (
        detect_overdue_issues,
        detect_urgent_due_soon,
        detect_stuck_issues,
        detect_unassigned_issues,
        detect_overloaded_assignees,
    )
    global _last_alert_text

    logger.info("[RISK AGENT] Running direct proactive scan (no agent loop)...")

    # ── Step 1: Run all risk tools directly ──────────────────────────────────
    tool_results = {}
    checks = {
        "overdue": (detect_overdue_issues, {"project_id": project_id}),
        "urgent": (detect_urgent_due_soon, {"project_id": project_id, "days_threshold": 3}),
        "stuck": (detect_stuck_issues, {"project_id": project_id, "stale_days": 5}),
        "unassigned": (detect_unassigned_issues, {"project_id": project_id}),
        "overloaded": (detect_overloaded_assignees, {"project_id": project_id, "threshold": 10}),
    }

    for name, (fn, args) in checks.items():
        try:
            tool_results[name] = fn.invoke(args)
            logger.info(f"[RISK AGENT] Tool '{name}' done")
        except Exception as e:
            tool_results[name] = f"⚠️ Check failed: {e}"
            logger.warning(f"[RISK AGENT] Tool '{name}' failed: {e}")

    # ── Step 2: Count risks from tool output ─────────────────────────────────
    combined_text = "\n\n".join(tool_results.values())
    risk_line_count = combined_text.count("RISK:")
    has_risks = risk_line_count > 0

    # ── Step 3: Single LLM call to produce a human summary + JSON ────────────
    llm = get_llm()
    summary_prompt = f"""You are a project risk analyst. Based on the scan results below, write:
1. A concise proactive_message (2-3 sentences, suitable for a Slack alert)
2. A list of top recommendations (max 3, each under 15 words)
3. overall_health: one of "Healthy", "Needs Attention", "At Risk", "Critical"
4. critical_count: integer count of critical/overdue risks found

Scan results:
{combined_text}

Respond ONLY with valid JSON, no markdown:
{{"critical_count": 0, "overall_health": "...", "proactive_message": "...", "recommendations": ["..."]}}"""

    try:
        response = llm.invoke([HumanMessage(content=summary_prompt)])
        raw = response.content.strip()
        # Strip markdown fences if present
        if "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
        data = json.loads(raw)
        parsed = {
            "critical_count": data.get("critical_count", 0),
            "overall_health": data.get("overall_health", "Unknown"),
            "proactive_message": data.get("proactive_message", combined_text[:500]),
            "recommendations": data.get("recommendations", []),
            "slack_sent": False,
            "risks": [],
            "raw_analysis": combined_text,
        }
    except Exception as e:
        logger.error(f"[RISK AGENT] LLM summary failed: {e}")
        # Fallback: derive health from tool output without LLM
        parsed = {
            "critical_count": risk_line_count,
            "overall_health": "At Risk" if has_risks else "Healthy",
            "proactive_message": combined_text[:500],
            "recommendations": [],
            "slack_sent": False,
            "risks": [],
            "raw_analysis": combined_text,
        }

    parsed = _add_checked_at(parsed)

    # ── Step 4: Send Slack alert if configured and risks found ────────────────
    if parsed["critical_count"] > 0 and SLACK_BOT_TOKEN and SLACK_CHANNEL_ID:
        try:
            from tools.slack_tools import send_slack_risk_alert
            slack_msg = (
                f"🚨 RedMind Risk Alert — {parsed['overall_health']}\n"
                f"{parsed['proactive_message']}\n"
                f"Critical issues: {parsed['critical_count']}"
            )
            result = send_slack_risk_alert.invoke({
                "message": slack_msg,
                "channel_id": SLACK_CHANNEL_ID,
            })
            parsed["slack_sent"] = "successfully" in result.lower()
            logger.info(f"[RISK AGENT] Slack result: {result}")
        except Exception as e:
            logger.error(f"[RISK AGENT] Slack send failed: {e}")

    _last_alert_text = slack_msg

    log_event(
        "proactive_risk_complete", agent="risk_agent",
        extra={
            "critical_count": parsed["critical_count"],
            "slack_sent": parsed["slack_sent"],
            "overall_health": parsed["overall_health"],
            "checked_at": parsed["checked_at"],
        },
    )
    logger.info(
        f"[RISK AGENT] Scan complete — health={parsed['overall_health']} "
        f"critical={parsed['critical_count']} slack={parsed['slack_sent']}"
    )
    return parsed


def _parse_risk_response(raw: str) -> dict:
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


_DIRECT_DISPATCH = [
    # (keywords that trigger it, tool_fn, kwargs)
    ({"longest", "unresolved", "long-running", "long running", "oldest", "open longest"},
     "detect_long_running_issues", {"project_id": "", "max_days": 1}),
    ({"today", "new risk", "appeared today", "new today", "recent risk"},
     "detect_long_running_issues", {"project_id": "", "max_days": 0}),  # overridden below
    ({"overdue"},
     "detect_overdue_issues", {"project_id": ""}),
    ({"stuck", "stale", "no progress", "not updated"},
     "detect_stuck_issues", {"project_id": "", "stale_days": 5}),
    ({"unassigned", "no assignee"},
     "detect_unassigned_issues", {"project_id": ""}),
    ({"overloaded", "too many tasks", "workload"},
     "detect_overloaded_assignees", {"project_id": "", "threshold": 10}),
    ({"urgent", "due soon", "deadline"},
     "detect_urgent_due_soon", {"project_id": "", "days_threshold": 3}),
    ({"changed since", "anything change", "improved since", "still the same", "update since alert"},
     "detect_overdue_issues", {"project_id": ""}),
    ({"summarize the alert", "what was the alert", "what did you send", "alert you sent", "slack alert"},
     "detect_overdue_issues", {"project_id": ""}),  # re-runs current scan as proxy for alert content
]


def _try_direct_dispatch(query: str) -> str | None:
    """
    If the query maps to a single known risk tool, call it directly and
    summarize with ONE LLM call. Returns None if no match.
    """
    from tools.risk_tools import (
        detect_long_running_issues,
        detect_overdue_issues,
        detect_stuck_issues,
        detect_unassigned_issues,
        detect_overloaded_assignees,
        detect_urgent_due_soon,
    )
    tool_map = {
        "detect_long_running_issues": detect_long_running_issues,
        "detect_overdue_issues": detect_overdue_issues,
        "detect_stuck_issues": detect_stuck_issues,
        "detect_unassigned_issues": detect_unassigned_issues,
        "detect_overloaded_assignees": detect_overloaded_assignees,
        "detect_urgent_due_soon": detect_urgent_due_soon,
    }

    q_lower = query.lower()

    # Special case: "today" / "new risks" → issues created today (age = 0 days)
    if any(kw in q_lower for kw in {"today", "appeared today", "new today", "new risk", "recent risk"}):
        from datetime import date
        raw = detect_long_running_issues.invoke({"project_id": "", "max_days": 0})
        # Filter to only issues created today by checking age in the raw output
        # Simpler: just summarize the full list and ask LLM to highlight new ones
        return _summarize_tool_output(query, raw)

    if any(kw in q_lower for kw in {"summarize the alert", "what was the alert", "alert you sent", "what did you send"}):
        if _last_alert_text:
            return _summarize_tool_output(query, _last_alert_text)
        return "No alert has been sent in this session yet."

    for keywords, tool_name, kwargs in _DIRECT_DISPATCH:
        if any(kw in q_lower for kw in keywords):
            fn = tool_map.get(tool_name)
            if fn:
                logger.info(f"[RISK AGENT] Direct dispatch → {tool_name}")
                raw = fn.invoke(kwargs)
                return _summarize_tool_output(query, raw)

    return None


def _summarize_tool_output(query: str, tool_output: str) -> str:
    """One LLM call to turn raw tool output into a human answer."""
    llm = get_llm()
    prompt = f"""You are RedMind's risk analyst. The user asked: "{query}"

Here is the raw data from the risk detection tool:

{tool_output}

Write a clear, concise answer (3-6 sentences or a short bullet list).
- Speak like a senior PM, not an alarmist.
- Focus on the top findings only.
- Do NOT mention tools, agents, or internal processes.
- Do NOT append JSON."""

    try:
        response = llm.invoke([HumanMessage(content=prompt)])
        return response.content.strip()
    except Exception as e:
        logger.error(f"[RISK AGENT] Summary LLM failed: {e}")
        return tool_output[:1000]  # fallback: raw output truncated
