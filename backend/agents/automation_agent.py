"""
agents/automation_agent.py

Automation Agent — executes Redmine actions intelligently.

Key fix from v1:
  - inject_context() pre-loads project/member/status/tracker data so the
    agent already knows valid IDs, user names, and workflow states without
    making 3-4 read calls before it can act.
  - AGENT_RECURSION_LIMIT (iterations × 2) prevents mid-task cutoff.
  - Has full read + write access.
"""
import logging
from langgraph.prebuilt import create_react_agent
from langchain_core.messages import HumanMessage

from config import load_prompt, AGENT_RECURSION_LIMIT
from llm import get_llm
from tools.read_tools import READ_TOOLS
from tools.write_tools import WRITE_TOOLS
from context_builder import inject_context
from audit import log_event, TimedAudit

logger = logging.getLogger(__name__)

AUTOMATION_TOOLS = READ_TOOLS + WRITE_TOOLS

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        prompt = load_prompt("automation_agent")
        _agent = create_react_agent(get_llm(), tools=AUTOMATION_TOOLS, prompt=prompt)
        logger.info("[AUTOMATION AGENT] Initialized")
    return _agent


def run_automation_agent(query: str, project_identifier: str = None) -> str:
    """
    Run the automation agent.
    Context is pre-loaded so the agent knows users, statuses, and trackers
    by name without spending tool calls to look them up.
    """
    from metrics import MetricsCollector
    import time as _time

    agent = _get_agent()

    t_ctx = _time.perf_counter()
    enriched_query = inject_context(query, project_identifier)
    ctx_ms = (_time.perf_counter() - t_ctx) * 1000

    with MetricsCollector("automation_agent", query) as mc:
        mc.record_context_build(ctx_ms, len(enriched_query))
        try:
            result = agent.invoke(
                {"messages": [HumanMessage(content=enriched_query)]},
                config={"recursion_limit": AGENT_RECURSION_LIMIT},
            )
            messages = result.get("messages", [])
            final = messages[-1].content if messages else "Automation agent produced no output."

            mc.record_output(final)
            mc.record_tool_calls(messages)

            # Count write operations from tool call list
            write_tool_names = {
                "create_redmine_issue", "update_redmine_issue",
                "delete_redmine_issue", "bulk_update_issues",
            }
            for msg in messages:
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                        if name in write_tool_names:
                            mc.record_write_operation(name)

            log_event("agent_response", agent="automation_agent", user_input=query)
            return final
        except Exception as e:
            logger.error(f"[AUTOMATION AGENT] Error: {e}")
            return f"Automation agent error: {e}"
