"""
supervisor.py — RedMind Supervisor (Subagents Pattern)

KEY FIX for dashboard rendering:
  The supervisor was receiving the JSON string from call_dashboard_agent and then
  passing it to the LLM, which re-summarized it into prose ("Here is your dashboard...").
  The frontend never saw the JSON, only text.

  Solution (from LangChain subagents docs pattern):
    call_dashboard_agent is marked with a special sentinel prefix so that
    run_supervisor() can detect it and return the raw JSON directly,
    bypassing the supervisor LLM's synthesis step entirely.

  This is NOT routing — the supervisor still decides WHICH tool to call.
  We only intercept the result AFTER the tool has already run.
"""

import hashlib
import json
import logging
import time

import mlflow
import redis as redis_lib
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents.automation_agent import run_automation_agent
from agents.dashboard_agent import run_dashboard_agent
from agents.data_agent import run_data_agent
from agents.risk_agent import run_risk_agent
from audit import log_event, TimedAudit
from config import (
    CACHE_TTL_LLM_RESPONSE,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PORT,
)
from llm import get_llm

logger = logging.getLogger(__name__)

# Sentinel prefix added to dashboard tool results so we can detect them
# in the final message list and return them verbatim to the frontend.
_DASHBOARD_SENTINEL = "__DASHBOARD_JSON__:"

# ── Redis ──────────────────────────────────────────────────────────────────────
try:
    _redis = redis_lib.Redis(
        host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    _redis.ping()
    logger.info("[SUPERVISOR] Redis connected")
except Exception as _e:
    logger.warning(f"[SUPERVISOR] Redis unavailable: {_e}")
    _redis = None


# ── Cache helpers ──────────────────────────────────────────────────────────────

def _make_cache_key(user_input: str, history: list) -> str:
    raw = json.dumps(
        {"q": user_input, "h": history[-4:] if history else []},
        sort_keys=True,
    )
    return "llm:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _cache_get(key: str) -> str | None:
    if not _redis:
        return None
    try:
        val = _redis.get(key)
        if val:
            logger.info(f"[LLM CACHE HIT] {key[:20]}…")
        return val
    except Exception:
        return None


def _cache_set(key: str, response: str) -> None:
    if not _redis:
        return
    try:
        _redis.setex(key, CACHE_TTL_LLM_RESPONSE, response)
        logger.info(f"[LLM CACHE SET] {key[:20]}… TTL={CACHE_TTL_LLM_RESPONSE}s")
    except Exception as e:
        logger.warning(f"[LLM CACHE SET ERROR]: {e}")


# ── Subagent tools ─────────────────────────────────────────────────────────────

@tool(
    "call_data_agent",
    description=(
        "Fetch live data from Redmine. Use for: issue lookups, counts, assignments, "
        "status checks, member lists, due dates, workload queries, listing issues by "
        "tracker/priority/assignee, 'show me all X', 'how many open bugs', 'what is "
        "Alice working on', 'show me issues without a due date', etc. "
        "Pure read — no write actions."
    ),
)
def call_data_agent(query: str) -> str:
    """Invoke the read-only Redmine data agent."""
    try:
        return run_data_agent(query)
    except Exception as e:
        logger.error(f"[DATA AGENT TOOL] failed: {e}")
        return (
            "I wasn't able to retrieve that data. "
            "Please check the issue or project exists and try again."
        )


@tool(
    "call_dashboard_agent",
    description=(
        "Generate visual dashboards, charts, KPIs, summaries, overviews, and health "
        "reports from Redmine data. Use for: 'summary', 'overview', 'report', "
        "'dashboard', 'workload distribution', 'how is the team performing', "
        "'team performance', 'priority breakdown', 'overdue report'. "
        "Returns structured JSON the frontend renders as charts."
    ),
)
def call_dashboard_agent(query: str) -> str:
    """
    Invoke the dashboard / visualization agent.

    Returns the raw dashboard JSON prefixed with a sentinel so that
    run_supervisor() can detect it and pass it straight to the frontend
    without the supervisor LLM re-summarizing it into plain text.
    """
    try:
        dashboard_json = run_dashboard_agent(query, history=[], session_id="supervisor")
        # Prefix with sentinel — stripped by run_supervisor before returning to caller
        return _DASHBOARD_SENTINEL + dashboard_json
    except Exception as e:
        logger.error(f"[DASHBOARD AGENT TOOL] failed: {e}")
        return _DASHBOARD_SENTINEL + json.dumps(
            {"type": "no_data", "message": f"Dashboard error: {e}"}
        )


@tool(
    "call_automation_agent",
    description=(
        "Perform write actions in Redmine: create issues/projects, update issues "
        "(status, assignee, priority, due date, progress), bulk-update, delete issues "
        "(requires confirmation), assign issues to team members. "
        "Use for any request that changes data: 'create a bug', 'assign #5 to Alice', "
        "'close all resolved issues', 'update issue #7 status to In Progress', "
        "'delete issue #3', 'create a project called X'."
    ),
)
def call_automation_agent(query: str) -> str:
    """Invoke the automation / write-action agent."""
    try:
        return run_automation_agent(query, history=[], session_id="supervisor")
    except Exception as e:
        logger.error(f"[AUTOMATION AGENT TOOL] failed: {e}")
        return (
            "⚠️ I encountered an error processing that action. "
            "Please check that the issue, project, or user exists in Redmine and try again."
        )


@tool(
    "call_risk_agent",
    description=(
        "Analyse project risks, blockers, and deadline concerns. Use for: "
        "'any risks?', 'overdue issues', 'who is behind schedule', 'blockers', "
        "'issues due in the next 3 days', 'stuck issues', 'unassigned work', "
        "'what new risks appeared today', 'full risk scan', 'overloaded team members'."
    ),
)
def call_risk_agent(query: str) -> str:
    """Invoke the risk-analysis agent."""
    try:
        return run_risk_agent(query, history=[], session_id="supervisor")
    except Exception as e:
        logger.error(f"[RISK AGENT TOOL] failed: {e}")
        return f"Risk analysis error: {e}"


# ── Supervisor agent ───────────────────────────────────────────────────────────

_SUPERVISOR_SYSTEM = """You are RedMind — an intelligent Redmine project management partner.

## SCOPE — ABSOLUTE RULE
You ONLY answer questions related to Redmine: projects, issues, team workload,
assignments, risks, deadlines, and project health.

Call each subagent AT MOST ONCE per turn. Never call the same subagent twice.

If the user asks ANYTHING outside this scope (general knowledge, coding help,
jokes, weather, math, etc.), respond EXACTLY with:
"I'm RedMind, your Redmine assistant. I can only help with Redmine projects,
issues, team workload, and project health."
Do NOT call any tools for off-topic questions. Do NOT try to be helpful outside this scope.

## YOUR IDENTITY
You understand what project managers *mean*, not just what they say.
You reason deeply, act decisively, and communicate clearly.

## YOUR TOOLS
You have four specialised subagents available as tools:

- call_data_agent        — read / fetch live Redmine data (issue lookups, counts,
                           assignments, status checks, member lists, workload queries)
- call_dashboard_agent   — visual summaries, charts, KPIs, health reports, team performance
- call_automation_agent  — write actions (create, update, delete, assign issues / projects)
- call_risk_agent        — risks, blockers, overdue issues, deadline concerns

## HOW TO WORK

1. **Use tools freely** — you MUST call at least one tool before answering unless the
   user is only greeting you or asking a meta question about your capabilities.
2. **Parallel calls** — when a request spans multiple domains, call the relevant tools
   in the same turn.
3. **Synthesize results** — combine everything into ONE clear, natural response.
4. **Be concise** — project managers are busy. Lead with the answer.

## SPECIAL RULE FOR DASHBOARD RESPONSES
When call_dashboard_agent is called, its result is chart data for the UI.
Simply confirm to the user that the dashboard is ready — do not describe the numbers.
Example: "Here's your team workload overview." — then stop.
The UI will render the charts automatically.

## RESPONSE RULES
- Maximum 5 sentences for direct answers.
- Use bullet points only for lists of actions or key metrics.
- Never mention tools, agents, or technical processes.
- Always end with one short forward-looking question.
- Never expose field names, null values, API flags, or implementation details.

## WHEN TO ACT WITHOUT ASKING
Execute clear commands directly. Only ask for clarification when:
- A user name cannot be resolved.
- Target issues cannot be identified.
- Action is destructive (delete / close-all) without explicit confirmation."""

_supervisor_agent = None


def _get_supervisor() -> object:
    global _supervisor_agent
    if _supervisor_agent is None:
        _supervisor_agent = create_agent(
            model=get_llm(),
            tools=[
                call_data_agent,
                call_dashboard_agent,
                call_automation_agent,
                call_risk_agent,
            ],
            system_prompt=_SUPERVISOR_SYSTEM,
            name="redmind_supervisor",
        )
        logger.info("[SUPERVISOR] Supervisor agent initialized")
    return _supervisor_agent


# ── Public entry point ─────────────────────────────────────────────────────────

def run_supervisor(
    user_input: str,
    history: list = None,
    session_id: str = "default",
) -> str:
    """
    Main entry point for every chat message.

    If the supervisor called call_dashboard_agent, we extract the raw JSON
    from the tool message and return it directly — skipping the supervisor
    LLM's synthesis step, which would otherwise convert it to plain text.
    """
    history = history or []
    start = time.perf_counter()

    with mlflow.start_run(run_name=f"supervisor_{session_id[:8]}"):
        mlflow.log_param("session_id", session_id)
        mlflow.log_param("user_input", user_input[:500])
        mlflow.log_param("history_length", len(history))

        # 1. Cache check
        cache_key = _make_cache_key(user_input, history)
        cached = _cache_get(cache_key)
        if cached:
            mlflow.log_metric("cache_hit", 1)
            mlflow.log_metric("latency_ms", (time.perf_counter() - start) * 1000)
            log_event(
                "supervisor_cache_hit",
                agent="supervisor",
                user_input=user_input,
                latency_ms=(time.perf_counter() - start) * 1000,
            )
            return cached

        mlflow.log_metric("cache_hit", 0)

        # 2. Build messages
        messages = []
        for msg in history[-4:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            messages.append(
                HumanMessage(content=content) if role == "user"
                else AIMessage(content=content)
            )
        messages.append(HumanMessage(content=user_input))

        # 3. Invoke supervisor
        result = None
        response: str
        _error_occurred = False

        try:
            with TimedAudit("supervisor_invoke", agent="supervisor", user_input=user_input):
                result = _get_supervisor().invoke(
                    {"messages": messages},
                    config={"recursion_limit": 30},
                )

            # ── KEY FIX: Check if a dashboard tool result is in the messages ──
            # The supervisor LLM re-summarizes tool output into prose, which
            # destroys the JSON structure the frontend needs.
            # We walk the messages and if we find a ToolMessage that came from
            # call_dashboard_agent (identified by the sentinel prefix), we return
            # that raw JSON directly — the supervisor's prose summary is discarded.
            dashboard_json = _find_dashboard_result(result.get("messages", []))

            if dashboard_json:
                response = dashboard_json
                logger.info("[SUPERVISOR] Returning raw dashboard JSON to frontend")
            else:
                response = result["messages"][-1].content

        except Exception as e:
            _error_occurred = True
            logger.error(f"[SUPERVISOR] Invocation failed: {e}")
            log_event("supervisor_error", agent="supervisor", error=str(e), success=False)
            mlflow.log_param("error", str(e)[:500])
            response = (
                "⚠️ I encountered an error processing your request. "
                "Please check that the issue, project, or user exists in Redmine and try again."
            )

        # 4. Extract tool calls used
        tool_calls_used: list[str] = []
        if result is not None:
            for msg in result.get("messages", []):
                if hasattr(msg, "tool_calls"):
                    for tc in (msg.tool_calls or []):
                        tool_calls_used.append(tc.get("name", ""))

        # 5. Cache only non-write responses
        contains_write = "call_automation_agent" in tool_calls_used
        if not contains_write and response and not _error_occurred:
            _cache_set(cache_key, response)

        # 6. Log metrics
        total_ms = (time.perf_counter() - start) * 1000
        mlflow.log_metric("latency_ms", total_ms)
        mlflow.log_metric("response_length", len(response))
        mlflow.log_metric("contains_write", int(contains_write))
        mlflow.log_param("tools_called", str(tool_calls_used))
        mlflow.log_text(user_input, "input.txt")
        mlflow.log_text(response, "output.txt")

        log_event(
            "supervisor_complete",
            agent="supervisor",
            user_input=user_input,
            latency_ms=total_ms,
            extra={"tools_called": tool_calls_used},
        )
        logger.info(
            f"[SUPERVISOR] {total_ms:.0f}ms | tools={tool_calls_used} | "
            f"write={contains_write}"
        )

        return response


def _find_dashboard_result(messages: list) -> str | None:
    """
    Walk the message list and return the raw dashboard JSON if call_dashboard_agent
    was invoked. Strips the sentinel prefix before returning.

    Returns None if no dashboard tool was called.
    """
    for msg in messages:
        if isinstance(msg, ToolMessage):
            content = msg.content
            if isinstance(content, str) and content.startswith(_DASHBOARD_SENTINEL):
                return content[len(_DASHBOARD_SENTINEL):]
    return None


def _clean_for_handoff(text: str) -> str:
    """Strip embedded JSON tails before passing to another agent or LLM."""
    lines = text.rstrip().split("\n")
    while lines and lines[-1].strip().startswith("{") and "risk_payload" in lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()
