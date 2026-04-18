"""
agents/risk_agent.py

Risk analysis agent built with create_agent (LangChain subagents pattern).

Bell alerts now work because _push_alert_to_cache() writes to the SAME
Redis key ("proactive:risk:latest") that /api/proactive-risks reads.

Previously the alert pipeline called set_proactive_risk_cache() which wrote
to a different key (or a dead in-memory dict), so Slack fired but the bell
never lit up. No routing logic anywhere — the LLM-embedded JSON payload
drives everything.

HOW IT WORKS (no routing):
  - The system prompt tells the LLM to ALWAYS append a small JSON payload at
    the end of its reply, regardless of the query type.
  - run_risk_agent() splits the response: text → returned to caller,
    JSON → alert pipeline.
  - The alert pipeline decides whether to ring the bell and/or send Slack
    based on the payload's critical_count — no if/elif dispatch anywhere.

proactive_risk_check() is kept as a direct tool-call function (not
agent-based) because it runs as a background APScheduler job.
"""

import hashlib
import json
import logging
import re

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage

import mlflow

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
from tools.slack_tools import SLACK_TOOLS, send_slack_risk_alert

logger = logging.getLogger(__name__)

# Redis key consumed by GET /api/proactive-risks  (must match main.py)
_REDIS_RISK_KEY = "proactive:risk:latest"
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

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL SELECTION GUIDE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"any risks?" / "full risk scan" / "project health"  → run_full_risk_scan
"overdue" / "late" / "past deadline"                → detect_overdue_issues
"due soon" / "deadline" / "upcoming"                → detect_urgent_due_soon
"stuck" / "stale" / "no progress"                  → detect_stuck_issues
"unassigned" / "no owner"                          → detect_unassigned_issues
"overloaded" / "workload" / "too many tasks"        → detect_overloaded_assignees
"long running" / "oldest" / "open too long"         → detect_long_running_issues
"milestone" / "crunch" / "deadline cluster"         → detect_milestone_risk

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLACK ALERT RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Call send_slack_risk_alert ONLY when:
  - The user explicitly asks to notify the team / send an alert.
  - You detect CRITICAL/HIGH severity risks AND the query implies a proactive scan
    ("scan for risks", "any blockers?", "run a risk check").

Do NOT send Slack for simple informational queries ("how many overdue?").

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
    No routing logic — the LLM payload drives everything.

    Follows the LangChain subagents pattern: the sub-agent is invoked,
    its last message content is returned to the supervisor as plain text,
    and side effects (bell + Slack) are handled here transparently.
    """
    history = history or []
    agent = _get_agent()

    # Build message list per LangChain subagents docs pattern
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

        # Split text from the embedded JSON payload
        text_part, risk_payload = _extract_risk_payload(raw)

        # Fire bell alert + conditionally Slack — no routing, payload decides
        if risk_payload:
            _handle_alert(risk_payload, session_id)

        log_event("agent_response", agent="risk_agent", user_input=query)
        return text_part.strip()

    except Exception as e:
        logger.error(f"[RISK AGENT] Error: {e}")
        return f"Risk agent error: {e}"


# ── Alert handling (no routing) ───────────────────────────────────────────────

def _handle_alert(payload: dict, session_id: str) -> None:
    """
    Given a risk_payload dict, decide whether to push a bell alert and/or
    send a Slack message.  No if/elif routing — the payload fields drive this.

    THE FIX: _push_alert_to_cache() now writes to "proactive:risk:latest",
    the exact Redis key that GET /api/proactive-risks reads.  Previously it
    called set_proactive_risk_cache() which wrote to a different key, so the
    bell endpoint never saw chat-triggered alerts.
    """
    critical_count = payload.get("critical_count", 0)
    overall_health = payload.get("overall_health", "Healthy")
    proactive_message = payload.get("proactive_message", "")
    recommendations = payload.get("recommendations", [])

    # Bell alert: push whenever there is something to report
    should_alert = critical_count > 0 or overall_health not in ("Healthy",)

    if should_alert:
        content_hash = hashlib.md5(
            f"{critical_count}{overall_health}".encode()
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
            "proactive_message": proactive_message,  # kept for scheduler compat
        }

        _push_alert_to_cache(alert_data)
        logger.info(
            f"[RISK AGENT] Bell alert queued: "
            f"health={overall_health} critical={critical_count}"
        )

    # Slack: only when critical AND Slack is configured
    slack_worthy = (
        critical_count > 0
        and SLACK_BOT_TOKEN
        and SLACK_CHANNEL_ID
    )
    if slack_worthy:
        try:
            slack_msg = (
                f"🚨 RedMind Risk Alert — {overall_health}\n"
                f"{proactive_message}\n"
                f"Critical issues: {critical_count}"
            )
            result = send_slack_risk_alert.invoke({
                "message": slack_msg,
                "channel_id": SLACK_CHANNEL_ID,
            })
            logger.info(f"[RISK AGENT] Slack sent: {result}")
        except Exception as e:
            logger.error(f"[RISK AGENT] Slack failed: {e}")


def _push_alert_to_cache(alert_data: dict) -> None:
    """
    Write the alert to Redis under "proactive:risk:latest" — the SAME key
    that GET /api/proactive-risks reads.  This is the fix: previously this
    function called set_proactive_risk_cache() which wrote to a different
    key, so chat-triggered alerts never reached the bell endpoint.

    Falls back to an in-memory store when Redis is unavailable.
    """
    # ── Primary path: Redis (same key as scheduler + main.py endpoint) ──────
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
        r.setex(_REDIS_RISK_KEY, _REDIS_TTL, json.dumps(alert_data))
        logger.info(
            f"[RISK AGENT] Alert written to Redis key='{_REDIS_RISK_KEY}'"
        )
        return
    except Exception as e:
        logger.warning(f"[RISK AGENT] Redis write failed, using memory: {e}")

    # ── Fallback: in-memory dict (read by get_cached_alert() below) ──────────
    _IN_MEMORY_ALERT_CACHE.update(alert_data)
    logger.debug("[RISK AGENT] Alert stored in memory cache")


# Module-level fallback cache (used when Redis is unavailable)
_IN_MEMORY_ALERT_CACHE: dict = {}


def get_cached_alert() -> dict:
    """Called by /api/proactive-risks when Redis is unavailable."""
    return _IN_MEMORY_ALERT_CACHE.copy()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _extract_risk_payload(text: str) -> tuple[str, dict | None]:
    """
    Pull the JSON tail off the agent's response.
    Returns (clean_text, payload_dict | None).

    Handles both:
      {"risk_payload": {...}}       ← new format
      {"critical_count": N, ...}    ← old proactive format (backwards compat)
    """
    lines = text.rstrip().split("\n")
    last_line = lines[-1].strip()

    def try_parse(s: str) -> dict | None:
        try:
            parsed = json.loads(s)
            if "risk_payload" in parsed:
                return parsed["risk_payload"]
            if "critical_count" in parsed or "overall_health" in parsed:
                return parsed  # backwards compat
        except Exception:
            pass
        return None

    # Fast path: last line is the payload
    payload = try_parse(last_line)
    if payload is not None:
        clean_text = "\n".join(lines[:-1])
        return clean_text, payload

    # Fallback: regex scan for embedded JSON
    match = re.search(
        r'\{[^{}]*"(?:risk_payload|critical_count|overall_health)"[^{}]*\}',
        text,
        re.DOTALL,
    )
    if match:
        payload = try_parse(match.group())
        if payload is not None:
            clean_text = text[: match.start()].strip()
            return clean_text, payload

    # No payload found — strip any accidental JSON fences
    clean_text = re.sub(
        r'\n*```json\s*\{.*?\}\s*```\s*$', '', text, flags=re.DOTALL
    ).strip()
    return clean_text, None


def _add_checked_at(parsed: dict) -> dict:
    try:
        content_hash = hashlib.md5(
            f"{parsed.get('critical_count', 0)}"
            f"{parsed.get('overall_health', 'Unknown')}".encode()
        ).hexdigest()[:12]
        parsed["checked_at"] = f"risk_{content_hash}"
        logger.info(f"[RISK AGENT] checked_at={parsed['checked_at']}")
    except Exception as e:
        logger.error(f"[RISK AGENT] _add_checked_at failed: {e}")
        parsed["checked_at"] = "risk_fallback_000000"
    return parsed


# ── Proactive background scan (scheduler job — NOT user-facing) ────────────────

def proactive_risk_check(project_id: str = "") -> dict:
    """
    Background scan called by APScheduler every N hours.
    Calls risk tools directly, then ONE LLM call to summarize.
    Sends a Slack alert if critical risks are found.
    Returns a dict; the scheduler in main.py writes it to Redis under
    "proactive:risk:latest" (same key _push_alert_to_cache uses).
    """
    logger.info("[RISK AGENT] Running proactive scan (direct tool calls)...")

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

    combined_text = "\n\n".join(tool_results.values())
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
        try:
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

    log_event(
        "proactive_risk_complete",
        agent="risk_agent",
        extra={
            "critical_count": parsed["critical_count"],
            "slack_sent": parsed["slack_sent"],
            "overall_health": parsed["overall_health"],
            "checked_at": parsed["checked_at"],
        },
    )
    # Note: the scheduler in main.py writes this return value to Redis
    # under "proactive:risk:latest".  _push_alert_to_cache() uses the same
    # key, so chat-triggered alerts and scheduled scans share one source of
    # truth for the bell endpoint.
    return parsed
