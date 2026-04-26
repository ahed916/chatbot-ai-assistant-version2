"""
agents/risk_agent.py

Risk analysis agent built with create_agent (LangChain subagents pattern).

KEY FIX (vs previous version):
  proactive_risk_check() was making its own LLM call AND the scheduler was
  calling run_tools_for_project() + summarize_risk_results() which made
  another LLM call — every PM scan triggered TWO LLM calls per PM.

  Now proactive_risk_check() is a thin wrapper that delegates directly to
  run_tools_for_project() + summarize_risk_results(). The scheduler path
  (which is the only production caller) always goes through summarize_risk_results()
  exactly once — no duplication.

  The internal _llm_summary() helper is shared by both paths.

OTHER FIXES (kept from previous version):
  1. checked_at hash uses '|' separator to prevent collisions.
  2. Slack send failure delegates to slack_tools._post_to_slack (retry + DLQ).
"""

import hashlib
import json
import logging
import re

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage

from audit import log_event
from config import (
    AGENT_RECURSION_LIMIT,
    load_prompt,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PORT,
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL_ID,
)
from llm import get_llm
from tools.read_tools import READ_TOOLS
from tools.risk_tools import (
    clear_issue_cache,
    detect_long_running_issues,
    detect_milestone_risk,
    detect_no_due_date_issues,
    detect_overdue_issues,
    detect_overloaded_assignees,
    detect_stuck_issues,
    detect_unassigned_issues,
    detect_urgent_due_soon,
    run_full_risk_scan,
)
from tools.slack_tools import SLACK_TOOLS, send_slack_alert_for_pm

logger = logging.getLogger(__name__)

_REDIS_TTL = 86400  # 24 h

RISK_TOOLS = [
    run_full_risk_scan,
    detect_overdue_issues,
    detect_urgent_due_soon,
    detect_stuck_issues,
    detect_unassigned_issues,
    detect_no_due_date_issues,
    detect_overloaded_assignees,
    detect_milestone_risk,
    detect_long_running_issues,
]

ALL_TOOLS = RISK_TOOLS + READ_TOOLS + SLACK_TOOLS

# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are RedMind's Risk Agent — a project risk analyst for Redmine.

Your job: identify risks, blockers, and deadline concerns, then communicate them clearly.

WORKFLOW:
  1. Read the user query and identify what risk information is needed.
  2. Call the most relevant risk tool(s) to fetch live data.
  3. If the user explicitly asks to notify the team, OR you detect critical/high
     severity risks during a proactive scan, call send_slack_risk_alert.
     For simple informational queries, do NOT send Slack.
  4. Always end your response with the mandatory JSON payload described below.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT — MANDATORY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every response MUST end with a JSON payload on its own line (no markdown fences):

[Your plain-text analysis here, written like a senior PM]

{"risk_payload":{"critical_count":N,"overall_health":"Healthy|Needs Attention|At Risk|Critical","proactive_message":"2-sentence summary","recommendations":["action 1","action 2"]}}

RULES for the JSON tail:
- It MUST be the very last line of your response.
- No ```json``` fences around it.
- Use real numbers from the tool results.
- overall_health values: "Healthy", "Needs Attention", "At Risk", "Critical"
- critical_count: count of overdue + high-priority issues combined.
- If nothing is wrong: critical_count=0, overall_health="Healthy".
- The JSON line is stripped by the backend before showing the user.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RESPONSE STYLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- Speak like a senior PM, not an alarmist.
- Lead with the top findings. Use bullets for issue lists.
- Use exact numbers and issue IDs from tool results.
- Do NOT mention tools, agents, or internal processes.
- Do NOT include the JSON payload in your human-readable analysis."""

# ── Agent (lazy singleton) ─────────────────────────────────────────────────────

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        try:
            prompt = load_prompt("risk_agent")
        except Exception:
            prompt = _SYSTEM_PROMPT

        _agent = create_agent(
            model=get_llm(),
            tools=ALL_TOOLS,
            system_prompt=prompt,
            name="risk_agent",
        )
        logger.info("[RISK AGENT] Initialized")
    return _agent


# ── Public entry point ─────────────────────────────────────────────────────────

def run_risk_agent(
    query: str,
    history: list = None,
    session_id: str = "default",
) -> str:
    """
    Invoke the risk agent with a user query.
    Returns clean text for the user bubble.
    Extracts the JSON tail, fires bell alert + Slack when warranted.
    """
    history = history or []
    agent = _get_agent()

    messages = []
    for msg in history[-4:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        messages.append(
            HumanMessage(content=content) if role == "user"
            else AIMessage(content=content)
        )
    messages.append(HumanMessage(content=query))

    try:
        result = agent.invoke(
            {"messages": messages},
            config={"recursion_limit": AGENT_RECURSION_LIMIT},
        )
        result_messages = result.get("messages", [])
        raw = result_messages[-1].content if result_messages else ""

        text_part, risk_payload = _extract_risk_payload(raw)

        if risk_payload:
            _handle_alert(risk_payload, session_id)

        log_event("agent_response", agent="risk_agent", user_input=query)
        return text_part.strip()

    except Exception as e:
        logger.error(f"[RISK AGENT] Error: {e}")
        return f"Risk agent error: {e}"


# ── Alert handling ─────────────────────────────────────────────────────────────

def _handle_alert(payload: dict, session_id: str) -> None:
    critical_count = payload.get("critical_count", 0)
    overall_health = payload.get("overall_health", "Healthy")
    proactive_message = payload.get("proactive_message", "")
    recommendations = payload.get("recommendations", [])

    should_alert = critical_count > 0 or overall_health not in ("Healthy",)

    if should_alert:
        content_hash = hashlib.md5(
            f"{critical_count}|{overall_health}".encode()
        ).hexdigest()[:12]
        checked_at = f"risk_{content_hash}"

        alert_data = {
            "has_alert": True,
            "message": proactive_message,
            "critical_count": critical_count,
            "overall_health": overall_health,
            "recommendations": recommendations,
            "checked_at": checked_at,
            "slack_sent": False,
            "risks": [],
            "proactive_message": proactive_message,
        }

        _push_alert_to_cache(alert_data, session_id)
        logger.info(
            "[RISK AGENT] Bell alert queued: health=%s critical=%d",
            overall_health, critical_count,
        )

    slack_worthy = (
        critical_count > 0
        and SLACK_BOT_TOKEN
        and SLACK_CHANNEL_ID
    )
    if slack_worthy:
        slack_msg = (
            f"*{overall_health}* — {proactive_message}\n"
            f"Critical issues: {critical_count}"
        )
        send_slack_alert_for_pm(message=slack_msg, pm_name="")


def _push_alert_to_cache(alert_data: dict, session_id: str = "default") -> None:
    redis_key = f"proactive:risk:{session_id}"
    try:
        import redis as redis_lib
        r = redis_lib.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            db=REDIS_DB,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        r.setex(redis_key, _REDIS_TTL, json.dumps(alert_data))
        logger.info(f"[RISK AGENT] Alert written to Redis key='{redis_key}'")
        return
    except Exception as e:
        logger.warning(f"[RISK AGENT] Redis write failed, using memory: {e}")

    _IN_MEMORY_ALERT_CACHE[session_id] = alert_data


_IN_MEMORY_ALERT_CACHE: dict = {}


def get_cached_alert(session_id: str = "default") -> dict:
    return _IN_MEMORY_ALERT_CACHE.get(session_id, {}).copy()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_risk_payload(text: str) -> tuple[str, dict | None]:
    lines = text.rstrip().split("\n")
    last_line = lines[-1].strip()

    def try_parse(s: str) -> dict | None:
        try:
            parsed = json.loads(s)
            if "risk_payload" in parsed:
                return parsed["risk_payload"]
            if "critical_count" in parsed or "overall_health" in parsed:
                return parsed
        except Exception:
            pass
        return None

    payload = try_parse(last_line)
    if payload is not None:
        return "\n".join(lines[:-1]), payload

    match = re.search(
        r'\{[^{}]*"(?:risk_payload|critical_count|overall_health)"[^{}]*\}',
        text,
        re.DOTALL,
    )
    if match:
        payload = try_parse(match.group())
        if payload is not None:
            return text[: match.start()].strip(), payload

    clean_text = re.sub(
        r'\n*```json\s*\{.*?\}\s*```\s*$', '', text, flags=re.DOTALL
    ).strip()
    return clean_text, None


def _add_checked_at(parsed: dict) -> dict:
    try:
        content_hash = hashlib.md5(
            f"{parsed.get('critical_count', 0)}|{parsed.get('overall_health', 'Unknown')}".encode()
        ).hexdigest()[:12]
        parsed["checked_at"] = f"risk_{content_hash}"
        logger.info(f"[RISK AGENT] checked_at={parsed['checked_at']}")
    except Exception as e:
        logger.error(f"[RISK AGENT] _add_checked_at failed: {e}")
        parsed["checked_at"] = "risk_fallback_000000"
    return parsed


# ── Proactive background scan ──────────────────────────────────────────────────

def proactive_risk_check(project_id: str = "", pm_name: str = "") -> dict:
    """
    FIX: Previously this function ran its own tool calls AND its own LLM
    summarization call. The scheduler was ALSO calling run_tools_for_project()
    + summarize_risk_results() — causing 2 LLM calls per PM per cycle.

    Now this function is a thin wrapper: it collects tool output for the
    given project and delegates to summarize_risk_results() for the ONE
    LLM call. The scheduler calls this per-project and merges results itself,
    making exactly one summarize call across all projects via _run_scan_for_pm().

    For single-project live calls (e.g. from /api/proactive-risks?project_id=X),
    this still works correctly — one tool collection, one LLM call.
    """
    logger.info(
        "[RISK AGENT] Running proactive scan (project=%r pm=%s)...",
        project_id, pm_name or "system",
    )

    # Clear cache so this live scan gets fresh data
    clear_issue_cache()

    # Collect tool output (no LLM here)
    raw_text = run_tools_for_project(project_id=project_id)

    # ONE LLM call
    result = summarize_risk_results(combined_text=raw_text, pm_name=pm_name)

    log_event(
        "proactive_risk_complete",
        agent="risk_agent",
        extra={
            "pm_name": pm_name,
            "project_id": project_id,
            "critical_count": result["critical_count"],
            "slack_sent": result["slack_sent"],
            "overall_health": result["overall_health"],
            "checked_at": result["checked_at"],
        },
    )
    return result


def run_tools_for_project(project_id: str = "") -> str:
    """
    Run all risk detection tools for a single project and return raw text.
    NO LLM call — just tool results. Called by the scheduler to batch
    tool runs across multiple projects before making a single summarization call.

    FIX: Clears the per-project issue cache before running so each project
    gets a fresh fetch, but all 5 tools within that project share one fetch.
    """
    # Clear cache for this project so tools share a fresh fetch
    clear_issue_cache()

    checks = {
        "overdue":    (detect_overdue_issues,       {"project_id": project_id}),
        "urgent":     (detect_urgent_due_soon,      {"project_id": project_id, "days_threshold": 3}),
        "stuck":      (detect_stuck_issues,         {"project_id": project_id, "stale_days": 5}),
        "unassigned": (detect_unassigned_issues,    {"project_id": project_id}),
        "overloaded": (detect_overloaded_assignees, {"project_id": project_id, "threshold": 10}),
    }

    parts = []
    for name, (fn, args) in checks.items():
        try:
            result = fn.invoke(args)
            logger.info(f"[RISK AGENT] Tool '{name}' done (project={project_id!r})")
            parts.append(result)
        except Exception as e:
            parts.append(f"⚠️ Check '{name}' failed: {e}")
            logger.warning(f"[RISK AGENT] Tool '{name}' failed: {e}")

    return "\n\n".join(parts)


def summarize_risk_results(combined_text: str, pm_name: str = "") -> dict:
    """
    Make exactly ONE LLM call to summarize pre-collected tool output.
    Sends Slack if critical issues found.
    """
    risk_line_count = combined_text.count("RISK:")
    has_risks = risk_line_count > 0

    llm = get_llm()
    summary_prompt = f"""You are a project risk analyst. Based on the scan results below, produce:
1. proactive_message: 2-3 sentences suitable for a Slack alert
2. recommendations: up to 3 action items, each under 15 words
3. overall_health: one of "Healthy", "Needs Attention", "At Risk", "Critical"
4. critical_count: integer count of critical/overdue risks found

Scan results:
{combined_text}

Respond ONLY with valid JSON, no markdown fences:
{{"critical_count": 0, "overall_health": "...", "proactive_message": "...", "recommendations": ["..."]}}"""

    try:
        response = llm.invoke([HumanMessage(content=summary_prompt)])
        raw = response.content.strip()
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

    if parsed["critical_count"] > 0 and SLACK_BOT_TOKEN and SLACK_CHANNEL_ID:
        slack_msg = (
            f"*{parsed['overall_health']}* — {parsed['proactive_message']}\n"
            f"Critical issues: {parsed['critical_count']}"
        )
        result = send_slack_alert_for_pm(message=slack_msg, pm_name=pm_name)
        parsed["slack_sent"] = "successfully" in result.lower()
        logger.info(f"[RISK AGENT] Slack result: {result}")

    return parsed