"""
agents/data_agent.py

Read-only Redmine data agent built with create_agent (LangChain subagents pattern).
https://docs.langchain.com/oss/python/langchain/multi-agent/subagents

This module was previously an inline closure inside supervisor.py (_get_data_agent).
It is now a proper, self-contained subagent module so it can be:
  - tested independently
  - imported cleanly by the supervisor
  - extended without touching supervisor.py

NO routing logic lives here. The agent gets all READ_TOOLS and the LLM
decides which tool(s) to call based on the query — that is the ReAct loop.
"""

import logging

from langchain.agents import create_agent

from llm import get_llm
from tools.read_tools import READ_TOOLS

logger = logging.getLogger(__name__)

# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are RedMind's Data Agent — a read-only Redmine assistant.

You have access to tools that fetch live Redmine data. Use them to answer the query.

RULES:
- For single-issue lookups (#N), call get_issue_details ONCE with that ID. Done.
- For filtered lists (priority / tracker / assignee), call the relevant tool ONCE. Done.
- NEVER call the same tool twice with the same arguments.
- Answer in plain, human-friendly language. No field names, no null values, no API internals.
- Use exact numbers, names, and IDs from the tool results.
- Do not invent data. If a tool returns nothing, say so clearly.
- Maximum 5 sentences for simple facts. Use bullets for lists.
- Do NOT add a closing question — the supervisor will handle that."""

# ── Agent (lazy singleton) ─────────────────────────────────────────────────────

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        _agent = create_agent(
            model=get_llm(),
            tools=READ_TOOLS,
            system_prompt=_SYSTEM_PROMPT,
            name="data_agent",
        )
        logger.info("[DATA AGENT] Initialized")
    return _agent


# ── Public entry point ─────────────────────────────────────────────────────────

def run_data_agent(query: str) -> str:
    """
    Invoke the data agent with a plain-text query.
    Returns the agent's final text response.

    Called by the supervisor's call_data_agent tool.
    """
    agent = _get_agent()
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": query}]},
            config={"recursion_limit": 8},
        )
        return result["messages"][-1].content
    except Exception as e:
        logger.error(f"[DATA AGENT] Invocation failed: {e}")
        return (
            "I wasn't able to retrieve that data. "
            "Please check the issue or project exists and try again."
        )
