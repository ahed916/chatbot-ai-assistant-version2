"""
supervisor.py — The Supervisor Agent

Architecture:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The supervisor makes ONE routing LLM call to classify intent, then:

  - Read/direct queries        → ReAct loop with READ_TOOLS
  - Dashboard/summary          → dashboard_agent
  - Write/action               → automation_agent
  - Risk/deadline              → risk_agent
  - Read + Write               → direct (ReAct) + automation_agent
  - Multiple needs             → parallel agents (any combination)

KEY DESIGN DECISIONS:
  - No extra LLM call for query splitting. The routing LLM emits read_query
    and write_query inline when it chooses the parallel route.

  - Each agent in a parallel call gets its OWN namespaced session_id so
    internal session histories never bleed across agents.

  - Parallel responses are SYNTHESIZED into a single coherent reply by a
    final LLM call rather than mechanically concatenated with separators.
    "Show me issue 7 and mark it as resolved" → one natural answer, not two
    disconnected blocks with headers and dividers.
"""

import asyncio
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import redis as redis_lib
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

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
from tools.read_tools import READ_TOOLS

logger = logging.getLogger(__name__)

# ── Redis ─────────────────────────────────────────────────────────────────────
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

_executor = ThreadPoolExecutor(max_workers=4)


# ── Cache helpers ─────────────────────────────────────────────────────────────

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


def _cache_set(key: str, response: str):
    if not _redis:
        return
    try:
        _redis.setex(key, CACHE_TTL_LLM_RESPONSE, response)
        logger.info(f"[LLM CACHE SET] {key[:20]}… TTL={CACHE_TTL_LLM_RESPONSE}s")
    except Exception as e:
        logger.warning(f"[LLM CACHE SET ERROR]: {e}")


# ── Routing ───────────────────────────────────────────────────────────────────

ROUTING_PROMPT = """You are a routing engine for a Redmine project management assistant.
Analyze the user message and respond ONLY with valid JSON. No explanation. No markdown. Pure JSON only.

{
  "route": "direct" | "dashboard_agent" | "automation_agent" | "risk_agent" | "parallel",
  "agents": [],
  "read_query": null,
  "write_query": null,
  "reason": "one sentence"
}

AGENT CAPABILITIES:
  "direct"            → read/fetch live Redmine data (issue lookups, counts, assignments, status checks, member lists, due dates, workload queries)
  "dashboard_agent"   → visual summaries, overviews, charts, KPIs, health reports, team performance
  "automation_agent"  → write actions (create, update, delete, assign, close, bulk-update issues/projects)
  "risk_agent"        → risks, blockers, overdue issues, deadline concerns, behind-schedule queries

ROUTING RULES:

"direct":
  Pure read questions. No write intent.
  Examples: "who is assigned to #5", "how many open bugs", "what is Alice working on",
            "list all issues", "show me issues without a due date", "what's the status of #12"

"dashboard_agent":
  Visual summary, overview, charts, KPIs, reports, health status.
  Examples: "summary", "overview", "report", "dashboard", "workload distribution chart"
  RULE: "summary" / "overview" / "report" → dashboard_agent, NEVER direct.

"automation_agent":
  Pure write actions with no read question attached.
  Examples: "delete issue #5", "close all bugs", "create a task", "assign #3 to Alice"

"risk_agent":
  Problems, concerns, risks, deadlines, blockers, new issues, recent changes.
  Examples: "any risks?", "overdue issues", "who is behind schedule", "blockers",
            "what new risks appeared today", "what changed today", "any new problems"
            "did anything change", "what changed since", "any updates since the alert",
            "has anything improved", "still the same risks?"

"parallel":
  Use when the request COMBINES multiple intents from different agents.
  Set "agents" to the list of agents needed.

  IMPORTANT — read_query / write_query fields:
    When agents includes BOTH "direct" AND "automation_agent", populate:
      "read_query":  the question/lookup portion only (string)
      "write_query": the action/modification portion only (string)
    Otherwise set both to null.

  Examples:
    "What is the status of issue #5 and assign it to Alice"
      → parallel, ["direct", "automation_agent"]
      → read_query: "What is the status of issue #5"
      → write_query: "Assign issue #5 to Alice"

    "Show me issue 7 and mark it as resolved"
      → parallel, ["direct", "automation_agent"]
      → read_query: "Show me issue 7"
      → write_query: "Mark issue 7 as resolved"

    "Give me a project summary and also create a task for login"
      → parallel, ["dashboard_agent", "automation_agent"]
      → read_query: null, write_query: null

    "write_query": the action portion, fully self-contained with all entities resolved.
    BAD:  "Change it to Amira"
    GOOD: "Change the assignee of issue 7 to Amira"

DEFAULT: unsure between direct and dashboard_agent → choose dashboard_agent.

IMPORTANT — read_query / write_query fields:
    When agents includes BOTH "direct" AND "automation_agent", populate:
      "read_query":  the question/lookup portion only (string)
      "write_query": the action/modification portion only, FULLY SELF-CONTAINED
                     with all entity references resolved. Never use pronouns like
                     "it", "them", "this". Always include the explicit issue ID,
                     project name, or other subject.
    Otherwise set both to null.

  Examples:
    "Who is assigned to issue 7 and change it to Bob"
      → read_query: "Who is assigned to issue 7"
      → write_query: "Change the assignee of issue 7 to Bob"   ← not "change it"

Respond with JSON only."""


def _decide_routing(user_input: str, history: list) -> dict:
    llm = get_llm()
    messages = [SystemMessage(content=ROUTING_PROMPT)]
    for msg in history[-4:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        messages.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))
    messages.append(HumanMessage(content=f"User message: {user_input}"))

    response = None
    try:
        response = llm.invoke(messages)
        raw = response.content.strip()

        # Strip markdown fences
        if "```" in raw:
            raw = raw.split("```")[1].split("```")[0]
            if raw.startswith("json"):
                raw = raw[4:]

        # Strip // comments (some models add them despite instructions)
        import re
        raw = re.sub(r"//.*", "", raw)

        # Extract the first complete JSON object even if there's trailing text
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)

        routing = json.loads(raw.strip())
        logger.info(
            f"[ROUTING] → {routing['route']} agents={routing.get('agents', [])} | "
            f"{routing.get('reason', '')}"
        )
        return routing
    except Exception as e:
        raw_preview = ""
        try:
            raw_preview = response.content[:200]
        except UnboundLocalError:
            raw_preview = "<no response — LLM call failed>"
        logger.warning(f"[ROUTING] Parse failed: {e} | raw={raw_preview!r} — defaulting to direct")
        return {
            "route": "direct", "agents": [],
            "read_query": None, "write_query": None,
            "reason": "routing parse failed",
        }


# ── Direct answer via ReAct tool loop ─────────────────────────────────────────

DIRECT_SYSTEM = """You are RedMind, an intelligent Redmine project management assistant.

You have access to tools that fetch live Redmine data. Use them to answer the user's question.

RULES:
- Call only the tools you actually need. One or two tool calls is usually enough.
- Answer in plain, human-friendly language. Never mention field names, null values, or API internals.
  Bad: "The closed_on field is null and is_closed is false."
  Good: "The issue is still open."
- Use exact numbers, names, and IDs from the tool results.
- Do not invent data. If a tool returns nothing useful, say so clearly.
- Maximum 5 sentences for simple facts. Use bullets only for lists.
- Never mention tools, agents, API calls, or internal processes.
- Do NOT add a closing question — the caller handles that."""


def _direct_answer(user_input: str, history: list) -> str:
    from langgraph.prebuilt import create_react_agent

    llm = get_llm()
    agent = create_react_agent(model=llm, tools=READ_TOOLS, prompt=DIRECT_SYSTEM)

    messages = []
    for msg in history[-4:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        messages.append(HumanMessage(content=content) if role == "user" else AIMessage(content=content))
    messages.append(HumanMessage(content=user_input))

    try:
        result = agent.invoke({"messages": messages}, config={"recursion_limit": 10})
        final = result["messages"][-1]
        return (final.content if hasattr(final, "content") else str(final)).strip()
    except Exception as e:
        logger.error(f"[DIRECT ANSWER] Agent failed: {e}")
        return f"I ran into an issue fetching that data: {e}. Please try rephrasing."


# ── Response synthesizer ──────────────────────────────────────────────────────

SYNTHESIZE_SYSTEM = """You are RedMind, a Redmine project management assistant.

Multiple processing steps ran in parallel to handle one user request.
Combine their outputs into a SINGLE, natural, coherent reply — as if one person answered everything.

STRICT RULES:
- ONE unified response. No section headers (no "📊 Dashboard", no "⚙️ Actions", no "---").
- Weave information together naturally. Mention actions taken alongside relevant facts.
- If an action succeeded (✅), mention it naturally. If it failed (⚠️ or ❌), include the reason.
- Do not repeat the same fact twice.
- End with exactly ONE short closing question.
- Never mention "agents", "steps", "parallel", or any internal architecture.
- Match the user's language."""


def _synthesize_parallel_responses(
    user_input: str,
    agent_results: list[tuple[str, str]],
) -> str:
    """Merge multiple agent outputs into one coherent reply."""
    label_map = {
        "direct": "Information",
        "dashboard_agent": "Dashboard summary",
        "automation_agent": "Action taken",
        "risk_agent": "Risk analysis",
        "error": "Error",
    }
    parts = [
        f"[{label_map.get(name, name)}]\n{text.strip()}"
        for name, text in agent_results
        if text.strip()
    ]

    if not parts:
        return "I wasn't able to get a response. Please try again."

    # If only one agent ran, skip synthesis — just add a closing prompt
    if len(parts) == 1:
        text = agent_results[0][1].strip()
        if not text.endswith("?"):
            text += "\n\nWhat would you like to do next?"
        return text

    combined = "\n\n".join(parts)
    prompt = (
        f'The user said: "{user_input}"\n\n'
        f"Outputs from the processing steps:\n\n{combined}\n\n"
        f"Write a single unified natural response combining all of the above."
    )

    try:
        llm = get_llm()
        response = llm.invoke([
            SystemMessage(content=SYNTHESIZE_SYSTEM),
            HumanMessage(content=prompt),
        ])
        return response.content.strip()
    except Exception as e:
        logger.warning(f"[SYNTHESIZE] Failed ({e}), falling back to plain join")
        return "\n\n".join(text.strip() for _, text in agent_results if text.strip())


# ── Parallel agent dispatch ───────────────────────────────────────────────────

def _get_agent_fn(name: str):
    if name == "direct":
        return lambda query, history, session_id: _direct_answer(query, history)
    if name == "dashboard_agent":
        return run_dashboard_agent
    if name == "automation_agent":
        return run_automation_agent
    if name == "risk_agent":
        return run_risk_agent
    return None


def _build_agent_queries(
    agents: list[str],
    original_query: str,
    read_query: str | None,
    write_query: str | None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for agent in agents:
        if agent == "direct" and read_query:
            result[agent] = read_query
        elif agent == "automation_agent" and write_query:
            result[agent] = write_query
        else:
            result[agent] = original_query
    return result


async def _run_agents_parallel(
    agents: list[str],
    query: str,
    history: list,
    session_id: str,
    read_query: str | None = None,
    write_query: str | None = None,
) -> str:
    loop = asyncio.get_event_loop()
    agent_queries = _build_agent_queries(agents, query, read_query, write_query)

    async def run_one(name: str):
        fn = _get_agent_fn(name)
        if not fn:
            return name, f"⚠️ Unknown agent: '{name}'"
        agent_query = agent_queries[name]
        # Namespace session_id — prevents history bleed between agents
        agent_session_id = f"{session_id}::{name}" if name != "direct" else session_id
        result = await loop.run_in_executor(
            _executor,
            lambda q=agent_query, sid=agent_session_id: fn(q, history, sid),
        )
        return name, result

    raw_results = await asyncio.gather(
        *[run_one(n) for n in agents],
        return_exceptions=True,
    )

    agent_results: list[tuple[str, str]] = []
    for item in raw_results:
        if isinstance(item, Exception):
            logger.error(f"[PARALLEL] Agent error: {item}")
            agent_results.append(("error", f"⚠️ A step encountered an error: {item}"))
        else:
            name, content = item
            agent_results.append((name, content or ""))

    return _synthesize_parallel_responses(query, agent_results)


def _run_agents_parallel_sync(
    agents: list[str],
    query: str,
    history: list,
    session_id: str,
    read_query: str | None = None,
    write_query: str | None = None,
) -> str:
    coro = _run_agents_parallel(agents, query, history, session_id, read_query, write_query)
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result(timeout=120)
        else:
            return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)
    except Exception as e:
        logger.error(f"[PARALLEL DISPATCH] Failed: {e}")
        return f"Error running agents in parallel: {e}"


# ── Main entry point ──────────────────────────────────────────────────────────

def run_supervisor(user_input: str, history: list = None, session_id: str = "default") -> str:
    """
    Main entry point for every chat message.

    Flow:
      1. Cache check
      2. Route (one LLM call; includes query split for parallel when needed)
      3. Execute
      4. For parallel: synthesize all outputs into one unified reply
      5. Cache + log
    """
    history = history or []
    start = time.perf_counter()

    # 1. Cache
    cache_key = _make_cache_key(user_input, history)
    cached = _cache_get(cache_key)
    if cached:
        log_event(
            "supervisor_cache_hit",
            agent="supervisor",
            user_input=user_input,
            latency_ms=(time.perf_counter() - start) * 1000,
        )
        return cached

    # 2. Route
    with TimedAudit("routing_decision", agent="supervisor", user_input=user_input):
        routing = _decide_routing(user_input, history)

    route = routing.get("route", "direct")
    agents = routing.get("agents", [])
    read_query = routing.get("read_query") or None
    write_query = routing.get("write_query") or None

    log_event(
        "routing",
        agent="supervisor",
        user_input=user_input,
        extra={"route": route, "agents": agents, "reason": routing.get("reason", "")},
    )

    # 3. Execute
    try:
        if route == "direct":
            response = _direct_answer(user_input, history)
            if response and not response.rstrip().endswith("?"):
                response += "\n\nWould you like me to take any action on this?"

        elif route == "dashboard_agent":
            response = run_dashboard_agent(user_input, history, session_id)

        elif route == "automation_agent":
            response = run_automation_agent(user_input, history, session_id)

        elif route == "risk_agent":
            response = run_risk_agent(user_input, history, session_id)

        elif route == "parallel":
            if not agents:
                agents = ["dashboard_agent", "risk_agent"]
            seen: set = set()
            agents = [a for a in agents if not (a in seen or seen.add(a))]
            valid = {"direct", "dashboard_agent", "automation_agent", "risk_agent"}
            agents = [a for a in agents if a in valid]
            if not agents:
                logger.warning("[SUPERVISOR] parallel route had no valid agents; falling back to direct")
                response = _direct_answer(user_input, history)
            else:
                response = _run_agents_parallel_sync(
                    agents, user_input, history, session_id,
                    read_query=read_query,
                    write_query=write_query,
                )

        else:
            logger.warning(f"[SUPERVISOR] Unknown route '{route}', falling back to direct")
            response = _direct_answer(user_input, history)

    except Exception as e:
        logger.error(f"[SUPERVISOR] Execution failed: {e}")
        log_event("supervisor_error", agent="supervisor", error=str(e), success=False)
        response = (
            f"I encountered an error processing your request: {e}\n"
            "Please try again or rephrase your question."
        )

    # 4. Cache — never cache write responses (they mutate state)
    contains_write = route == "automation_agent" or (
        route == "parallel" and "automation_agent" in agents
    )
    if not contains_write:
        _cache_set(cache_key, response)

    total_ms = (time.perf_counter() - start) * 1000
    log_event(
        "supervisor_complete",
        agent="supervisor",
        user_input=user_input,
        latency_ms=total_ms,
        extra={"route": route, "agents": agents},
    )
    logger.info(f"[SUPERVISOR] {total_ms:.0f}ms via route='{route}' agents={agents}")

    return response
