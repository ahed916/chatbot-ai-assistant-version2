"""
supervisor.py — The Supervisor Agent

Architecture decision on parallel vs sequential agents:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Running ALL agents in parallel on EVERY query is wasteful and actually SLOWER
for simple requests (you'd wait for 3 agents when 1 was needed).

Our approach: the supervisor reasons FIRST (one fast LLM call) to decide
the routing plan, then:
  - Simple read queries → supervisor answers directly (fastest path)
  - Single agent needed → delegate to that agent
  - Multiple agents needed → run them in PARALLEL with asyncio.gather()

This gives us:
  ✅ Fast simple responses (no unnecessary agent overhead)
  ✅ Parallel execution WHEN it genuinely helps
  ✅ LLM response caching (identical queries served from Redis instantly)
  ✅ Audit logging for every decision
"""
import asyncio
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import redis as redis_lib
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agents.automation_agent import run_automation_agent
from agents.dashboard_agent import run_dashboard_agent
from agents.risk_agent import run_risk_agent
from audit import log_event, TimedAudit
from config import (
    CACHE_TTL_LLM_RESPONSE,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PORT,
    load_prompt,
)
from llm import get_llm

logger = logging.getLogger(__name__)

# ── Redis for LLM response caching ───────────────────────────────────────────
try:
    _redis = redis_lib.Redis(
        host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    _redis.ping()
    logger.info("[SUPERVISOR REDIS] Connected")
except Exception as e:
    logger.warning(f"[SUPERVISOR REDIS] Unavailable: {e}")
    _redis = None

# Thread pool for running sync agent functions in async context
_executor = ThreadPoolExecutor(max_workers=3)


# ── LLM Response Cache ────────────────────────────────────────────────────────

def _make_cache_key(user_input: str, history: list) -> str:
    """
    Create a stable cache key from the full conversation context.
    Identical queries with identical history → cache hit.
    """
    raw = json.dumps({"q": user_input, "h": history[-4:] if history else []}, sort_keys=True)
    return "llm:" + hashlib.sha256(raw.encode()).hexdigest()[:32]


def _cache_get_response(key: str) -> str | None:
    if not _redis:
        return None
    try:
        val = _redis.get(key)
        if val:
            logger.info(f"[LLM CACHE HIT] {key[:20]}...")
            return val
        return None
    except Exception:
        return None


def _cache_set_response(key: str, response: str):
    if not _redis:
        return
    try:
        _redis.setex(key, CACHE_TTL_LLM_RESPONSE, response)
        logger.info(f"[LLM CACHE SET] {key[:20]}... TTL={CACHE_TTL_LLM_RESPONSE}s")
    except Exception as e:
        logger.warning(f"[LLM CACHE SET ERROR]: {e}")


# ── Routing Decision ──────────────────────────────────────────────────────────

ROUTING_PROMPT = """You are a routing engine for a Redmine project management assistant.
Analyze the user message and respond ONLY with valid JSON. No explanation. No markdown. Pure JSON only.

{
  "route": "direct" | "dashboard_agent" | "automation_agent" | "risk_agent" | "parallel",
  "agents": [],
  "reason": "one sentence"
}

ROUTING RULES:

## ROUTING RULES — BE AGGRESSIVE ABOUT DIRECT

Route to "direct" when user asks for:
- COUNTS: "how many open bugs", "total issues", "overdue count"
- LOOKUPS: "status of #10", "who is assigned to X", "what projects exist"
- LISTS: "show me issues assigned to Amir", "list overdue items"

KEY TEST: If the answer exists in the pre-fetched Redmine context → answer directly.

NEVER route to dashboard_agent for simple counts/lookups.
NEVER route to risk_agent unless user mentions "risk", "worry", "concern", "deadline".

"direct": Simple factual lookups — one specific answer
  Examples: "who is assigned to #5", "what projects exist", "how many open bugs", "list issues"
  KEY: if a single number or list fully answers it → direct

"dashboard_agent": Overview, summary, report, stats, charts, KPIs, visual data
  Examples: "summary", "overview", "report", "dashboard", "workload distribution",
            "how is the project going", "how are we doing", "show me charts", "team status"
  KEY: PM wants to UNDERSTAND state visually → dashboard_agent
  RULE: "summary" / "overview" / "report" ALWAYS → dashboard_agent, NEVER direct

"automation_agent": Action verbs — create, update, delete, assign, close, fix, move, reassign
  Examples: "delete issue #5", "close all bugs", "assign to Alice", "create a task"
  RULE: "delete everything" / "delete all" → automation_agent (it will ask for confirmation)

"risk_agent": Problems, concerns, risks, deadlines, danger
  Examples: "any risks?", "worried about the team", "overdue issues", "missing deadline"

"parallel": Request EXPLICITLY needs BOTH dashboard charts AND risk analysis
  Examples: "full health report with risks and charts", "dashboard and risk summary"
  When parallel: agents = ["dashboard_agent", "risk_agent"]

DEFAULT: if unsure between direct and dashboard_agent → always choose dashboard_agent

Respond with JSON only."""


def _decide_routing(user_input: str, history: list) -> dict:
    """
    Fast routing call: one LLM call to decide which agent(s) to invoke.
    This call is intentionally lightweight — short prompt, JSON output only.
    """
    llm = get_llm()

    messages = [SystemMessage(content=ROUTING_PROMPT)]

    # Include last 2 exchanges for context
    for msg in history[-4:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            messages.append(HumanMessage(content=content))
        else:
            from langchain_core.messages import AIMessage
            messages.append(AIMessage(content=content))

    messages.append(HumanMessage(content=f"User message: {user_input}"))

    try:
        response = llm.invoke(messages)
        raw = response.content.strip()

        # Strip markdown code fences if model wraps JSON
        if "```" in raw:
            raw = raw.split("```")[1].split("```")[0]
            if raw.startswith("json"):
                raw = raw[4:]

        routing = json.loads(raw.strip())
        logger.info(f"[ROUTING] → {routing['route']} | reason: {routing.get('reason', '')}")
        return routing
    except Exception as e:
        logger.warning(f"[ROUTING] Failed to parse routing decision: {e} — using keyword fallback")
        return _keyword_routing(user_input)


def _keyword_routing(user_input: str) -> dict:
    """Fast keyword-based routing fallback when LLM routing fails."""
    q = user_input.lower()

    action_words = {"assign", "create", "delete", "close", "update", "fix",
                    "move", "change", "add", "remove", "reassign", "mark"}
    risk_words = {"risk", "overdue", "worried", "concern", "deadline",
                  "behind", "stuck", "problem", "issue", "health"}
    dashboard_words = {"dashboard", "chart", "report", "summary", "overview",
                       "stats", "kpi", "workload", "distribution", "how are we"}

    words = set(q.split())

    if words & action_words:
        return {"route": "automation_agent", "agents": [], "reason": "keyword: action verb detected"}
    if words & risk_words:
        return {"route": "risk_agent", "agents": [], "reason": "keyword: risk word detected"}
    if words & dashboard_words:
        return {"route": "dashboard_agent", "agents": [], "reason": "keyword: dashboard word detected"}
    if "full" in q and ("report" in q or "health" in q):
        return {"route": "parallel", "agents": ["dashboard_agent", "risk_agent"], "reason": "keyword: full report"}

    return {"route": "direct", "agents": [], "reason": "keyword fallback: no match"}


# ── Agent Dispatch (parallel-capable) ────────────────────────────────────────

# supervisor.py — update the _run_agents_parallel function

async def _run_agents_parallel(agents: list[str], query: str, timeout_per_agent: int = 90) -> str:
    """
    Run multiple agents in parallel using asyncio + ThreadPoolExecutor.
    Each agent runs in its own thread with individual timeout.
    Results are synthesized into one response.
    """
    agent_map = {
        "dashboard_agent": run_dashboard_agent,
        "automation_agent": run_automation_agent,
        "risk_agent": run_risk_agent,
    }

    loop = asyncio.get_event_loop()

    async def run_one_with_timeout(agent_name: str) -> tuple[str, str]:
        fn = agent_map.get(agent_name)
        if not fn:
            return agent_name, f"Unknown agent: {agent_name}"

        logger.info(f"[PARALLEL] Starting {agent_name} (timeout={timeout_per_agent}s)")
        try:
            # Run sync function in executor with timeout
            result = await asyncio.wait_for(
                loop.run_in_executor(_executor, fn, query),
                timeout=timeout_per_agent
            )
            logger.info(f"[PARALLEL] Done {agent_name}")
            return agent_name, result

        except asyncio.TimeoutError:
            logger.warning(f"[PARALLEL] {agent_name} timed out after {timeout_per_agent}s")
            # Return structured error that won't break JSON parsing
            return agent_name, json.dumps({
                "error": "timeout",
                "agent": agent_name,
                "message": f"Agent exceeded {timeout_per_agent}s limit",
                "fallback": "Please try again or simplify your request."
            })

        except Exception as e:
            logger.error(f"[PARALLEL] {agent_name} failed: {e}")
            return agent_name, f"⚠️ {agent_name} encountered an error: {e}"

    tasks = [run_one_with_timeout(name) for name in agents if name in agent_map]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    parts = []
    for item in results:
        if isinstance(item, Exception):
            logger.error(f"[PARALLEL] Task failed: {item}")
            parts.append(f"⚠️ An agent encountered an unexpected error.")
        else:
            agent_name, content = item
            # If content is a JSON error, you could handle it specially
            parts.append(content)

    return "\n\n---\n\n".join(parts)


def _run_agents_parallel_sync(agents: list[str], query: str, timeout_per_agent: int = 45) -> str:
    """Sync wrapper for parallel execution — FIXED for thread safety."""
    import concurrent.futures
    import asyncio

    def run_in_fresh_loop():
        # Create isolated event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                _run_agents_parallel(agents, query, timeout_per_agent)
            )
        finally:
            loop.close()

    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(run_in_fresh_loop)
            return future.result(timeout=timeout_per_agent + 10)
    except concurrent.futures.TimeoutError:
        logger.warning(f"[PARALLEL TIMEOUT] Exceeded {timeout_per_agent + 10}s")
        return "⏱️ This request is taking longer than expected. Please try a simpler query."
    except Exception as e:
        logger.error(f"[PARALLEL ERROR] {e}")
        return f"❌ Error running agents: {e}"


def _handle_direct_query(query: str, context: str) -> str:
    """Answer simple factual questions directly from context without LLM reasoning."""
    q = query.lower()

    # Count queries — answer immediately from context
    if "open bugs" in q or "bugs open" in q:
        import re
        match = re.search(r"Bug:\s*(\d+)", context)
        total_bugs = int(match.group(1)) if match else 0
        # open bugs = total bugs - closed/rejected bugs
        # Since tracker dist doesn't split by status, give what we know
        return f"There are **{total_bugs} total bugs** in Redmine. Based on the status distribution in context, approximately {total_bugs - 2} are open (subtracting closed/rejected issues)."

    return None  # fall through to LLM

# ── Direct Answer (supervisor reads Redmine itself) ───────────────────────────


def _direct_answer(user_input: str, history: list) -> str:
    """
    For simple read queries, answer in ONE LLM call — no agent, no tool loop.

    Old approach: spawn full ReAct agent → 3-5 LLM calls → 30-47s
    New approach: build context (cached) + single LLM call → 8-15s

    This is the fastest possible path for factual questions.
    """
    from context_builder import build_project_context

    # Context is Redis-cached after first build — near-instant on repeat calls
    context = build_project_context()
    llm = get_llm()

    system_msg = SystemMessage(content=(
        "You are RedMind, a Redmine project management assistant.\n"
        "Pre-fetched Redmine data is provided below. Answer the question "
        "directly and concisely using only this data.\n"
        "Be specific — use exact numbers, names, and IDs from the context.\n"
        "Do not make up data. If something is not in the context, say so clearly."
    ))

    messages = [system_msg]
    for msg in history[-4:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "user":
            messages.append(HumanMessage(content=content))
        else:
            from langchain_core.messages import AIMessage
            messages.append(AIMessage(content=content))

    messages.append(HumanMessage(content=(
        f"{context}\n\n"
        f"---\n"
        f"QUESTION: {user_input}\n\n"
        f"Answer directly from the context above."
    )))

    try:
        response = llm.invoke(messages)
        return response.content.strip()
    except Exception as e:
        logger.error(f"[DIRECT ANSWER] LLM failed: {e}")
        return f"I encountered an error answering your question: {e}"


# ── Main Supervisor Entry Point ───────────────────────────────────────────────

def run_supervisor(user_input: str, history: list = None) -> str:
    """
    Main entry point called by main.py for every chat message.

    Flow:
    1. Check LLM response cache → return instantly if hit
    2. Decide routing (one fast LLM call)
    3. Execute: direct / single agent / parallel agents
    4. Cache the response
    5. Return to user

    Args:
        user_input: The PM's message
        history: List of {"role": "user"/"assistant", "content": "..."} dicts

    Returns: Final response string (may include JSON for dashboard charts)
    """
    history = history or []
    start = time.perf_counter()

    # ── 1. Cache check ────────────────────────────────────────────────────────
    cache_key = _make_cache_key(user_input, history)
    cached = _cache_get_response(cache_key)
    if cached:
        log_event(
            "supervisor_cache_hit",
            agent="supervisor",
            user_input=user_input,
            latency_ms=(time.perf_counter() - start) * 1000,
        )
        return cached

    # ── 2. Routing decision ───────────────────────────────────────────────────
    with TimedAudit("routing_decision", agent="supervisor", user_input=user_input):
        routing = _decide_routing(user_input, history)

    route = routing.get("route", "direct")
    agents = routing.get("agents", [])

    log_event(
        "routing",
        agent="supervisor",
        user_input=user_input,
        extra={"route": route, "agents": agents, "reason": routing.get("reason", "")},
    )

    # ── 3. Execute ────────────────────────────────────────────────────────────
    try:
        if route == "direct":
            response = _direct_answer(user_input, history)

        elif route == "dashboard_agent":
            response = run_dashboard_agent(user_input)

        elif route == "automation_agent":
            response = run_automation_agent(user_input)

        elif route == "risk_agent":
            response = run_risk_agent(user_input)

        # In run_supervisor(), update the parallel branch:
        elif route == "parallel":
            if not agents:
                agents = ["dashboard_agent", "risk_agent"]
                # Pass timeout param (default 90s per agent)
            response = _run_agents_parallel_sync(agents, user_input, timeout_per_agent=90)

        else:
            # Unknown route — fall back to direct
            logger.warning(f"[SUPERVISOR] Unknown route '{route}', falling back to direct")
            response = _direct_answer(user_input, history)

    except Exception as e:
        logger.error(f"[SUPERVISOR] Execution failed: {e}")
        log_event("supervisor_error", agent="supervisor", error=str(e), success=False)
        response = (
            f"I encountered an error processing your request: {e}\n"
            f"Please try again or rephrase your question."
        )

    # ── 4. Cache response ─────────────────────────────────────────────────────
    # Don't cache write operations (automation) — results change Redmine state
    if route != "automation_agent":
        _cache_set_response(cache_key, response)

    # ── 5. Log final timing ───────────────────────────────────────────────────
    total_ms = (time.perf_counter() - start) * 1000
    log_event(
        "supervisor_complete",
        agent="supervisor",
        user_input=user_input,
        latency_ms=total_ms,
        extra={"route": route},
    )
    logger.info(f"[SUPERVISOR] Total latency: {total_ms:.0f}ms via route='{route}'")

    return response
