"""
agents/tools/write_tools.py

LangChain tools for write operations in Redmine.
Only used by the automation_agent.
All operations are audit-logged in redmine.py.

Fix: _resolve_user_id uses exact full-name match first, then a stricter
  word-set check that requires ALL query words to appear in the candidate name.
  Falls back to single-token whole-word match only.

Fix: request_bulk_delete_confirmation now registers _pending_bulk_delete state
  inside the tool itself (via _current_session_id ContextVar imported from
  automation_agent). This matches the pattern used by request_delete_confirmation
  and ensures the confirmation state is set exactly once — eliminating the
  double-ask bug that occurred when the post-run tool_call scan also tried to
  register state.

  NOTE: importing from automation_agent here creates a circular dependency risk.
  To avoid it, _pending_bulk_delete and _current_session_id are imported directly
  from automation_agent at call time (lazy import inside the tool function).
"""
import logging
from langchain_core.tools import tool
import redmine as rm
from datetime import date

logger = logging.getLogger(__name__)


# ── Internal User Resolver ────────────────────────────────────────────────────

def _resolve_user_id(name: str) -> int | None:
    """
    Resolve a user display name to its Redmine ID using project memberships.

    Match priority (first match wins):
      1. Exact full-name match (case-insensitive)
      2. All words in the query appear in the candidate name
      3. Single-token query is a whole-word match inside the candidate
    """
    name_stripped = name.strip()
    name_lower = name_stripped.lower()
    query_words = set(name_lower.split())

    candidates: list[tuple[int, str]] = []

    try:
        projects = rm.list_projects()
        seen_ids: set[int] = set()
        for p in projects:
            for m in rm.list_members(str(p["id"])):
                user = m.get("user", {})
                uid = user.get("id")
                uname = user.get("name", "")
                if uid and uid not in seen_ids:
                    seen_ids.add(uid)
                    candidates.append((uid, uname))
    except Exception:
        return None

    # Pass 1 — exact match
    for uid, uname in candidates:
        if uname.lower() == name_lower:
            return uid

    # Pass 2 — all query words present in candidate name
    if len(query_words) > 1:
        for uid, uname in candidates:
            uname_lower = uname.lower()
            if all(w in uname_lower for w in query_words):
                return uid

    # Pass 3 — single token, whole-word match only
    if len(query_words) == 1:
        import re
        pattern = re.compile(r'\b' + re.escape(name_lower) + r'\b')
        for uid, uname in candidates:
            if pattern.search(uname.lower()):
                return uid

    return None


# ── Write tools ───────────────────────────────────────────────────────────────

@tool
def create_redmine_issue(
    project_identifier: str,
    subject: str,
    description: str = "",
    assignee_name: str = "",
    priority: str = "Normal",
    tracker: str = "Task",
    due_date: str = "",
) -> str:
    """
    Create a new issue in a Redmine project.

    Args:
        project_identifier: Project name, identifier, or numeric ID
        subject: Issue title/subject
        description: Detailed description of the issue
        assignee_name: Name of the person to assign (leave empty for unassigned)
        priority: 'Low', 'Normal', 'High', 'Urgent', 'Immediate'
        tracker: 'Bug', 'Feature', 'Task', 'Support' (depends on your Redmine setup)
        due_date: Due date in YYYY-MM-DD format (leave empty if none)

    Returns: Confirmation with the created issue ID.
    """
    try:
        project_id = rm.resolve_project_id(project_identifier)

        trackers = rm.list_trackers()
        tracker_id = next(
            (t["id"] for t in trackers if t["name"].lower() == tracker.lower()), 1
        )

        priority_map = {"low": 1, "normal": 2, "high": 3, "urgent": 4, "immediate": 5}
        priority_id = priority_map.get(priority.lower(), 2)

        assigned_to_id = None
        if assignee_name:
            assigned_to_id = _resolve_user_id(assignee_name)
            if assigned_to_id is None:
                return (
                    f"❌ User '{assignee_name}' was not found in any project membership. "
                    f"Issue was NOT created. "
                    f"Ask the user to check the exact display name as it appears in Redmine."
                )

        result = rm.create_issue(
            project_id=project_id,
            subject=subject,
            description=description,
            assigned_to_id=assigned_to_id,
            priority_id=priority_id,
            tracker_id=tracker_id,
            due_date=due_date or None,
            start_date=date.today().isoformat(),
        )

        if not result:
            return "Issue creation failed — Redmine returned empty response."

        return (
            f"✅ Issue #{result['id']} created successfully.\n"
            f"  Title: {result.get('subject')}\n"
            f"  Project: {project_identifier}\n"
            f"  Assignee: {assignee_name or 'Unassigned'}\n"
            f"  Due: {due_date or 'Not set'}"
        )
    except Exception as e:
        logger.error(f"[TOOL] create_redmine_issue failed: {e}")
        return f"Error creating issue: {e}"


@tool
def update_redmine_issue(
    issue_id: int,
    new_status: str = "",
    new_assignee_name: str = "",
    new_priority: str = "",
    new_due_date: str = "",
    notes: str = "",
) -> str:
    """
    Update an existing Redmine issue. Only provide the fields you want to change.

    Args:
        issue_id: The Redmine issue ID to update
        new_status: New status name (e.g., 'In Progress', 'Resolved', 'Closed')
        new_assignee_name: New assignee's name (leave empty to keep current)
        new_priority: New priority: 'Low', 'Normal', 'High', 'Urgent', 'Immediate'
        new_due_date: New due date in YYYY-MM-DD format
        notes: Comment/note to add to the issue

    Returns: Confirmation of what was updated, or a clear error if it failed.
    """
    try:
        kwargs = {"notes": notes}

        if new_status:
            statuses = rm.list_issue_statuses()
            status_id = next(
                (s["id"] for s in statuses if s["name"].lower() == new_status.lower()), None
            )
            if status_id is None:
                available = ", ".join(s["name"] for s in statuses)
                return f"Status '{new_status}' not found. Available statuses: {available}"
            kwargs["status_id"] = status_id

        if new_assignee_name:
            assigned_to_id = _resolve_user_id(new_assignee_name)
            if assigned_to_id is None:
                return f"User '{new_assignee_name}' not found. Issue not updated."
            kwargs["assigned_to_id"] = assigned_to_id

        if new_priority:
            priority_map = {"low": 1, "normal": 2, "high": 3, "urgent": 4, "immediate": 5}
            kwargs["priority_id"] = priority_map.get(new_priority.lower(), 2)

        if new_due_date:
            kwargs["due_date"] = new_due_date

        error = rm.update_issue(issue_id, **kwargs)

        if error is None:
            changes = []
            if new_status:
                changes.append(f"status → {new_status}")
            if new_assignee_name:
                changes.append(f"assignee → {new_assignee_name}")
            if new_priority:
                changes.append(f"priority → {new_priority}")
            if new_due_date:
                changes.append(f"due date → {new_due_date}")
            if notes:
                changes.append("note added")
            return f"✅ Issue #{issue_id} updated: {', '.join(changes) or 'no visible changes'}."

        if error == "NOT_FOUND":
            return f"❌ Issue #{issue_id} not found in Redmine."

        if error.startswith("WORKFLOW_ERROR"):
            try:
                issue = rm.get_issue(issue_id)
                current_status = issue.get("status", {}).get("name", "Unknown")
            except Exception:
                current_status = "Unknown"
            raw_reason = error.replace("WORKFLOW_ERROR:", "").strip()
            return (
                f"❌ Cannot update issue #{issue_id}.\n"
                f"   Current status : {current_status}\n"
                f"   Requested status: {new_status}\n"
                f"   Reason          : The Redmine workflow does not allow this transition.\n"
                f"   Redmine says    : {raw_reason}"
            )

        if error.startswith("VALIDATION_ERROR"):
            raw_reason = error.replace("VALIDATION_ERROR:", "").strip()
            return (
                f"❌ Issue #{issue_id} could not be updated — validation failed.\n"
                f"   Redmine says: {raw_reason}"
            )

        return f"❌ Issue #{issue_id} update failed: {error}"

    except Exception as e:
        logger.error(f"[TOOL] update_redmine_issue failed: {e}")
        return f"❌ Error updating issue #{issue_id}: {e}"


@tool
def request_bulk_delete_confirmation(issue_ids: list[int], reason: str) -> str:
    """
    Use this INSTEAD of deleting multiple issues directly.
    Call when the user asks to delete multiple issues or uses vague language
    like "delete everything", "delete all", "clean up", "remove all issues".

    This tool does NOT delete anything. It registers the pending bulk-delete
    state and returns a confirmation prompt the user must approve.

    Args:
        issue_ids: List of issue IDs that WOULD be deleted
        reason: Why these issues were selected (e.g., "all open issues in project X")
    """
    # Lazy import to avoid circular dependency at module load time
    from agents.automation_agent import _pending_bulk_delete, _current_session_id

    session_id = _current_session_id.get()
    _pending_bulk_delete[session_id] = issue_ids
    logger.info(
        f"[TOOL] session={session_id} registered pending bulk delete "
        f"for {len(issue_ids)} issues"
    )

    from agents.automation_agent import CONFIRMATION_SENTINEL

    count = len(issue_ids)
    ids_preview = ", ".join(f"#{i}" for i in issue_ids[:10])
    more = f" ... and {count - 10} more" if count > 10 else ""

    return (
        CONFIRMATION_SENTINEL +
        f"⚠️ **Confirmation required** — Bulk Delete\n\n"
        f"You are about to permanently delete {count} issue(s): {ids_preview}{more}\n"
        f"Reason: {reason}\n\n"
        f"This action is **irreversible**. All issue history will be lost.\n\n"
        f"Reply **\"Yes\"** to confirm, or **\"No\"** to cancel."
    )


@tool
def bulk_update_issues(
    issue_ids: list[int],
    new_status: str = "",
    new_assignee_name: str = "",
    new_priority: str = "",
    notes: str = "",
) -> str:
    """
    Update multiple issues at once with the same changes.

    Args:
        issue_ids: List of issue IDs to update
        new_status: New status name for all issues (leave empty to skip)
        new_assignee_name: New assignee name for all issues (leave empty to skip)
        new_priority: New priority for all issues (leave empty to skip)
        notes: Note/comment to add to all issues

    Returns: Summary of successes and failures.
    """
    if not issue_ids:
        return "No issue IDs provided."

    success_ids = []
    failed = []

    status_id = None
    if new_status:
        statuses = rm.list_issue_statuses()
        status_id = next(
            (s["id"] for s in statuses if s["name"].lower() == new_status.lower()), None
        )
        if status_id is None:
            available = ", ".join(s["name"] for s in statuses)
            return f"Status '{new_status}' not found. Available: {available}"

    assigned_to_id = None
    if new_assignee_name:
        assigned_to_id = _resolve_user_id(new_assignee_name)
        if assigned_to_id is None:
            return f"User '{new_assignee_name}' not found."

    priority_map = {"low": 1, "normal": 2, "high": 3, "urgent": 4, "immediate": 5}
    priority_id = priority_map.get(new_priority.lower(), None) if new_priority else None

    for issue_id in issue_ids:
        error = rm.update_issue(
            issue_id,
            status_id=status_id,
            assigned_to_id=assigned_to_id,
            priority_id=priority_id,
            notes=notes,
        )
        if error:
            failed.append(f"#{issue_id}: {error}")
        else:
            success_ids.append(issue_id)

    lines = [f"Bulk update complete: {len(success_ids)}/{len(issue_ids)} succeeded."]
    if success_ids:
        lines.append(f"✅ Updated: {', '.join('#' + str(i) for i in success_ids)}")
    if failed:
        lines.append(f"❌ Failed: {', '.join(failed)}")

    return "\n".join(lines)


# ── Exported list for agent creation ─────────────────────────────────────────
WRITE_TOOLS = [
    create_redmine_issue,
    update_redmine_issue,
    bulk_update_issues,
    request_bulk_delete_confirmation,
]