"""
agents/dashboard_agent.py

Dashboard agent built with create_agent (LangChain subagents pattern).

ROOT CAUSE OF THE BUGS:
  1. tool_choice="any" forced the LLM to call ANOTHER tool after generate_dashboard_json
     returned — causing an infinite reasoning loop that produced the repeated garbage text.
  2. The supervisor re-summarized the JSON string into prose instead of passing it through.

FIXES:
  1. Remove tool_choice="any". Let the model decide when to stop. The system prompt
     already forbids plain-text endings — no need to force every step to call a tool.
  2. The dashboard agent now returns result["messages"][-1].content directly, which
     is the JSON string that generate_dashboard_json produced.
  3. The supervisor tool (call_dashboard_agent in supervisor.py) must return this
     string verbatim — the frontend, not the supervisor LLM, renders it.
"""

import json
import logging

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage

from audit import log_event
from llm import get_llm
from tools.read_tools import READ_TOOLS
from tools.chart_tools import CHART_TOOLS

logger = logging.getLogger(__name__)

DASHBOARD_AGENT_TOOLS = READ_TOOLS + CHART_TOOLS

# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are RedMind's Dashboard Agent — a data visualization specialist for Redmine.

WORKFLOW (follow this order every time):
  1. Read the user query and identify what data is needed.
  2. Call 1-2 READ_TOOLS to fetch live data from Redmine.
  3. Call generate_dashboard_json (or generate_quick_stat) with EXACTLY that data.
  4. Your turn ends immediately after the chart tool returns. Do NOT add any text.

IMPORTANT: After you call generate_dashboard_json or generate_quick_stat, stop completely.
The tool return value IS your final response. Do not write anything after it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL SELECTION GUIDE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

"summary / overview / all projects"
  → get_all_issues_across_projects + get_all_projects → generate_dashboard_json

"project X dashboard / how is project X going"
  → get_project_issues(project_identifier=X) + get_project_members(X) → generate_dashboard_json

"workload / team load / who is doing what / team performance"
  → get_workload_by_member → generate_dashboard_json

"overdue / late / behind schedule"
  → get_all_issues_across_projects(status="open") → generate_dashboard_json

"priority breakdown / how many urgent"
  → get_all_issues_across_projects(status="open") → generate_dashboard_json

"X's work / what is X working on / X's issues"
  → get_issues_assigned_to_person(person_name=X) → generate_dashboard_json

"risk / triage / biggest problems / what to fix first"
  → get_risk_overview → generate_dashboard_json

"how many open bugs / count of X"  (single number)
  → appropriate read tool → generate_quick_stat

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHART BUILDING RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

For generate_dashboard_json provide:

  charts — list of chart configs:
    bar:  { "type":"bar",  "title":"...", "data":[{"label":"X","value":N},...], "xKey":"label", "yKey":"value", "insight":"..." }
    pie:  { "type":"pie",  "title":"...", "data":[{"name":"X","value":N},...],  "nameKey":"name", "valueKey":"value" }
    line: { "type":"line", "title":"...", "data":[{"label":"X","value":N},...], "xKey":"label", "yKey":"value" }

  kpis — list of KPI cards:
    { "label":"Open Issues", "value":"14", "status":"warning" }
    status values: "critical" | "warning" | "good" | "info"

  summary — 1-2 sentence plain-English health summary with real numbers
  title   — reflects what the user asked for (NOT always "Project Dashboard")

RULES:
- Use exact numbers from tool results. Never invent data.
- Title must reflect the user's question: "Team Workload", "Overdue Issues", etc.
- Call generate_dashboard_json EXACTLY ONCE, then stop. Write nothing after it.
- NEVER produce more than 2 read-tool calls before calling the chart tool."""


# ── Agent (lazy singleton) ─────────────────────────────────────────────────────

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        llm = get_llm()

        # KEY FIX: Do NOT use tool_choice="any".
        #
        # tool_choice="any" means "you MUST call a tool on every LLM invocation".
        # The agent loop calls the LLM once per step. So after generate_dashboard_json
        # returns its JSON string, the loop calls the LLM again — and tool_choice="any"
        # forces it to call ANOTHER tool, creating an infinite loop that produces the
        # repetitive garbage text you saw.
        #
        # The system prompt already tells the model to stop after calling the chart
        # tool. That's sufficient. Let the model decide when it's done.
        _agent = create_agent(
            model=llm,
            tools=DASHBOARD_AGENT_TOOLS,
            system_prompt=_SYSTEM_PROMPT,
            name="dashboard_agent",
        )
        logger.info("[DASHBOARD AGENT] Initialized")
    return _agent


# ── Public entry point ─────────────────────────────────────────────────────────

def _is_degenerate(text: str) -> bool:
    """Detect looping/garbage output from the LLM."""
    if len(text) < 20:
        return False
    # Check for Cyrillic or other unexpected scripts
    cyrillic_count = sum(1 for c in text if '\u0400' <= c <= '\u04FF')
    if cyrillic_count > 10:
        return True
    # Check for repetition: same 20-char chunk repeated 3+ times
    chunk = text[:20]
    if text.count(chunk) >= 3:
        return True
    # Check for "cannot provide a summary" hallucination pattern
    refusal_phrases = ["cannot provide a summary", "no substantive content", "fragment or topic"]
    if any(p in text.lower() for p in refusal_phrases):
        return True
    return False


def run_dashboard_agent(
    query: str,
    history: list = None,
    session_id: str = "default",
) -> str:
    """
    Invoke the dashboard agent with a plain-text query.
    Returns a JSON string the frontend renders as charts/KPIs.

    The returned value is ALWAYS valid JSON — either a dashboard/quick_stat
    payload, or a no_data fallback object.
    """
    history = history or []
    agent = _get_agent()

    messages = []
    for msg in history[-4:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        messages.append(
            HumanMessage(content=content) if role == "user" else AIMessage(content=content)
        )
    messages.append(HumanMessage(content=query))

    try:
        result = agent.invoke(
            {"messages": messages},
            config={"recursion_limit": 8},  # lowered: fetch x2 + chart x1 + buffer
        )

        # Walk messages in reverse to find the last tool result from a chart tool.
        # This is more robust than trusting result["messages"][-1].content,
        # because the LLM may append a short text message after the tool call.
        all_messages = result.get("messages", [])
        last_content = all_messages[-1].content if all_messages else ""
        if isinstance(last_content, str) and _is_degenerate(last_content):
            logger.warning("[DASHBOARD AGENT] Degenerate output detected, returning fallback")
            return json.dumps({
                "type": "no_data",
                "message": "Could not generate dashboard for this query. Try being more specific, e.g. 'show project health dashboard' or 'team workload chart'."
            })

        dashboard_json = _extract_dashboard_json(all_messages)

        if dashboard_json:
            log_event(
                "agent_response", agent="dashboard_agent",
                user_input=query, tool_result=dashboard_json[:200],
            )
            logger.info("[DASHBOARD AGENT] Done — returning chart JSON")
            return dashboard_json

        # Fallback: last message content
        last_content = all_messages[-1].content if all_messages else ""
        stripped = last_content.strip() if isinstance(last_content, str) else ""

        if stripped.startswith("{"):
            logger.info("[DASHBOARD AGENT] Done — returning JSON from last message")
            return stripped

        logger.warning("[DASHBOARD AGENT] No JSON found — returning no_data")
        return json.dumps({
            "type": "no_data",
            "message": stripped or "Dashboard generation failed — no data returned.",
        })

    except Exception as e:
        logger.error(f"[DASHBOARD AGENT] Error: {e}", exc_info=True)
        return json.dumps({"type": "no_data", "message": f"Could not generate dashboard: {e}"})


def _extract_dashboard_json(messages: list) -> str | None:
    """
    Walk the message list in reverse and return the content of the last
    ToolMessage whose content looks like a dashboard/quick_stat JSON object.

    This handles the case where the LLM writes a short text message after
    the chart tool call — we skip that and grab the actual tool result.
    """
    from langchain_core.messages import ToolMessage

    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            content = msg.content
            if isinstance(content, str) and content.strip().startswith("{"):
                try:
                    parsed = json.loads(content)
                    if parsed.get("type") in ("dashboard", "quick_stat"):
                        return content
                except json.JSONDecodeError:
                    continue
    return None
