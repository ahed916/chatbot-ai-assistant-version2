"""
agents/automation_agent.py

Architecture: Planner + Executor

  LLM role:  Interpret intent → output structured ActionPlan JSON
  Backend:   Validate schema → validate Redmine rules → execute

The LLM never calls tools. It outputs one JSON object.
The ActionExecutor does all validation and API calls deterministically.

HOW HISTORY WORKS
─────────────────
History is managed in TWO places:

1. The caller (main.py / supervisor.py) passes `history` — the full prior
   conversation — into run_automation_agent() on every call.

2. ADDITIONALLY, this module keeps its own _session_history dict as a
   self-contained fallback. This means even if supervisor.py forgets to pass
   history, or passes a stale copy, the agent still has context.

   Every time run_automation_agent() is called:
     - The passed `history` is merged with the internal store (external wins
       on conflicts since it's more authoritative).
     - After execution, the new user+assistant turn is saved internally.

This dual approach means history works correctly regardless of whether the
caller is well-behaved or not.

IMPORTANT: When the supervisor runs automation_agent in a parallel route,
it must pass a namespaced session_id (e.g. "session123::automation_agent")
to prevent history from leaking between the direct-answer agent and this one.
"""
import json
import logging
import re
from collections import deque

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from llm import get_llm
from action_schema import parse_action_plan
from action_executor import ActionExecutor
from audit import log_event

logger = logging.getLogger(__name__)

# ── Internal history store (self-contained fallback) ──────────────────────────
# Keyed by session_id. Stores list of {"role": ..., "content": ...} dicts.
# Max 20 turns (40 entries) per session.
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
    """
    Wipe internal history and pending confirmations for a session.

    Call this after a successful action if you want a clean slate,
    or when switching contexts to prevent stale plan replay.
    """
    if session_id in _session_history:
        del _session_history[session_id]
    if session_id in _pending_confirmations:
        del _pending_confirmations[session_id]
    logger.debug(f"[AUTO] Cleared session history for '{session_id}'")


def _merge_histories(external: list[dict], internal: list[dict]) -> list[dict]:
    """
    External (caller-provided) wins if non-empty — it is the source of truth
    and may include turns from other agents in the same session.
    Fall back to internal only when the caller passes nothing.
    """
    if external:
        return external
    return internal


# ── System prompt ──────────────────────────────────────────────────────────────

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
            subject, description, done_ratio (0–100), notes (str)

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

unsupported:
  params: { "reason": "why this is not supported" }
  use when: request is outside scope or requires permissions not available

needs_clarification:
  params: { "question": "what you need to know" }
  use when: request is TRULY ambiguous and cannot be inferred from history.
  IMPORTANT: Only use this when critical info is genuinely missing.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRACKER RULES — READ CAREFULLY:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You MUST set the tracker field based on what the user says, NOT on the subject text alone.

RULE: Map the user's words to tracker like this:
  • User says "task", "todo", "chore", "implement X", "add X", "set up X"  → tracker = "Task"
  • User says "bug", "error", "crash", "broken", "defect", "fix X"         → tracker = "Bug"
  • User says "feature", "enhancement", "new X", "add support for"         → tracker = "Feature"
  • User says "support", "help", "question", "how to"                      → tracker = "Support"

CRITICAL: If the user says "create a task for login functionality", you MUST output:
  "tracker": "Task"
Even though the subject "login functionality" contains no tracker keyword.
The word "task" in the USER'S REQUEST is what determines the tracker — not the subject.

DEFAULT: If no tracker signal exists anywhere in the request, omit the tracker field.
The backend will default to "Task".

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STATUS AND PRIORITY DEFAULTS:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

- If the user does NOT mention a status → OMIT the status field. Backend defaults to "New".
- If the user does NOT mention a priority → OMIT the priority field. Backend defaults to "Normal".
- Never invent status or priority values the user didn't ask for.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
READING CONVERSATION HISTORY — CRITICAL RULES:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You will receive the full conversation history before the current message.
USE IT. This is how you resolve follow-up replies.

RULE 1 — AMBIGUOUS NAME FOLLOW-UP:
  If a previous assistant message said something like:
    "The name 'Amir' matches multiple team members: 'Amir Backend', 'Amira Frontend'."
  And the user now replies with just a name like "Amir Backend" —
  you KNOW from history what the original action was.
  → Re-issue that EXACT same action with the clarified name. Do NOT ask again.
  → Never output needs_clarification when history already tells you the intent.

RULE 2 — SHORT FOLLOW-UPS:
  If the user sends a very short message (a name, a number, "yes", "the second one"),
  ALWAYS look at the previous assistant message to understand what it refers to.
  A short message is almost always an answer to the previous question.

RULE 3 — NEVER ASK WHAT YOU ALREADY KNOW:
  If you can determine the full action from (current message + history), do it.
  Only use needs_clarification when critical info (project name, issue ID) is
  genuinely missing and cannot be inferred at all.

RULE 4 — COMPLETING A PRIOR INCOMPLETE ACTION:
  If a previous assistant message asked for a missing field (e.g. "What should
  the task be called?"), and the user's reply provides that field (e.g. "fix login error"),
  reconstruct the FULL action using ALL params from the original user message in history
  PLUS the new field from the current message.

  Example:
    History:
      user: "create a task in the e commerce website project"
      assistant: "What should the task be called?"
    Current: "fix login error"

    Correct → create_issue with project="e commerce website", subject="fix login error", tracker="Task"
    WRONG   → needs_clarification or create_issue with project=null

RULE 5 — ONLY ACT ON THE CURRENT REQUEST:
  When called as part of a parallel operation (read + write), you will receive
  only the WRITE portion of the request. Focus solely on that.
  Do NOT replay or continue actions from earlier in history unless the current
  message is explicitly a follow-up (e.g. a clarification reply or "yes" confirmation).
  If the current message is a fresh, self-contained request, treat it as new.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULES:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Always output valid JSON. Never wrap it in markdown code fences.
2. For delete operations: ALWAYS set requires_confirmation=true with a clear prompt.
3. For bulk operations on multiple issues: use ONE bulk_update_issues action.
4. If the user asks for something outside supported actions: use "unsupported".
5. Pass user names as strings in params.assignee — the backend resolves them.
6. Multiple actions = multiple entries in the "actions" array, executed in order.
7. Do NOT invent optional fields. Omit them if not mentioned by the user.
8. done_ratio must be an integer 0–100 if provided.
9. Dates: resolve natural language to YYYY-MM-DD. Today is {TODAY}.

RULE (statuses): Common mappings:
  "ready for code review" → "Code Review"
  "in review" → "Code Review"
  "done" → "Resolved" or "Closed"
  "wont fix" → "Rejected"
  "resolved" → "Resolved"
  "closed" → "Closed"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXAMPLES:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

User: "Create a high priority bug in alpha project called Login fails on Safari"
{
  "preamble": "Creating a high priority bug in the Alpha project.",
  "requires_confirmation": false,
  "confirmation_prompt": "",
  "actions": [{
    "type": "create_issue",
    "description": "Create bug: Login fails on Safari",
    "params": { "project": "alpha", "subject": "Login fails on Safari", "tracker": "Bug", "priority": "High" }
  }]
}

User: "Create a task for login functionality in the mobile-app project"
{
  "preamble": "Creating a login functionality task in the Mobile App project.",
  "requires_confirmation": false,
  "confirmation_prompt": "",
  "actions": [{
    "type": "create_issue",
    "description": "Create task: login functionality",
    "params": { "project": "mobile-app", "subject": "Login functionality", "tracker": "Task" }
  }]
}

User: "Assign issues #12, #13, #14 to Abir and mark them In Progress"
{
  "preamble": "Assigning issues #12, #13, #14 to Abir and setting status to In Progress.",
  "requires_confirmation": false,
  "confirmation_prompt": "",
  "actions": [{
    "type": "bulk_update_issues",
    "description": "Assign #12, #13, #14 to Abir and set In Progress",
    "params": { "issue_ids": [12, 13, 14], "assignee": "Abir", "status": "In Progress" }
  }]
}

User: "Delete issue #99"
{
  "preamble": "",
  "requires_confirmation": true,
  "confirmation_prompt": "⚠️ You are about to permanently delete issue #99. This cannot be undone. Reply 'yes' to confirm.",
  "actions": [{ "type": "delete_issue", "description": "Delete issue #99", "params": { "issue_id": 99 } }]
}

User: "Create a new project called Mobile App and add a task for login functionality"
{
  "preamble": "Creating the Mobile App project and adding a login functionality task.",
  "requires_confirmation": false,
  "confirmation_prompt": "",
  "actions": [
    {
      "type": "create_project",
      "description": "Create project: Mobile App",
      "params": { "name": "Mobile App", "identifier": "mobile-app" }
    },
    {
      "type": "create_issue",
      "description": "Create task: login functionality",
      "params": { "project": "mobile-app", "subject": "Login functionality", "tracker": "Task" }
    }
  ]
}

--- HISTORY EXAMPLE (most important): ---

History:
  user:      "Assign issues #1, #2, #3 to Amir"
  assistant: "⚠️ The name 'Amir' matches multiple team members: 'Amir Backend', 'Amira Frontend'.
              Please use the full name so I know exactly who you mean."
Current message: "Amir Backend"

Correct response:
{
  "preamble": "Got it — assigning issues #1, #2, and #3 to Amir Backend.",
  "requires_confirmation": false,
  "confirmation_prompt": "",
  "actions": [{
    "type": "bulk_update_issues",
    "description": "Assign #1, #2, #3 to Amir Backend",
    "params": { "issue_ids": [1, 2, 3], "assignee": "Amir Backend" }
  }]
}

WRONG (never do this when history makes intent clear):
{ "actions": [{ "type": "needs_clarification", "params": { "question": "What would you like to do with Amir Backend?" } }] }

--- SEARCH AND UPDATE ---

search_and_update:
  Filter params: filter_project, filter_tracker, filter_priority, filter_status, filter_assignee
  Update params: status, assignee, priority, tracker, notes

Example:
User: "Move all urgent bugs to Amir Frontend and set them as In Progress"
{
  "preamble": "Finding all urgent bugs and reassigning them.",
  "requires_confirmation": false,
  "confirmation_prompt": "",
  "actions": [{
    "type": "search_and_update",
    "description": "Find urgent bugs and assign to Amir Frontend, set In Progress",
    "params": { "filter_tracker": "Bug", "filter_priority": "Urgent", "assignee": "Amir Frontend", "status": "In Progress" }
  }]
}
"""


# ── LLM planner ────────────────────────────────────────────────────────────────

def _plan(query: str, history: list[dict]) -> dict:
    """
    Ask the LLM to produce an ActionPlan JSON.
    history = full prior conversation as [{"role": "user"|"assistant", "content": str}]
    The current query is NOT included in history — it is appended here as the final HumanMessage.
    """
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
    raw_text = response.content.strip()

    # Strip markdown fences if the LLM wraps output despite instructions
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text)

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        logger.error(f"[PLANNER] Failed to parse LLM JSON: {e}\nRaw: {raw_text}")
        raise ValueError(f"LLM returned invalid JSON: {e}")


# ── Pending confirmation store ─────────────────────────────────────────────────
# Keyed by session_id → pending ActionPlan. One pending action per session.
_pending_confirmations: dict[str, "ActionPlan"] = {}

# ── Executor ───────────────────────────────────────────────────────────────────
_executor = ActionExecutor()


# ── Main entry point ───────────────────────────────────────────────────────────

def run_automation_agent(
    query: str,
    history: list[dict] = None,
    session_id: str = "default",
    project_identifier: str = None,
) -> str:
    """
    Main entry point for the automation agent.

    Args:
        query:      The current user message.
        history:    Prior conversation turns from the caller (main.py / supervisor).
                    Format: [{"role": "user"|"assistant", "content": str}, ...]
                    The current query must NOT be included in this list.
        session_id: Stable identifier for this user's session. Used to key
                    the internal history store and pending confirmations.
                    Defaults to "default" for single-user / dev usage.
                    NOTE: When called from a parallel supervisor route, this
                    should be namespaced (e.g. "abc123::automation_agent") to
                    prevent history bleed from the direct-answer agent.

    Flow:
      1. Merge caller-provided history with internal history (fallback)
      2. Check for cancellation
      3. Check for pending confirmation reply ("yes")
      4. Call LLM with full merged history
      5. If plan needs confirmation → store it, return prompt (do NOT execute)
      6. Execute, save turn internally, return result
    """
    if history is None:
        history = []

    q_stripped = query.strip()
    q_lower = q_stripped.lower()

    # ── Step 1: Build effective history ───────────────────────────────────────
    internal = _get_internal_history(session_id)
    effective_history = _merge_histories(history, internal)

    logger.debug(
        f"[AUTO] session={session_id} history_turns={len(effective_history) // 2} "
        f"query={q_stripped[:80]!r}"
    )

    # ── Step 2: Cancellation ───────────────────────────────────────────────────
    if q_lower in ("cancel", "no", "nevermind", "stop", "abort"):
        if session_id in _pending_confirmations:
            del _pending_confirmations[session_id]
            reply = "Action cancelled. Nothing was changed."
        else:
            reply = "No pending action to cancel."
        _save_internal_history(session_id, q_stripped, reply)
        return reply

    # ── Step 3: Confirmation reply ─────────────────────────────────────────────
    # Handled BEFORE the LLM to avoid re-planning a destructive action.
    if session_id in _pending_confirmations and q_lower.startswith("yes"):
        pending_plan = _pending_confirmations.pop(session_id)
        logger.info(f"[AUTO] session={session_id} executing confirmed plan")
        result = _executor.execute(pending_plan)
        log_event("agent_response", agent="automation_agent", user_input=query)
        reply = _format_response(None, result)
        _save_internal_history(session_id, q_stripped, reply)
        return reply

    # ── Step 4: Plan ───────────────────────────────────────────────────────────
    try:
        raw_plan = _plan(query, effective_history)
        plan = parse_action_plan(raw_plan)
    except ValueError as e:
        logger.error(f"[AUTO] Planning failed: {e}")
        reply = (
            "I couldn't process that request right now — the planning service returned an error.\n"
            "Please try again in a moment."
        )
        _save_internal_history(session_id, q_stripped, reply)
        return reply

    # ── Step 5: Confirmation gate ──────────────────────────────────────────────
    # Store plan, return prompt. Do NOT execute anything yet.
    if plan.requires_confirmation and plan.actions:
        _pending_confirmations[session_id] = plan
        logger.info(f"[AUTO] session={session_id} awaiting confirmation")
        reply = plan.confirmation_prompt
        _save_internal_history(session_id, q_stripped, reply)
        return reply

    # ── Step 6: Handle pseudo-actions ─────────────────────────────────────────
    if plan.actions and plan.actions[0].type == "needs_clarification":
        reply = plan.actions[0].params.get("question", "Could you clarify?")
        _save_internal_history(session_id, q_stripped, reply)
        return reply

    # ── Step 7: Execute ────────────────────────────────────────────────────────
    result = _executor.execute(plan)
    log_event("agent_response", agent="automation_agent", user_input=query)
    reply = _format_response(plan.preamble, result)

    # Always save to internal history so the next call has context
    # even if supervisor.py doesn't pass history correctly.
    _save_internal_history(session_id, q_stripped, reply)

    return reply


def _format_response(preamble: str | None, result: str) -> str:
    parts = []
    if preamble and preamble.strip():
        parts.append(preamble.strip())
    if result and result.strip():
        parts.append(result.strip())
    parts.append("What would you like to do next?")
    return "\n\n".join(parts)
