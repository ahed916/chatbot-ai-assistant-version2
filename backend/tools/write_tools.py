"""
agents/tools/redmine_write_tools.py

LangChain tools for write operations in Redmine.
Only used by the automation_agent.
All operations are audit-logged in redmine.py.
"""
import logging
from langchain_core.tools import tool
import redmine as rm

logger = logging.getLogger(__name__)


# ── Internal User Resolver (NO admin key needed) ─────────────────────────────
def _resolve_user_id(name: str) -> int | None:
    """Resolve a user name to ID using project memberships + assignees."""
    name_lower = name.lower().strip()
    try:
        projects = rm.list_projects()
        for p in projects:
            for m in rm.list_members(str(p["id"])):
                user = m.get("user", {})
                uname = user.get("name", "").lower().strip()
                # Fuzzy match: substring or word overlap
                if name_lower in uname or uname in name_lower or set(name_lower.split()) & set(uname.split()):
                    return user["id"]
    except Exception:
        pass
    return None


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

        # Resolve tracker name to ID
        trackers = rm.list_trackers()
        tracker_id = next(
            (t["id"] for t in trackers if t["name"].lower() == tracker.lower()), 1
        )

        # Resolve priority name to ID
        priority_map = {"low": 1, "normal": 2, "high": 3, "urgent": 4, "immediate": 5}
        priority_id = priority_map.get(priority.lower(), 2)

        # Resolve assignee name to ID
        assigned_to_id = None
        if assignee_name:
            assigned_to_id = _resolve_user_id(assignee_name)
            if assigned_to_id is None:
                return f"Could not find user '{assignee_name}'. Issue not created. Check spelling or project membership."

        result = rm.create_issue(
            project_id=project_id,
            subject=subject,
            description=description,
            assigned_to_id=assigned_to_id,
            priority_id=priority_id,
            tracker_id=tracker_id,
            due_date=due_date or None,
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

    IMPORTANT: Always call get_allowed_status_transitions before updating status
    to ensure the transition is valid.

    Args:
        issue_id: The Redmine issue ID to update
        new_status: New status name (e.g., 'In Progress', 'Resolved', 'Closed')
        new_assignee_name: New assignee's name (leave empty to keep current)
        new_priority: New priority: 'Low', 'Normal', 'High', 'Urgent', 'Immediate'
        new_due_date: New due date in YYYY-MM-DD format
        notes: Comment/note to add to the issue

    Returns: Confirmation of what was updated.
    """
    try:
        kwargs = {"notes": notes}

        # Resolve status name to ID
        if new_status:
            statuses = rm.list_issue_statuses()
            status_id = next(
                (s["id"] for s in statuses if s["name"].lower() == new_status.lower()), None
            )
            if status_id is None:
                available = ", ".join(s["name"] for s in statuses)
                return f"Status '{new_status}' not found. Available: {available}"
            kwargs["status_id"] = status_id

        # Resolve assignee
        if new_assignee_name:
            assigned_to_id = _resolve_user_id(new_assignee_name)
            if assigned_to_id is None:
                return f"User '{new_assignee_name}' not found. Issue not updated."
            kwargs["assigned_to_id"] = assigned_to_id

        # Resolve priority
        if new_priority:
            priority_map = {"low": 1, "normal": 2, "high": 3, "urgent": 4, "immediate": 5}
            kwargs["priority_id"] = priority_map.get(new_priority.lower(), 2)

        if new_due_date:
            kwargs["due_date"] = new_due_date

        error = rm.update_issue(issue_id, **kwargs)

        if error == "NOT_FOUND":
            return f"Issue #{issue_id} not found in Redmine."
        if error and error.startswith("WORKFLOW_ERROR"):
            return (
                f"Workflow error updating issue #{issue_id}: {error}\n"
                f"Tip: Use get_allowed_status_transitions({issue_id}) to see valid transitions."
            )

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
    except Exception as e:
        logger.error(f"[TOOL] update_redmine_issue failed: {e}")
        return f"Error updating issue #{issue_id}: {e}"


@tool
def delete_redmine_issue(issue_id: int) -> str:
    """
    Permanently delete ONE specific Redmine issue by ID.

    ⚠️  IRREVERSIBLE. Only call this for a single, explicitly named issue.
    ⚠️  NEVER call this in a loop to delete multiple issues.
    ⚠️  NEVER call this in response to "delete everything", "delete all", or any
        vague bulk delete request — use request_bulk_delete_confirmation instead.

    Args:
        issue_id: The exact Redmine issue ID to delete (must be a specific number)
    """
    try:
        error = rm.delete_issue(issue_id)
        if error == "NOT_FOUND":
            return f"Issue #{issue_id} not found — it may have already been deleted."
        return f"🗑️ Issue #{issue_id} has been permanently deleted."
    except Exception as e:
        logger.error(f"[TOOL] delete_redmine_issue failed: {e}")
        return f"Error deleting issue #{issue_id}: {e}"


@tool
def request_bulk_delete_confirmation(issue_ids: list[int], reason: str) -> str:
    """
    Use this INSTEAD of delete_redmine_issue when the PM asks to delete
    multiple issues or uses vague language like "delete everything", "delete all",
    "clean up", "remove all issues".

    This tool does NOT delete anything. It returns a confirmation prompt
    that the PM must explicitly approve before any deletion happens.

    Args:
        issue_ids: List of issue IDs that WOULD be deleted
        reason: Why these issues were selected (e.g., "all open issues in project X")
    """
    count = len(issue_ids)
    ids_preview = ", ".join(f"#{i}" for i in issue_ids[:10])
    more = f" ... and {count - 10} more" if count > 10 else ""

    return (
        f"⚠️ CONFIRMATION REQUIRED — Bulk Delete\n\n"
        f"You are about to permanently delete {count} issue(s): {ids_preview}{more}\n"
        f"Reason selected: {reason}\n\n"
        f"This action is IRREVERSIBLE. All issue history will be lost.\n\n"
        f"To confirm, please reply: 'Yes, delete those {count} issues'\n"
        f"To cancel, reply: 'Cancel' or ask for something else."
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
    Use this for bulk operations like closing all overdue bugs or reassigning
    all issues from one person.

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

    # Pre-resolve status/assignee/priority once
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
    delete_redmine_issue,
    bulk_update_issues,
    request_bulk_delete_confirmation,  # safety gate for bulk/vague deletes
]
