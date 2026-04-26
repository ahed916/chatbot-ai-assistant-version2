"""
supervisor.py — RedMind Supervisor (Subagents Pattern)

Changes vs previous version:

  - Removed all CONFIRMATION_SENTINEL / _pending_delete / _pending_bulk_delete
    imports and references. The automation agent now handles confirmation
    internally via LangGraph interrupt() — the supervisor needs no special-casing.

  - Removed the confirmation-prompt sentinel detection in run_supervisor().
    _find_tool_result_by_sentinel() is kept only for dashboard JSON pass-through.

  - Removed the cache bypass that skipped caching when a pending delete was
    detected. Automation responses are never cached (contains_write guard
    already handles this correctly).

  - Removed the stable _automation_session_id ContextVar — it was only needed
    to key _pending_delete/bulk across turns. The automation agent now uses its
    own LangGraph thread_id (keyed on session_id) for interrupt state persistence.

  - Simplified the duplicate-write guard: no longer exempts a second
    call_automation_agent invocation for "confirmation reply" reasons, because
    there is no longer a turn where the supervisor calls the automation agent twice.
"""

import hashlib
import json
import logging
import time
from contextvars import ContextVar

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

# Sentinel prefix for dashboard JSON — returned verbatim to frontend
_DASHBOARD_SENTINEL = "__DASHBOARD_JSON__:"

# ContextVar: session_id passed to call_automation_agent tool
_session_id_var: ContextVar[str] = ContextVar("session_id", default="default")

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
    """
    Invoke the automation / write-action agent.

    The automation agent manages its own LangGraph thread state (keyed on
    session_id) so interrupt() / Command(resume=) for delete confirmation
    works transparently across turns — no supervisor involvement required.
    """
    session_id = _session_id_var.get()
    try:
        return run_automation_agent(query, history=[], session_id=session_id)
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

## WRITE ACTIONS — STRICT RULES
- Call call_automation_agent EXACTLY ONCE per turn. Never call it a second time.
- If the automation agent returns an error or validation failure, report that error
  to the user as-is. Do NOT retry, do NOT call call_automation_agent again.
- Retrying write actions causes duplicate records (e.g. the same issue created twice).
- If the automation agent returns a confirmation prompt (starts with ⚠️), relay it
  to the user EXACTLY as written. Do NOT add any text before or after it.

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

    Sets _session_id_var so call_automation_agent can forward the correct
    session_id to run_automation_agent() for LangGraph thread scoping.

    After the supervisor runs, checks ToolMessages for the dashboard sentinel
    (passed directly to frontend without LLM synthesis). Automation confirmation
    prompts are now plain text returned by the automation agent — no special
    sentinel detection needed.
    """
    history = history or []
    start = time.perf_counter()

    # Make session_id available to call_automation_agent tool
    _session_id_var.set(session_id)

    with mlflow.start_run(run_name=f"supervisor_{session_id[:8]}"):
        mlflow.log_param("session_id", session_id)
        mlflow.log_param("user_input", user_input[:500])
        mlflow.log_param("history_length", len(history))

        # Cache check — automation responses are excluded later via contains_write
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

        # Build messages
        messages = []
        for msg in history[-4:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            messages.append(
                HumanMessage(content=content) if role == "user"
                else AIMessage(content=content)
            )
        messages.append(HumanMessage(content=user_input))

        # Invoke supervisor
        result = None
        response: str
        _error_occurred = False

        try:
            with TimedAudit("supervisor_invoke", agent="supervisor", user_input=user_input):
                result = _get_supervisor().invoke(
                    {"messages": messages},
                    config={"recursion_limit": 30},
                )

            all_messages = result.get("messages", [])

            # Dashboard JSON passes through verbatim (no LLM synthesis)
            dashboard_json = _find_tool_result_by_sentinel(
                all_messages, _DASHBOARD_SENTINEL
            )

            if dashboard_json:
                response = dashboard_json
                logger.info("[SUPERVISOR] Returning raw dashboard JSON to frontend")
            else:
                response = all_messages[-1].content

        except Exception as e:
            _error_occurred = True
            logger.error(f"[SUPERVISOR] Invocation failed: {e}")
            log_event("supervisor_error", agent="supervisor", error=str(e), success=False)
            mlflow.log_param("error", str(e)[:500])
            response = (
                "⚠️ I encountered an error processing your request. "
                "Please check that the issue, project, or user exists in Redmine and try again."
            )

        # Extract tool calls used
        tool_calls_used: list[str] = []
        if result is not None:
            for msg in result.get("messages", []):
                if hasattr(msg, "tool_calls"):
                    for tc in (msg.tool_calls or []):
                        tool_calls_used.append(tc.get("name", ""))

        # Duplicate write guard
        automation_call_count = tool_calls_used.count("call_automation_agent")
        if automation_call_count > 1:
            logger.error(
                f"[SUPERVISOR] call_automation_agent invoked {automation_call_count}x "
                f"in a single turn (session={session_id}). Possible duplicate writes."
            )
            _error_occurred = True
            response = (
                "⚠️ Something went wrong — the action may have been attempted more than once. "
                "Please check Redmine for duplicate issues before retrying."
            )

        # Cache only non-write responses
        contains_write = "call_automation_agent" in tool_calls_used
        if not contains_write and response and not _error_occurred:
            _cache_set(cache_key, response)

        # Metrics
        total_ms = (time.perf_counter() - start) * 1000
        mlflow.log_metric("latency_ms", total_ms)
        mlflow.log_metric("response_length", len(response))
        mlflow.log_metric("contains_write", int(contains_write))
        mlflow.log_metric("automation_call_count", automation_call_count)
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


def _find_tool_result_by_sentinel(messages: list, sentinel: str) -> str | None:
    """
    Walk the message list and return the content of the first ToolMessage
    whose content starts with the given sentinel prefix (stripped).
    Returns None if no such message exists.
    """
    for msg in messages:
        if isinstance(msg, ToolMessage):
            content = msg.content
            if isinstance(content, str) and content.startswith(sentinel):
                return content[len(sentinel):]
    return None


def _clean_for_handoff(text: str) -> str:
    """Strip embedded JSON tails before passing to another agent or LLM."""
    lines = text.rstrip().split("\n")
    while lines and lines[-1].strip().startswith("{") and "risk_payload" in lines[-1]:
        lines.pop()
    return "\n".join(lines).strip()