"""
agents/automation_agent.py  (v10 — clean subagent pattern + LangGraph interrupt() HITL)

WHAT CHANGED FROM v9:

  - Removed ALL manual routing logic (the big if/elif chain that checked
    _pending_delete, _pending_bulk_delete, keyword sets, etc.).

  - Removed ALL keyword-based confirmation intent resolution
    (_resolve_confirmation_intent, _CONFIRM_WORDS, _CANCEL_WORDS, etc.).

  - The agent is now a plain create_agent() subagent — same pattern as
    data_agent.py / dashboard_agent.py / risk_agent.py. No pre/post
    processing around agent.invoke().

  - Human-in-the-loop for delete confirmation is now handled via
    LangGraph's interrupt() primitive inside the delete_redmine_issue tool.
    The tool pauses execution, surfaces a prompt to the user, and the
    supervisor resumes the graph with the user's reply via Command(resume=).
    The LLM never sees a "yes/no" classification problem.

  - The CONFIRMATION_SENTINEL and _pending_delete / _pending_bulk_delete
    dicts are GONE. The supervisor no longer needs to detect them or do
    any special-casing for automation responses.

  - Requires a checkpointer (InMemorySaver) so that interrupt() can persist
    graph state between the pause and the resume call.

ARCHITECTURE:
  Supervisor calls call_automation_agent(query) as a tool.
  The automation agent runs as a LangGraph subagent via create_agent().
  When the agent decides to delete an issue it calls delete_redmine_issue().
  That tool calls interrupt() — execution pauses and the confirmation
  prompt is surfaced to the frontend.
  The user replies; run_automation_agent() detects the pending interrupt,
  calls agent.invoke(Command(resume=user_reply)) with the same thread_id,
  the tool receives the reply, and either deletes the issue or cancels.

CHECKPOINTER / THREAD ID:
  - A single InMemorySaver is shared across all sessions.
  - Each user session uses "automation_{session_id}" as the thread_id so
    interrupt state survives between turns within the same conversation.
  - Swap InMemorySaver for a Redis/Postgres saver in production if you need
    persistence across process restarts.
"""
from __future__ import annotations

import logging
import time

import mlflow
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command, interrupt

import redmine as rm
from tools.write_tools import (
    create_redmine_issue,
    update_redmine_issue,
    bulk_update_issues,
    request_bulk_delete_confirmation,
)
from audit import log_event
from llm import get_llm
from datetime import date as _date, timedelta

logger = logging.getLogger(__name__)

# ── Shared checkpointer ────────────────────────────────────────────────────────
# InMemorySaver keeps interrupt state in-process.
# Swap for RedisSaver / PostgresSaver in production.
_checkpointer = InMemorySaver()


# ── Delete tool with interrupt() ───────────────────────────────────────────────

@tool
def delete_redmine_issue(issue_id: int) -> str:
    """
    Permanently delete a Redmine issue. Automatically asks the user to
    confirm before doing anything — you do NOT need to ask first.

    Call this whenever the user asks to delete a specific issue by ID.
    The built-in human approval step handles the confirmation prompt.

    Args:
        issue_id: The Redmine issue ID to delete.
    """
    # interrupt() pauses the LangGraph execution and surfaces the payload
    # to the frontend. When the user replies, the graph resumes and
    # `user_reply` contains whatever the user typed.
    user_reply: str = interrupt(
        {
            "type": "delete_confirmation",
            "issue_id": issue_id,
            "message": (
                f"⚠️ **Confirmation required** — you are about to permanently delete "
                f"issue #{issue_id}. This cannot be undone.\n\n"
                f"Reply **\"Yes\"** to confirm, or **\"No\"** to cancel."
            ),
        }
    )

    # Simple check — did the user cancel?
    normalized = user_reply.strip().lower()
    _cancel = {"no", "n", "nope", "nah", "cancel", "stop", "abort",
               "nevermind", "never mind", "don't", "do not"}
    if normalized in _cancel or any(w in normalized for w in _cancel):
        return f"❌ Deletion cancelled. Issue #{issue_id} was NOT deleted."

    # Anything else is treated as confirmation — attempt the delete.
    try:
        error = rm.delete_issue(issue_id)
    except Exception as e:
        logger.error(f"[AUTO] delete_issue raised: {e}")
        return f"❌ Failed to delete issue #{issue_id}: {e}"

    if error == "NOT_FOUND":
        return f"⚠️ Issue #{issue_id} doesn't exist — it may have already been deleted."
    if error:
        return f"❌ Failed to delete issue #{issue_id}: {error}"

    return f"🗑️ Issue #{issue_id} has been permanently deleted."


# ── Tool list ──────────────────────────────────────────────────────────────────

_AGENT_TOOLS = [
    create_redmine_issue,
    update_redmine_issue,
    bulk_update_issues,
    request_bulk_delete_confirmation,
    delete_redmine_issue,         # replaces the old sentinel-based approach
]


def _build_system_prompt() -> str:
    today = _date.today()
    tomorrow = today + timedelta(days=1)
    next_week = today + timedelta(days=7)
    return f"""You are RedMind's Automation Agent. You perform write actions in Redmine.

TODAY'S DATE: {today.isoformat()}
Use this for resolving relative dates:
  - "tomorrow"   → {tomorrow.isoformat()}
  - "next week"  → {next_week.isoformat()}
  - "in N days"  → today + N days from {today.isoformat()}
Always convert relative dates to YYYY-MM-DD before calling any tool.

RULES:
1. Execute clear write requests directly — do NOT ask unnecessary clarifying questions.
2. For DELETE requests on a single issue, call delete_redmine_issue. It handles
   confirmation automatically via a built-in human approval step.
3. For BULK DELETE requests, call request_bulk_delete_confirmation first.
4. For ambiguous user names, try the name as given; the tool will surface errors.
5. Translate natural language status names: "done" → "Resolved", "in review" → "Code Review".
6. Dates: resolve "tomorrow", "next Friday", "in 3 days" to YYYY-MM-DD before passing.
7. Be concise — confirm what was done in one or two sentences.
8. Never expose internal field names, IDs, or API details in your response.
9. After completing an action, always ask one short forward-looking question.

CRITICAL — STOP AFTER SUCCESS:
- As soon as a write tool returns a success response (starts with ✅ or 🗑️), STOP.
  Do not update, verify, or follow up with additional tool calls in the same turn.

CRITICAL — DO NOT RETRY ON ERROR:
- If a write tool returns an error (starts with ❌), do NOT call the same tool again.
  Report the error to the user and STOP."""


# ── Agent (lazy singleton) ─────────────────────────────────────────────────────

_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        _agent = create_agent(
            model=get_llm(),
            tools=_AGENT_TOOLS,
            system_prompt=_build_system_prompt(),
            name="automation_agent",
            checkpointer=_checkpointer,
        )
        logger.info("[AUTO AGENT] Initialized with InMemorySaver checkpointer")
    return _agent


# ── Interrupt state detection ──────────────────────────────────────────────────

def _is_graph_interrupted(config: dict) -> bool:
    """
    Return True if the agent graph for this thread is currently paused at
    an interrupt() call (i.e. awaiting user input before it can resume).
    """
    try:
        agent = _get_agent()
        state = agent.get_state(config)
        # LangGraph marks an interrupted graph by having pending `next` nodes
        # and at least one task with a non-empty `interrupts` list.
        has_next = bool(getattr(state, "next", None))
        has_interrupts = any(
            getattr(task, "interrupts", None)
            for task in getattr(state, "tasks", [])
        )
        return has_next and has_interrupts
    except Exception:
        return False


# ── Public entry point ─────────────────────────────────────────────────────────

def run_automation_agent(
    query: str,
    history: list[dict] | None = None,
    session_id: str = "default",
    project_identifier: str | None = None,
) -> str:
    """
    Invoke the automation agent with a plain-text query.

    Uses "automation_{session_id}" as the LangGraph thread_id so that
    interrupt() state persists between turns within the same user session.

    If the previous turn left the graph interrupted (awaiting delete
    confirmation), this call automatically resumes it via Command(resume=query)
    instead of starting a fresh invocation — no manual routing required.
    """
    if history is None:
        history = []

    q = query.strip()
    start = time.perf_counter()

    # LangGraph config — thread scoped to user session
    config = {"configurable": {"thread_id": f"automation_{session_id}"}}

    with mlflow.start_run(run_name="automation_agent", nested=True):
        mlflow.log_param("session_id", session_id)
        mlflow.log_param("query", q[:300])

        agent = _get_agent()
        reply = ""

        try:
            if _is_graph_interrupted(config):
                # The previous turn ended with an interrupt() — resume with
                # the user's reply. LangGraph will pass `q` back into
                # interrupt() as its return value inside delete_redmine_issue.
                logger.info(
                    f"[AUTO AGENT] Resuming interrupted graph for session={session_id}"
                )
                result = agent.invoke(Command(resume=q), config=config)
            else:
                # Fresh invocation — build message list with recent history.
                messages = []
                for turn in history[-8:]:
                    role = turn.get("role", "user")
                    content = turn.get("content", "")
                    messages.append(
                        HumanMessage(content=content) if role == "user"
                        else AIMessage(content=content)
                    )
                messages.append(HumanMessage(content=q))

                result = agent.invoke({"messages": messages}, config=config)

            # If this invocation hit an interrupt, surface the confirmation
            # prompt directly to the user (the LangGraph way).
            if hasattr(result, "interrupts") and result.interrupts:
                interrupt_value = result.interrupts[0].value
                reply = interrupt_value.get(
                    "message",
                    "⚠️ Confirmation required — please reply yes or no.",
                )
                mlflow.log_param("outcome", "interrupted_awaiting_confirmation")
            else:
                all_messages = (
                    result.get("messages", []) if isinstance(result, dict) else []
                )
                reply = all_messages[-1].content if all_messages else ""
                mlflow.log_param("outcome", "executed")

        except Exception as e:
            logger.error(f"[AUTO AGENT] Invocation failed: {e}")
            log_event("agent_error", agent="automation_agent", error=str(e), success=False)
            mlflow.log_param("outcome", "error")
            mlflow.log_param("error", str(e)[:300])
            reply = (
                "⚠️ I encountered an error processing your request. "
                "Please check that the issue, project, or user exists in Redmine and try again."
            )

        total_ms = (time.perf_counter() - start) * 1000
        mlflow.log_metric("latency_ms", total_ms)
        mlflow.log_metric("response_length", len(reply))
        mlflow.log_text(reply, "agent_output.txt")

        log_event(
            "agent_response",
            agent="automation_agent",
            user_input=query,
            latency_ms=total_ms,
        )
        logger.info(f"[AUTO AGENT] {total_ms:.0f}ms | session={session_id}")

        return reply