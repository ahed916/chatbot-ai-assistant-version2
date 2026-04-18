"""
agents/automation_agent.py  (v2 — graceful failure handling for missing data)

KEY CHANGES from v1:
  - _format_response() now detects validation errors from the ActionExecutor
    and formats them as clean user messages instead of raw error strings
  - Missing project:  "Project 'alpha' not found" →
      "⚠️ I couldn't find a project called 'alpha'. Available projects are: ..."
  - Missing issue:    "Issue #5 doesn't exist" →
      "⚠️ Issue #5 doesn't exist — please double-check the issue number."
  - Missing user:     "User 'John' not found" →
      "⚠️ I couldn't find a team member called 'John'. Please use the full name."
  - All other logic is IDENTICAL to v1 — only error formatting changed.

See v1 (automation_agent_v1.py) for full architecture documentation.
"""
import json
import logging
import re
import time
from collections import deque

import mlflow
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from llm import get_llm
from action_schema import parse_action_plan
from action_executor import ActionExecutor
from audit import log_event

logger = logging.getLogger(__name__)

_MAX_HISTORY_ENTRIES = 40
_session_history: dict[str, deque] = {}


def _get_internal_history(session_id: str) -> list[dict]:
    dq = _session_history.get(session_id)
    return list(dq) if dq else []


def _save_internal_history(session_id: str, user_msg: str, assistant_msg: str):
    if session_id not in _session_history:
        _session_history[session_id] = deque(maxlen=_MAX_HISTORY_ENTRIES)
    dq = _session_history[session_id]
    dq.append({"role": "user", "content": user_msg})
    dq.append({"role": "assistant", "content": assistant_msg})


def clear_session_history(session_id: str) -> None:
    if session_id in _session_history:
        del _session_history[session_id]
    if session_id in _pending_confirmations:
        del _pending_confirmations[session_id]
    logger.debug(f"[AUTO] Cleared session history for '{session_id}'")


def _merge_histories(external: list[dict], internal: list[dict]) -> list[dict]:
    if external:
        return external
    return internal


# ── System prompt (UNCHANGED from v1) ─────────────────────────────────────────
SYSTEM_PROMPT = """You are RedMind's Automation Agent. Your ONLY job is to convert a user request into a JSON ActionPlan.

You do NOT execute actions. You do NOT call tools. You output ONE JSON object.
The backend validates and executes everything — you are the planner only.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT (always return this JSON, nothing else):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{
  "preamble": "optional short message describing what you'll do",
  "requires_confirmation": false,
  "confirmation_prompt": "",
  "actions": [
    {
      "type": "<action_type>",
      "description": "human-readable summary of this action",
      "params": { ... }
    }
  ]
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SUPPORTED ACTION TYPES AND PARAMS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

create_issue:
  required: project (str), subject (str)
  optional: description, assignee (name), priority (Low/Normal/High/Urgent/Immediate),
            tracker (Bug/Feature/Task/Support), due_date (YYYY-MM-DD)

update_issue:
  required: issue_id (int)
  optional: status (str), assignee (name), priority, tracker, due_date,
            subject, description, done_ratio (0-100), notes (str)

bulk_update_issues:
  required: issue_ids (list of ints)
  optional: status, assignee (name), priority, tracker, notes

delete_issue:
  required: issue_id (int)
  note: ALWAYS set requires_confirmation=true and write a clear confirmation_prompt

create_project:
  required: name (str), identifier (str — lowercase, no spaces, hyphens ok)
  optional: description, is_public (bool, default false)

add_file_to_issue:
  required: issue_id (int), file_path (str)

search_and_update:
  Filter params: filter_project, filter_tracker, filter_priority, filter_status, filter_assignee
  Update params: status, assignee, priority, tracker, notes

unsupported:
  params: { "reason": "why this is not supported" }

needs_clarification:
  params: { "question": "what you need to know" }
  IMPORTANT: Only use this when critical info is genuinely missing.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRACKER RULES:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  • "task", "todo", "implement X", "add X", "set up X"  → tracker = "Task"
  • "bug", "error", "crash", "broken", "fix X"          → tracker = "Bug"
  • "feature", "enhancement", "new X"                   → tracker = "Feature"
  • "support", "help", "question"                        → tracker = "Support"
  DEFAULT: omit tracker field if unclear. Backend defaults to "Task".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STATUS AND PRIORITY DEFAULTS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  - No status mentioned → OMIT status field. Backend defaults to "New".
  - No priority mentioned → OMIT priority field. Backend defaults to "Normal".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
READING CONVERSATION HISTORY — CRITICAL RULES:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE 1: If a previous message disambiguated a name, re-issue the original action with the clarified name.
RULE 2: Short replies ("yes", a name, a number) always refer to the previous assistant message.
RULE 3: Never ask what you already know from history.
RULE 4: If history asked for a missing field and current message provides it, reconstruct the FULL action.
RULE 5: When called as part of a parallel operation, focus ONLY on the current write request.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STATUS MAPPINGS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  "ready for code review" / "in review" → "Code Review"
  "done" → "Resolved" or "Closed"
  "wont fix" → "Rejected"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Always output valid JSON. Never wrap in markdown fences.
2. For delete: ALWAYS requires_confirmation=true.
3. Bulk operations: use ONE bulk_update_issues action.
4. Pass user names as strings — backend resolves them.
5. Multiple actions = multiple entries in "actions" array.
6. Do NOT invent optional fields. Omit if not mentioned.
7. done_ratio must be integer 0-100.
8. Dates: resolve natural language to YYYY-MM-DD. Today is {TODAY}.

Respond with JSON only."""


# ── LLM planner ────────────────────────────────────────────────────────────────

def _plan(query: str, history: list[dict]) -> dict:
    from datetime import date
    today_str = date.today().isoformat()
    system = SYSTEM_PROMPT.replace("{TODAY}", today_str)

    llm = get_llm()
    messages = [SystemMessage(content=system)]
    for turn in history:
        if turn["role"] == "user":
            messages.append(HumanMessage(content=turn["content"]))
        else:
            messages.append(AIMessage(content=turn["content"]))
    messages.append(HumanMessage(content=query))

    response = llm.invoke(messages)
    if hasattr(response, 'usage_metadata') and response.usage_metadata:
        mlflow.log_metric("input_tokens", response.usage_metadata.get("input_tokens", 0))
        mlflow.log_metric("output_tokens", response.usage_metadata.get("output_tokens", 0))
    raw_text = response.content.strip()
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text)

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"[PLANNER] Failed to parse LLM JSON: {e}\nRaw: {raw_text}")
        raise ValueError(f"LLM returned invalid JSON: {e}")


_pending_confirmations: dict[str, "ActionPlan"] = {}
_executor_instance = ActionExecutor()


# ── Graceful failure formatter ─────────────────────────────────────────────────

def _format_execution_result(preamble: str | None, result: str) -> str:
    """
    Format the executor result as a clean user-facing message.

    Handles three categories:
      1. Success (✅ or action keywords) → keep as-is, add closing prompt
      2. Validation error (project/issue/user not found) → rewrite as friendly message
      3. Other content → keep as-is, add closing prompt
    """
    result_lower = result.lower() if result else ""

    # ── Missing project ────────────────────────────────────────────────────────
    if "project" in result_lower and ("not found" in result_lower or "validation error" in result_lower):
        # Extract project name and available projects from executor error if present
        project_name_match = re.search(r"project ['\"]([^'\"]+)['\"]", result, re.IGNORECASE)
        available_match = re.search(r"available projects?:(.+?)(?:\n|$)", result, re.IGNORECASE | re.DOTALL)

        project_name = project_name_match.group(1) if project_name_match else "that project"
        if available_match:
            available_raw = available_match.group(1).strip()
            # Extract just the display names (before the identifiers in parentheses)
            display_names = re.findall(r"'([^']+)'\s*\(", available_raw)
            if display_names:
                if len(display_names) <= 5:
                    available_str = ", ".join(f'"{n}"' for n in display_names)
                else:
                    available_str = ", ".join(
                        f'"{n}"' for n in display_names[:5]) + f" and {len(display_names)-5} more"
                friendly = (
                    f"⚠️ I couldn't find a project called \"{project_name}\". "
                    f"The available projects are: {available_str}. "
                    f"Please use one of these names and try again."
                )
            else:
                friendly = (
                    f"⚠️ I couldn't find a project called \"{project_name}\". "
                    f"Please check the project name and try again."
                )
        else:
            friendly = (
                f"⚠️ I couldn't find a project called \"{project_name}\". "
                f"Please check the project name and try again."
            )
        return friendly + "\n\nWhat would you like to do next?"

    # ── Missing issue ──────────────────────────────────────────────────────────
    if ("issue #" in result_lower or "issue" in result_lower) and (
        "doesn't exist" in result_lower or "not found" in result_lower or
        "does not exist" in result_lower
    ):
        issue_match = re.search(r"issue #(\d+)", result, re.IGNORECASE)
        issue_ref = f"issue #{issue_match.group(1)}" if issue_match else "that issue"
        friendly = (
            f"⚠️ {issue_ref.capitalize()} doesn't exist in Redmine — "
            f"please double-check the issue number and try again."
        )
        return friendly + "\n\nWhat would you like to do next?"

    # ── Missing user / assignee ────────────────────────────────────────────────
    if ("user" in result_lower or "assignee" in result_lower or "member" in result_lower) and (
        "not found" in result_lower or "no member" in result_lower
    ):
        user_match = re.search(r"user ['\"]([^'\"]+)['\"]", result, re.IGNORECASE)
        user_name = user_match.group(1) if user_match else "that user"
        friendly = (
            f"⚠️ I couldn't find a team member called \"{user_name}\". "
            f"Please use the full name as it appears in Redmine (e.g. 'Alice Fullstack', 'Amir Backend')."
        )
        return friendly + "\n\nWhat would you like to do next?"

    # ── Normal result (success or other) ──────────────────────────────────────
    parts = []
    if preamble and preamble.strip():
        parts.append(preamble.strip())
    if result and result.strip():
        parts.append(result.strip())
    parts.append("What would you like to do next?")
    return "\n\n".join(parts)


# ── Main entry point ───────────────────────────────────────────────────────────

def run_automation_agent(
    query: str,
    history: list[dict] = None,
    session_id: str = "default",
    project_identifier: str = None,
) -> str:
    if history is None:
        history = []

    q_stripped = query.strip()
    q_lower = q_stripped.lower()
    start = time.perf_counter()

    with mlflow.start_run(run_name="automation_agent", nested=True):

        mlflow.log_param("session_id", session_id)
        mlflow.log_param("query", q_stripped[:300])

        # Step 1: Merge history
        internal = _get_internal_history(session_id)
        effective_history = _merge_histories(history, internal)

        # Step 2: Cancellation
        if q_lower in ("cancel", "no", "nevermind", "stop", "abort"):
            if session_id in _pending_confirmations:
                del _pending_confirmations[session_id]
                reply = "Action cancelled. Nothing was changed."
            else:
                reply = "No pending action to cancel."
            mlflow.log_param("outcome", "cancelled")
            mlflow.log_metric("latency_ms", (time.perf_counter() - start) * 1000)
            _save_internal_history(session_id, q_stripped, reply)
            return reply

        # Step 3: Confirmation reply
        if session_id in _pending_confirmations and q_lower.startswith("yes"):
            pending_plan = _pending_confirmations.pop(session_id)
            logger.info(f"[AUTO] session={session_id} executing confirmed plan")
            result = _executor_instance.execute(pending_plan)
            log_event("agent_response", agent="automation_agent", user_input=query)
            reply = _format_execution_result(None, result)
            mlflow.log_param("outcome", "confirmed_execution")
            mlflow.log_metric("latency_ms", (time.perf_counter() - start) * 1000)
            mlflow.log_metric("response_length", len(reply))
            _save_internal_history(session_id, q_stripped, reply)
            return reply

        # Step 4: Plan
        try:
            raw_plan = _plan(query, effective_history)
            plan = parse_action_plan(raw_plan)
        except ValueError as e:
            logger.error(f"[AUTO] Planning failed: {e}")
            mlflow.log_param("outcome", "planning_error")
            mlflow.log_param("error", str(e)[:300])
            mlflow.log_metric("latency_ms", (time.perf_counter() - start) * 1000)
            reply = (
                "I couldn't process that request right now — the planning service returned an error.\n"
                "Please try again in a moment."
            )
            _save_internal_history(session_id, q_stripped, reply)
            return reply

        action_types = [a.type for a in plan.actions] if plan.actions else []
        mlflow.log_param("action_types", str(action_types))
        mlflow.log_param("action_count", len(action_types))
        mlflow.log_param("requires_confirmation", str(plan.requires_confirmation))

        # Step 5: Confirmation gate
        if plan.requires_confirmation and plan.actions:
            _pending_confirmations[session_id] = plan
            logger.info(f"[AUTO] session={session_id} awaiting confirmation")
            reply = plan.confirmation_prompt
            mlflow.log_param("outcome", "awaiting_confirmation")
            mlflow.log_metric("latency_ms", (time.perf_counter() - start) * 1000)
            _save_internal_history(session_id, q_stripped, reply)
            return reply

        # Step 6: Pseudo-actions
        if plan.actions and plan.actions[0].type == "needs_clarification":
            reply = plan.actions[0].params.get("question", "Could you clarify?")
            mlflow.log_param("outcome", "needs_clarification")
            mlflow.log_metric("latency_ms", (time.perf_counter() - start) * 1000)
            _save_internal_history(session_id, q_stripped, reply)
            return reply

        # Step 7: Execute
        result = _executor_instance.execute(plan)
        log_event("agent_response", agent="automation_agent", user_input=query)

        # Use the improved formatter that handles not-found errors gracefully
        reply = _format_execution_result(plan.preamble, result)

        mlflow.log_param("outcome", "executed")
        mlflow.log_metric("latency_ms", (time.perf_counter() - start) * 1000)
        mlflow.log_metric("response_length", len(reply))
        mlflow.log_text(reply, "agent_output.txt")

        _save_internal_history(session_id, q_stripped, reply)
        return reply
