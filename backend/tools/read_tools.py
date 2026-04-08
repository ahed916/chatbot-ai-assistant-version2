"""
agents/tools/redmine_read_tools.py

LangChain tools that wrap redmine.py read functions.
The agent calls these to gather context. All responses are cached via Redis.

Design principle: tools return rich, descriptive strings so the agent has
full context to reason — not raw dicts that it has to interpret.
"""
import json
import logging
from datetime import date
from langchain_core.tools import tool
import redmine as rm

logger = logging.getLogger(__name__)


@tool
def get_all_projects() -> str:
    """
    List all projects in Redmine.
    Returns: project names, IDs, identifiers, and descriptions.
    Use this to discover available projects before querying issue data.
    """
    try:
        projects = rm.list_projects()
        if not projects:
            return "No projects found in Redmine."
        lines = [f"Found {len(projects)} projects:"]
        for p in projects:
            lines.append(
                f"  - [{p['id']}] {p['name']} (identifier: {p.get('identifier', 'N/A')}) "
                f"— {p.get('description', 'No description')}"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"[TOOL] get_all_projects failed: {e}")
        return f"Error fetching projects: {e}"


@tool
def get_project_issues(
    project_identifier: str,
    status: str = "open",
    limit: int = 100,
) -> str:
    """
    Get issues for a specific project.

    Args:
        project_identifier: Project name, identifier, or numeric ID
        status: Filter by status — 'open', 'closed', or '*' for all
        limit: Max number of issues to return (default 100)

    Returns: Detailed issue list with ID, subject, status, priority, assignee, due date.
    Use this to understand a project's current state.
    """
    try:
        project_id = rm.resolve_project_id(project_identifier)
        issues = rm.list_issues(project_id=project_id, status=status, limit=limit)
        if not issues:
            return f"No {status} issues found for project '{project_identifier}'."

        today = date.today().isoformat()
        lines = [f"Found {len(issues)} {status} issues for project '{project_identifier}':"]
        for i in issues:
            due = i.get("due_date", "no due date")
            overdue = " ⚠️ OVERDUE" if due and due < today and status != "closed" else ""
            assignee = i.get("assigned_to", {}).get("name", "Unassigned")
            lines.append(
                f"  - #{i['id']} [{i['priority']['name']}] {i['subject']}"
                f" | Status: {i['status']['name']}"
                f" | Assignee: {assignee}"
                f" | Due: {due}{overdue}"
                f" | Tracker: {i.get('tracker', {}).get('name', 'N/A')}"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"[TOOL] get_project_issues failed: {e}")
        return f"Error fetching issues for '{project_identifier}': {e}"


@tool
def get_issue_details(issue_id: int) -> str:
    """
    Get complete details for a single Redmine issue including description,
    status, priority, assignee, dates, and tracker.

    Args:
        issue_id: The numeric Redmine issue ID
    """
    try:
        issue = rm.get_issue(issue_id)
        if not issue:
            return f"Issue #{issue_id} not found."
        return json.dumps(issue, indent=2, default=str)
    except Exception as e:
        return f"Error fetching issue #{issue_id}: {e}"


@tool
def get_all_issues_across_projects(status: str = "open", limit: int = 100) -> str:
    """
    Get issues across ALL projects (not filtered by project).
    Useful for global overviews, risk analysis, or cross-project summaries.

    Args:
        status: 'open', 'closed', or '*' for all
        limit: Max issues to return
    """
    try:
        issues = rm.list_issues(status=status, limit=limit)
        if not issues:
            return f"No {status} issues found across all projects."

        today = date.today().isoformat()
        overdue_count = sum(
            1 for i in issues
            if i.get("due_date") and i["due_date"] < today and status != "closed"
        )

        lines = [
            f"Found {len(issues)} {status} issues across all projects.",
            f"Overdue: {overdue_count}",
            "---"
        ]
        for i in issues:
            due = i.get("due_date", "none")
            overdue = " ⚠️ OVERDUE" if due and due < today and status != "closed" else ""
            assignee = i.get("assigned_to", {}).get("name", "Unassigned")
            project = i.get("project", {}).get("name", "Unknown")
            lines.append(
                f"  #{i['id']} [{project}] [{i['priority']['name']}] {i['subject']}"
                f" | {i['status']['name']} | {assignee} | Due: {due}{overdue}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching all issues: {e}"


@tool
def get_project_members(project_identifier: str) -> str:
    """
    Get all members of a project with their roles.
    Useful for understanding team composition and identifying who to assign work to.

    Args:
        project_identifier: Project name, identifier, or numeric ID
    """
    try:
        project_id = rm.resolve_project_id(project_identifier)
        members = rm.list_members(project_id)
        if not members:
            return f"No members found for project '{project_identifier}'."

        lines = [f"Members of '{project_identifier}':"]
        for m in members:
            user = m.get("user", {})
            roles = [r["name"] for r in m.get("roles", [])]
            lines.append(
                f"  - {user.get('name', 'Unknown')} (ID: {user.get('id', '?')}) "
                f"| Roles: {', '.join(roles)}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching members for '{project_identifier}': {e}"


@tool
def get_available_statuses() -> str:
    """
    Get all available issue statuses in Redmine.
    Use this before attempting status updates to know valid status IDs.
    """
    try:
        statuses = rm.list_issue_statuses()
        lines = ["Available issue statuses:"]
        for s in statuses:
            closed = " (closed)" if s.get("is_closed") else ""
            lines.append(f"  - ID {s['id']}: {s['name']}{closed}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching statuses: {e}"


@tool
def get_available_trackers() -> str:
    """
    Get all available issue trackers (Bug, Feature, Task, etc.).
    Use this before creating issues to know valid tracker IDs.
    """
    try:
        trackers = rm.list_trackers()
        lines = ["Available trackers:"]
        for t in trackers:
            lines.append(f"  - ID {t['id']}: {t['name']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching trackers: {e}"


@tool
def get_all_users() -> str:
    """Get all users — requires admin API key."""
    return (
        "⚠️ list_users() is not available — API key lacks admin rights.\n"
        "Use the KNOWN USERS section from the injected context instead.\n"
        "All user IDs are already available there."
    )


@tool
def get_allowed_status_transitions(issue_id: int) -> str:
    """
    Get allowed status transitions for a specific issue based on its current workflow.
    ALWAYS call this before updating an issue's status to avoid workflow errors.

    Args:
        issue_id: The Redmine issue ID
    """
    try:
        transitions = rm.get_allowed_transitions(issue_id)
        if not transitions:
            return f"Issue #{issue_id} not found or has no transitions."
        current = transitions["current_status_name"]
        allowed = transitions.get("allowed", [])
        if not allowed:
            return (
                f"Issue #{issue_id} is currently '{current}'. "
                f"No status transitions are allowed from this state."
            )
        lines = [
            f"Issue #{issue_id} current status: '{current}'",
            "Allowed transitions to:"
        ]
        for s in allowed:
            lines.append(f"  - ID {s['id']}: {s['name']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching transitions for issue #{issue_id}: {e}"


@tool
def get_workload_by_member(project_identifier: str) -> str:
    """
    Analyze the workload distribution across team members for a project.
    Shows how many open issues are assigned to each person.
    Useful for identifying overloaded members or unassigned work.

    Args:
        project_identifier: Project name, identifier, or numeric ID
    """
    try:
        project_id = rm.resolve_project_id(project_identifier)
        issues = rm.list_issues(project_id=project_id, status="open", limit=100)

        workload: dict[str, list] = {}
        unassigned = []

        today = date.today().isoformat()

        for issue in issues:
            assignee = issue.get("assigned_to", {})
            name = assignee.get("name", None)
            due = issue.get("due_date")
            overdue = due and due < today
            entry = {
                "id": issue["id"],
                "subject": issue["subject"],
                "priority": issue["priority"]["name"],
                "overdue": overdue,
            }
            if name:
                workload.setdefault(name, []).append(entry)
            else:
                unassigned.append(entry)

        lines = [f"Workload analysis for '{project_identifier}':"]
        for member, items in sorted(workload.items(), key=lambda x: -len(x[1])):
            overdue_count = sum(1 for i in items if i["overdue"])
            lines.append(
                f"\n  {member} — {len(items)} open issues "
                f"({'⚠️ ' + str(overdue_count) + ' overdue' if overdue_count else 'all on track'})"
            )
            for item in items[:5]:  # show first 5
                flag = " ⚠️" if item["overdue"] else ""
                lines.append(f"    #{item['id']} [{item['priority']}] {item['subject']}{flag}")
            if len(items) > 5:
                lines.append(f"    ... and {len(items) - 5} more")

        if unassigned:
            lines.append(f"\n  Unassigned — {len(unassigned)} issues (needs attention)")

        return "\n".join(lines)
    except Exception as e:
        return f"Error analyzing workload for '{project_identifier}': {e}"


# ── Exported list for agent creation ─────────────────────────────────────────
READ_TOOLS = [
    get_all_projects,
    get_project_issues,
    get_issue_details,
    get_all_issues_across_projects,
    get_project_members,
    get_available_statuses,
    get_available_trackers,
    get_allowed_status_transitions,
    get_workload_by_member,
]
