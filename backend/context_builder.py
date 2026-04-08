"""
context_builder.py — Pre-fetch Redmine context before agent invocation.

THE CORE LATENCY PROBLEM WITH FREE MODELS:
  Each tool call = 1 round trip to the LLM (to decide) + 1 tool execution.
  If the agent calls 5 tools to gather context, that's 5 LLM round trips
  BEFORE it even starts analyzing. On a slow free model = 50-150s wasted.

THE FIX — "Context Injection":
  We fetch all relevant Redmine data in Python (fast, parallel, cached)
  and inject it directly into the agent's initial prompt as a structured
  text block. The agent receives the data ALREADY and can reason immediately
  without needing to call read tools first.

  Tool calls saved: ~4-6 per query
  Latency saved:    ~60-120s on free models

NOTE on list_users():
  Redmine's /users.json requires admin API key. Most project manager keys
  return 403. We NEVER call list_users() in context_builder — instead we
  extract user names directly from issue.assigned_to and project memberships,
  which work with any API key. This gives us the same information without
  admin rights.
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Optional

import redmine as rm

logger = logging.getLogger(__name__)


def _safe(fn, *args, **kwargs):
    """Call a redmine function safely, return [] or {} on error."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning(f"[CONTEXT] {fn.__name__} failed: {e}")
        return [] if "list" in fn.__name__ else {}


def _extract_users_from_issues_and_members(issues: list, members_list: list) -> list:
    """
    Build a user list from issue assignees + project memberships.
    Works with ANY Redmine API key — no admin rights needed.
    Returns list of {"id": int, "name": str} dicts.
    """
    seen_ids = set()
    users = []

    # From issue assignees
    for issue in issues:
        assignee = issue.get("assigned_to", {})
        uid = assignee.get("id")
        name = assignee.get("name", "")
        if uid and uid not in seen_ids:
            seen_ids.add(uid)
            users.append({"id": uid, "name": name})

    # From project memberships (richer: includes role info)
    for m in members_list:
        user = m.get("user", {})
        uid = user.get("id")
        name = user.get("name", "")
        if uid and uid not in seen_ids:
            seen_ids.add(uid)
            users.append({"id": uid, "name": name})

    return users


def build_project_context(project_identifier: Optional[str] = None) -> str:
    """
    Fetch all relevant Redmine data in parallel and return as a formatted
    text block ready to be injected into an agent's initial message.

    Key design decisions:
    - NEVER calls list_users() — requires admin rights, returns 403 for PM keys
    - Instead extracts users from issue.assigned_to + memberships (always works)
    - Always fetches members for ALL projects so automation agent has user IDs
    """
    today = date.today().isoformat()

    # ── Step 1: fetch projects first (needed to get members) ─────────────────
    projects = _safe(rm.list_projects)

    # ── Step 2: parallel fetch of all other data ──────────────────────────────
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            "trackers": pool.submit(_safe, rm.list_trackers),
            "statuses": pool.submit(_safe, rm.list_issue_statuses),
        }

        if project_identifier:
            project_id = rm.resolve_project_id(project_identifier)
            futures["issues"] = pool.submit(_safe, rm.list_issues, project_id, "*", 200)
            futures["members"] = pool.submit(_safe, rm.list_members, project_id)
        else:
            futures["issues"] = pool.submit(_safe, rm.list_issues, None, "*", 200)
            # Fetch members for all projects so we have user IDs without admin key
            futures["members_all"] = pool.submit(_fetch_all_members, projects)

        results = {key: f.result() for key, f in futures.items()}

    trackers = results["trackers"]
    statuses = results["statuses"]
    issues = results["issues"]
    members = results.get("members") or results.get("members_all", [])

    # Build user list from issues + members (no admin key needed)
    users = _extract_users_from_issues_and_members(issues, members)

    # ── Compute derived metrics ───────────────────────────────────────────────
    closed_status_names = {"closed", "rejected", "resolved"}
    open_issues = [i for i in issues
                   if i.get("status", {}).get("name", "").lower() not in closed_status_names]
    closed_issues = [i for i in issues if i not in open_issues]
    overdue = [i for i in open_issues
               if i.get("due_date") and i["due_date"] < today]
    unassigned = [i for i in open_issues if not i.get("assigned_to")]
    high_priority = [i for i in open_issues
                     if i.get("priority", {}).get("name", "").lower() in ("urgent", "immediate", "high")]

    # Workload per member
    workload: dict[str, list] = {}
    for issue in open_issues:
        name = issue.get("assigned_to", {}).get("name")
        if name:
            workload.setdefault(name, []).append(issue)

    # Distributions
    status_dist: dict[str, int] = {}
    for i in issues:
        s = i.get("status", {}).get("name", "Unknown")
        status_dist[s] = status_dist.get(s, 0) + 1

    priority_dist: dict[str, int] = {}
    for i in open_issues:
        p = i.get("priority", {}).get("name", "Normal")
        priority_dist[p] = priority_dist.get(p, 0) + 1

    tracker_dist: dict[str, int] = {}
    for i in issues:
        t = i.get("tracker", {}).get("name", "Unknown")
        tracker_dist[t] = tracker_dist.get(t, 0) + 1

    # ── Build context text block ──────────────────────────────────────────────
    lines = [
        f"=== REDMINE CONTEXT (fetched {today}) ===",
        "",
        f"PROJECTS ({len(projects)} total):",
    ]
    for p in projects[:10]:
        lines.append(f"  [{p['id']}] {p['name']} (identifier: {p.get('identifier', 'N/A')})")

    lines += [
        "",
        f"TRACKERS: {', '.join(t['name'] + ' (ID:' + str(t['id']) + ')' for t in trackers)}",
        f"STATUSES: {', '.join(s['name'] + ' (ID:' + str(s['id']) + ')' for s in statuses)}",
        "",
        "─── ISSUE STATISTICS" +
        (f" — Project: {project_identifier}" if project_identifier else " — All Projects") +
        " ───",
        f"  Total issues:      {len(issues)}",
        f"  Open issues:       {len(open_issues)}",
        f"  Closed/Resolved:   {len(closed_issues)}",
        f"  ⚠️  OVERDUE:        {len(overdue)}",
        f"  🚫 Unassigned:     {len(unassigned)}",
        f"  🔴 High priority:  {len(high_priority)}",
        "",
        "STATUS DISTRIBUTION:",
    ]
    for status, count in sorted(status_dist.items(), key=lambda x: -x[1]):
        lines.append(f"  {status}: {count}")

    lines += ["", "PRIORITY DISTRIBUTION (open issues):"]
    for priority, count in sorted(priority_dist.items(), key=lambda x: -x[1]):
        lines.append(f"  {priority}: {count}")

    lines += ["", "TRACKER DISTRIBUTION:"]
    for tracker, count in sorted(tracker_dist.items(), key=lambda x: -x[1]):
        lines.append(f"  {tracker}: {count}")

    lines += ["", "WORKLOAD PER ASSIGNEE (open issues):"]
    for name, assigned in sorted(workload.items(), key=lambda x: -len(x[1])):
        overdue_count = sum(1 for i in assigned if i.get("due_date") and i["due_date"] < today)
        flag = f" ⚠️ {overdue_count} overdue" if overdue_count else ""
        lines.append(f"  {name}: {len(assigned)} open issues{flag}")
    lines += ["", "UNASSIGNED OPEN ISSUES (full detail for assignment):"]
    if unassigned:
        for i in unassigned:
            tracker = i.get("tracker", {}).get("name", "Unknown")
            priority = i.get("priority", {}).get("name", "Normal")
            project = i.get("project", {}).get("name", "?")
            lines.append(
                f"  #{i['id']} [{tracker}] [{priority}] {i['subject']} | [{project}]"
            )
    else:
        lines.append("  None")

    if overdue:
        lines += ["", f"OVERDUE ISSUES ({len(overdue)}):"]
        for i in overdue[:15]:
            assignee = i.get("assigned_to", {}).get("name", "Unassigned")
            tracker = i.get("tracker", {}).get("name", "Unknown")
            project = i.get("project", {}).get("name", "?")
            lines.append(
                f"  #{i['id']} [{tracker}] [{i['priority']['name']}] {i['subject']}"
                f" | {assignee} | Due: {i['due_date']} | [{project}]"
            )

    if high_priority:
        lines += ["", f"HIGH PRIORITY OPEN ISSUES ({len(high_priority)}):"]
        for i in high_priority[:10]:
            assignee = i.get("assigned_to", {}).get("name", "Unassigned")
            due = i.get("due_date", "no due date")
            lines.append(
                f"  #{i['id']} [{i['priority']['name']}] {i['subject']}"
                f" | {i['status']['name']} | {assignee} | Due: {due}"
            )

    if members:
        lines += ["", "PROJECT MEMBERS (with roles):"]
        for m in members[:30]:
            user = m.get("user", {})
            roles = [r["name"] for r in m.get("roles", [])]
            lines.append(
                f"  {user.get('name', '?')} (ID:{user.get('id','?')}) — {', '.join(roles)}"
            )

    if users:
        lines += ["", f"KNOWN USERS — extracted from issues and memberships (ID → Name):"]
        for u in users[:30]:
            lines.append(f"  ID:{u['id']} — {u['name']}")

    lines += ["", f"ALL OPEN ISSUES ({len(open_issues)}):"]
    for i in open_issues[:50]:
        assignee = i.get("assigned_to", {}).get("name", "Unassigned")
        tracker = i.get("tracker", {}).get("name", "Unknown")
        priority = i.get("priority", {}).get("name", "Normal")
        status = i.get("status", {}).get("name", "Unknown")
        lines.append(
            f"  #{i['id']} [{tracker}] [{priority}] [{status}] {i['subject']} | {assignee}"
        )
    lines.append("\n=== END OF CONTEXT ===")
    context = "\n".join(lines)
    logger.info(
        f"[CONTEXT] Built: {len(issues)} issues, {len(projects)} projects, "
        f"{len(overdue)} overdue, {len(users)} known users ({len(context)} chars)"
    )
    return context


def _fetch_all_members(projects: list) -> list:
    """Fetch members from all projects and flatten into one list."""
    all_members = []
    seen_user_ids = set()
    for project in projects:
        try:
            members = rm.list_members(str(project["id"]))
            for m in members:
                uid = m.get("user", {}).get("id")
                if uid and uid not in seen_user_ids:
                    seen_user_ids.add(uid)
                    all_members.append(m)
        except Exception:
            pass
    return all_members


def inject_context(user_query: str, project_identifier: Optional[str] = None) -> str:
    context = build_project_context(project_identifier)

    # Hard cap at 3000 chars to prevent nvidia model 500 errors
    if len(context) > 3000:
        logger.warning(f"[CONTEXT] Too large ({len(context)} chars), trimming to 3000")
        # Keep everything up to ALL OPEN ISSUES section
        cutoff = context.find("ALL OPEN ISSUES")
        if cutoff > 0:
            context = context[:cutoff] + "\n=== END OF CONTEXT ==="

    return (
        f"{context}\n\n"
        f"---\n"
        f"USER REQUEST: {user_query}\n\n"
        f"Use the context above to answer. "
        f"You already have the data — use your tools only if you need "
        f"additional specific details not covered above."
    )


def build_compact_context(user_query: str, project_identifier: str = None) -> str:
    """
    Compact version: stats + workload only, no individual issue lists.
    Use when full context exceeds ~4000 chars.
    """
    context = build_project_context(project_identifier)
    # Strip the verbose issue-by-issue sections
    lines = context.split("\n")
    compact = []
    skip = False
    for line in lines:
        if any(line.startswith(s) for s in ["OVERDUE ISSUES", "HIGH PRIORITY OPEN", "PROJECT MEMBERS", "KNOWN USERS"]):
            skip = True
        if line.startswith("===") or line.startswith("───"):
            skip = False
        if not skip:
            compact.append(line)

    return (
        "\n".join(compact) + "\n\n---\n"
        f"USER REQUEST: {user_query}\n\n"
        "Use the stats above. Do NOT call read tools. Call generate_dashboard_json once."
    )


def build_stats_only_context(project_identifier: str = None) -> str:
    """
    Absolute minimum context: just the numbers, no lists at all.
    Used as last-resort retry context.
    """
    today = date.today().isoformat()
    projects = _safe(rm.list_projects)

    if project_identifier:
        project_id = rm.resolve_project_id(project_identifier)
        issues = _safe(rm.list_issues, project_id, "*", 100)
    else:
        issues = _safe(rm.list_issues, None, "*", 100)

    closed = {"closed", "rejected", "resolved"}
    open_issues = [i for i in issues if i.get("status", {}).get("name", "").lower() not in closed]
    overdue = [i for i in open_issues if i.get("due_date") and i["due_date"] < today]
    unassigned = [i for i in open_issues if not i.get("assigned_to")]

    workload: dict[str, int] = {}
    for i in open_issues:
        name = i.get("assigned_to", {}).get("name")
        if name:
            workload[name] = workload.get(name, 0) + 1

    workload_str = ", ".join(f"{n}: {c}" for n, c in sorted(workload.items(), key=lambda x: -x[1]))

    return (
        f"PROJECT STATS ({today}):\n"
        f"  Total issues: {len(issues)} | Open: {len(open_issues)} | "
        f"Overdue: {len(overdue)} | Unassigned: {len(unassigned)}\n"
        f"  Workload: {workload_str or 'no data'}\n"
        f"  Projects: {len(projects)}\n"
    )
