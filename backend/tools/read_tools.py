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
from typing import Optional
from datetime import date, timedelta

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
def get_all_issues_across_projects(status: str = "open", limit: int = 50) -> str:
    """..."""
    try:
        issues = rm.list_issues(status=status, limit=limit)  # was 100, now 50
        if not issues:
            return f"No {status} issues found across all projects."

        today = date.today().isoformat()
        overdue_count = sum(
            1 for i in issues
            if i.get("due_date") and i["due_date"] < today and status != "closed"
        )
        # Aggregate summary instead of listing every issue
        by_project: dict[str, dict] = {}
        for i in issues:
            proj = i.get("project", {}).get("name", "Unknown")
            entry = by_project.setdefault(proj, {"total": 0, "overdue": 0, "high": 0})
            entry["total"] += 1
            if i.get("due_date") and i["due_date"] < today and status != "closed":
                entry["overdue"] += 1
            if i.get("priority", {}).get("name", "") in ("High", "Urgent"):
                entry["high"] += 1

        lines = [
            f"Total {status} issues: {len(issues)} | Overdue: {overdue_count}",
            "--- Per-project breakdown ---"
        ]
        for proj, stats in sorted(by_project.items(), key=lambda x: -x[1]["total"]):
            lines.append(
                f"  {proj}: {stats['total']} open, {stats['overdue']} overdue, {stats['high']} high/urgent"
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


# In tools/read_tools.py — ADD this new tool
@tool
def resolve_user_name(name: str, project_id: Optional[str] = None) -> dict:
    """
    Resolve a user name to their Redmine ID.

    Args:
        name: User name or partial name (e.g., "Abir", "Abir Mobile")
        project_id: Optional project to scope the search

    Returns:
        dict with 'id', 'name', 'found' keys
        Example: {"id": 42, "name": "Abir Mobile", "found": True}

    Note: Uses project memberships + issue assignees — works without admin rights.
    """
    from redmine import list_members, list_projects, list_issues

    name_lower = name.lower().strip()

    # Helper: score how well a user name matches
    def score_match(user_name: str) -> int:
        user_lower = user_name.lower().strip()
        if user_lower == name_lower:
            return 100
        if name_lower in user_lower:
            return 50
        if user_lower in name_lower:
            return 30
        # Word overlap bonus
        name_parts = set(name_lower.split())
        user_parts = set(user_lower.split())
        return 20 * len(name_parts & user_parts)

    candidates = []

    # Search project memberships first (most reliable)
    projects = [project_id] if project_id else [p["id"] for p in list_projects()]
    for pid in projects:
        for m in list_members(str(pid)):
            user = m.get("user", {})
            uid, uname = user.get("id"), user.get("name", "")
            if uid and uname:
                score = score_match(uname)
                if score > 0:
                    candidates.append((score, uid, uname))

    # Fallback: search issue assignees
    if not candidates:
        issues = list_issues(status="*", limit=200)
        for i in issues:
            assignee = i.get("assigned_to", {})
            uid, uname = assignee.get("id"), assignee.get("name", "")
            if uid and uname:
                score = score_match(uname)
                if score > 0:
                    candidates.append((score, uid, uname))

    if not candidates:
        return {"id": None, "name": name, "found": False, "suggestions": []}

    # Return best match
    candidates.sort(key=lambda x: -x[0])
    best_score, best_id, best_name = candidates[0]

    return {
        "id": best_id,
        "name": best_name,
        "found": best_score >= 50,  # Only auto-confirm if confident
        "confidence": best_score,
        "alternatives": [
            {"id": c[1], "name": c[2]}
            for c in candidates[1:4] if c[0] >= 30
        ]
    }


@tool
def get_issues_assigned_to_person(
    person_name: str,
    project_identifier: str = None,
    status: str = "open",
) -> str:
    """
    Get all issues assigned to a specific person (by name).
    Resolves the name to a Redmine user ID first, then fetches their issues.

    Args:
        person_name: Full or partial name, e.g. "Alice", "Alice Smith"
        project_identifier: Optional — scope to one project
        status: 'open', 'closed', or '*' for all
    """
    try:
        project_id = rm.resolve_project_id(project_identifier) if project_identifier else None
        user_info = resolve_user_name.invoke(
            {"name": person_name, "project_id": str(project_id) if project_id else None})

        if not user_info.get("found"):
            alts = user_info.get("alternatives", [])
            suggestions = ", ".join(f'"{a["name"]}"' for a in alts)
            msg = f'Could not find user "{person_name}".'
            if suggestions:
                msg += f" Did you mean: {suggestions}?"
            return msg

        user_id = user_info["id"]
        resolved_name = user_info["name"]

        issues = rm.list_issues_filtered(
            project_id=project_id,
            status=status,
            assigned_to_id=user_id,
            limit=100,
        )

        if not issues:
            return f"No {status} issues assigned to {resolved_name}."

        today = date.today().isoformat()
        lines = [f"Found {len(issues)} {status} issues assigned to {resolved_name}:"]
        overdue_count = 0
        by_priority: dict[str, int] = {}
        by_project: dict[str, int] = {}

        for i in issues:
            due = i.get("due_date", "no due date")
            is_overdue = due and due != "no due date" and due < today
            if is_overdue:
                overdue_count += 1
            flag = " ⚠️ OVERDUE" if is_overdue else ""
            proj = i.get("project", {}).get("name", "Unknown")
            priority = i.get("priority", {}).get("name", "Normal")
            by_priority[priority] = by_priority.get(priority, 0) + 1
            by_project[proj] = by_project.get(proj, 0) + 1
            lines.append(
                f"  - #{i['id']} [{priority}] {i['subject']}"
                f" | Status: {i['status']['name']}"
                f" | Project: {proj}"
                f" | Due: {due}{flag}"
            )

        lines.append(f"\nSummary: {len(issues)} issues, {overdue_count} overdue")
        lines.append(f"By priority: {by_priority}")
        lines.append(f"By project: {by_project}")
        return "\n".join(lines)
    except Exception as e:
        logger.error(f"[TOOL] get_issues_assigned_to_person failed: {e}")
        return f"Error fetching issues for '{person_name}': {e}"


@tool
def get_issues_by_tracker(
    tracker_name: str,
    project_identifier: str = None,
    status: str = "open",
) -> str:
    """
    Get issues filtered by tracker type (Bug, Feature, Task, Support, etc.).

    Args:
        tracker_name: e.g. "Bug", "Feature", "Task"
        project_identifier: Optional project scope
        status: 'open', 'closed', or '*'
    """
    try:
        trackers = rm.list_trackers()
        tracker_name_lower = tracker_name.lower()
        matched = next(
            (t for t in trackers if tracker_name_lower in t["name"].lower()), None
        )
        if not matched:
            names = [t["name"] for t in trackers]
            return f'Tracker "{tracker_name}" not found. Available: {", ".join(names)}'

        tracker_id = matched["id"]
        project_id = rm.resolve_project_id(project_identifier) if project_identifier else None

        issues = rm.list_issues_filtered(
            project_id=project_id,
            status=status,
            tracker_id=tracker_id,
            limit=100,
        )

        if not issues:
            scope = f" in {project_identifier}" if project_identifier else ""
            return f"No {status} {matched['name']} issues found{scope}."

        today = date.today().isoformat()
        by_project: dict[str, int] = {}
        overdue_count = 0
        lines = [f"Found {len(issues)} {status} {matched['name']} issues:"]

        for i in issues:
            proj = i.get("project", {}).get("name", "Unknown")
            by_project[proj] = by_project.get(proj, 0) + 1
            due = i.get("due_date", "no due date")
            is_overdue = due and due != "no due date" and due < today
            if is_overdue:
                overdue_count += 1
            flag = " ⚠️" if is_overdue else ""
            assignee = i.get("assigned_to", {}).get("name", "Unassigned")
            lines.append(
                f"  - #{i['id']} [{i['priority']['name']}] {i['subject']}"
                f" | {proj} | {assignee} | Due: {due}{flag}"
            )

        lines.append(f"\nBy project: {by_project} | Overdue: {overdue_count}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching {tracker_name} issues: {e}"


@tool
def get_recent_issues(
    days: int = 7,
    event_type: str = "created",
    project_identifier: str = None,
) -> str:
    """
    Get issues created or updated within the last N days.
    Use for "recent activity", "what changed", "this week", "last 2 days" queries.

    Args:
        days: How many days back to look (default 7)
        event_type: 'created' or 'updated'
        project_identifier: Optional project scope
    """
    try:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        project_id = rm.resolve_project_id(project_identifier) if project_identifier else None

        kwargs = {"project_id": project_id, "status": "*", "limit": 100}
        if event_type == "updated":
            kwargs["updated_after"] = cutoff
        else:
            kwargs["created_after"] = cutoff

        issues = rm.list_issues_filtered(**kwargs)

        if not issues:
            scope = f" in {project_identifier}" if project_identifier else ""
            return f"No issues {event_type} in the last {days} days{scope}."

        by_project: dict[str, int] = {}
        by_status: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        lines = [f"Found {len(issues)} issues {event_type} in the last {days} days:"]

        for i in issues:
            proj = i.get("project", {}).get("name", "Unknown")
            status = i.get("status", {}).get("name", "Unknown")
            priority = i.get("priority", {}).get("name", "Normal")
            by_project[proj] = by_project.get(proj, 0) + 1
            by_status[status] = by_status.get(status, 0) + 1
            by_priority[priority] = by_priority.get(priority, 0) + 1
            assignee = i.get("assigned_to", {}).get("name", "Unassigned")
            ts = i.get("created_on" if event_type == "created" else "updated_on", "?")
            lines.append(
                f"  - #{i['id']} [{priority}] {i['subject']}"
                f" | {proj} | {status} | {assignee} | {ts[:10]}"
            )

        lines.append(f"\nBy project: {by_project}")
        lines.append(f"By status: {by_status}")
        lines.append(f"By priority: {by_priority}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching recent issues: {e}"


@tool
def get_unassigned_issues(
    project_identifier: str = None,
    status: str = "open",
) -> str:
    """
    Get all issues with no assignee.
    Useful for 'unassigned issues', 'work without an owner' queries.

    Args:
        project_identifier: Optional project scope
        status: 'open', 'closed', or '*'
    """
    try:
        project_id = rm.resolve_project_id(project_identifier) if project_identifier else None
        # Redmine: assigned_to_id=0 means unassigned (or filter client-side)
        all_issues = rm.list_issues_filtered(
            project_id=project_id, status=status, limit=200
        )
        issues = [i for i in all_issues if not i.get("assigned_to")]

        if not issues:
            scope = f" in {project_identifier}" if project_identifier else ""
            return f"No unassigned {status} issues{scope}. Great!"

        today = date.today().isoformat()
        by_project: dict[str, int] = {}
        overdue_count = 0
        lines = [f"Found {len(issues)} unassigned {status} issues:"]

        for i in issues:
            proj = i.get("project", {}).get("name", "Unknown")
            by_project[proj] = by_project.get(proj, 0) + 1
            due = i.get("due_date", "no due date")
            is_overdue = due and due != "no due date" and due < today
            if is_overdue:
                overdue_count += 1
            flag = " ⚠️ OVERDUE" if is_overdue else ""
            lines.append(
                f"  - #{i['id']} [{i['priority']['name']}] {i['subject']}"
                f" | {proj} | Due: {due}{flag}"
            )

        lines.append(f"\nBy project: {by_project} | Overdue: {overdue_count}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error fetching unassigned issues: {e}"


"""@tool
def get_risk_overview(project_identifier: str = None) -> str:
    """

# Generate a risk assessment combining: overdue issues, high/urgent priority,
# unassigned open issues, and projects with no recent activity.
# Use for: 'risk overview', 'biggest problems', 'what to fix first', 'what's at risk'.

# Args:
# project_identifier: Optional — scope to one project
"""
    #try:
        #today = date.today().isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        project_id = rm.resolve_project_id(project_identifier) if project_identifier else None

        issues = rm.list_issues_filtered(
            project_id=project_id, status="open", limit=100
        )

        overdue = []
        high_priority = []
        unassigned = []
        by_project_risk: dict[str, dict] = {}

        for i in issues:
            proj = i.get("project", {}).get("name", "Unknown")
            if proj not in by_project_risk:
                by_project_risk[proj] = {"overdue": 0, "high": 0, "unassigned": 0, "total": 0}
            by_project_risk[proj]["total"] += 1

            due = i.get("due_date")
            if due and due < today:
                overdue.append(i)
                by_project_risk[proj]["overdue"] += 1

            priority = i.get("priority", {}).get("name", "")
            if priority in ("High", "Urgent"):
                high_priority.append(i)
                by_project_risk[proj]["high"] += 1

            if not i.get("assigned_to"):
                unassigned.append(i)
                by_project_risk[proj]["unassigned"] += 1

        # Score each project by risk
        risk_scores = {
            proj: (stats["overdue"] * 3 + stats["high"] * 2 + stats["unassigned"])
            for proj, stats in by_project_risk.items()
        }
        ranked = sorted(risk_scores.items(), key=lambda x: -x[1])

        lines = [
            f"RISK OVERVIEW — {today}",
            f"Total open issues: {len(issues)}",
            f"Overdue: {len(overdue)} | High/Urgent: {len(high_priority)} | Unassigned: {len(unassigned)}",
            "",
            "Projects ranked by risk (overdue×3 + high×2 + unassigned):",
        ]
        for proj, score in ranked[:10]:
            s = by_project_risk[proj]
            lines.append(
                f"  [{score:3d}] {proj}: {s['overdue']} overdue, "
                f"{s['high']} high/urgent, {s['unassigned']} unassigned"
            )

        if overdue:
            lines.append(f"\nTop overdue issues:")
            for i in sorted(overdue, key=lambda x: x.get("due_date", ""))[:5]:
                days_late = (
                    date.today() - date.fromisoformat(i["due_date"])
                ).days
                lines.append(
                    f"  #{i['id']} {i['subject']} — {days_late}d late"
                    f" | {i.get('project', {}).get('name', '?')}"
                    f" | {i.get('assigned_to', {}).get('name', 'Unassigned')}"
                )

        return "\n".join(lines)
    except Exception as e:
        return f"Error generating risk overview: {e}" """


# ── Exported list for agent creation ─────────────────────────────────────────
READ_TOOLS = [
    get_all_projects,
    get_project_issues,
    get_issue_details,
    get_all_issues_across_projects,
    get_project_members,
    get_available_statuses,
    get_available_trackers,
    resolve_user_name,
    get_allowed_status_transitions,
    get_workload_by_member,
    get_issues_assigned_to_person,
    get_issues_by_tracker,
    get_recent_issues,
    get_unassigned_issues,
    # get_risk_overview,

]
