"""
Risk Detection Tools for Redmine Project Manager Chatbot.
Used by the Risk Agent to detect and report project risks.

FIX: Added _issue_cache to avoid fetching the same project's issues
multiple times per scan cycle. All 5 tools share one fetch per project_id.
Cache is cleared between scan cycles via clear_issue_cache().

FIX: _resolve() now passes numeric project IDs DIRECTLY to list_issues()
without going through resolve_project_id(). resolve_project_id() requires
the project to appear in list_projects(), which excludes private projects
in some Redmine configurations. Since the scheduler already has the numeric
ID from Redmine memberships, there's no need to resolve it — Redmine's
/issues.json accepts a numeric project_id directly.
"""

from langchain.tools import tool
import redmine as rm
from datetime import date, datetime
from collections import defaultdict
import logging
import threading

logger = logging.getLogger(__name__)

TODAY = date.today()

# ── Per-cycle issue cache ─────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_issue_cache: dict = {}


def clear_issue_cache():
    """Call this once per PM scan cycle to reset the cache."""
    with _cache_lock:
        _issue_cache.clear()


def _get_issues(project_id: str | None, status: str) -> list:
    """Cached wrapper around rm.list_issues()."""
    cache_key = (project_id, status)
    with _cache_lock:
        if cache_key in _issue_cache:
            return _issue_cache[cache_key]

    issues = rm.list_issues(project_id=project_id, status=status, limit=200)

    with _cache_lock:
        _issue_cache[cache_key] = issues
    return issues


def _days_since(date_str: str) -> int:
    try:
        d = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        return (TODAY - d).days
    except Exception:
        return 0


def _days_until(date_str: str) -> int:
    try:
        d = date.fromisoformat(date_str)
        return (d - TODAY).days
    except Exception:
        return 999


def _resolve(project_id: str) -> str | None:
    """
    Return the project_id to pass to rm.list_issues().

    FIX: If project_id is a bare numeric string (e.g. "5", "10"), pass it
    DIRECTLY to the issues endpoint — Redmine accepts numeric project IDs
    in /issues.json without needing to resolve the symbolic identifier first.
    This bypasses resolve_project_id() entirely for the scheduler path,
    which means private projects that don't appear in list_projects() work
    correctly.

    Only call resolve_project_id() when the input looks like a name or
    symbolic identifier (e.g. "mobile-app", "HR Management Platform").
    """
    if not project_id:
        return None

    # Numeric ID — pass straight through, no resolution needed
    if project_id.strip().lstrip('-').isdigit():
        return project_id.strip()

    # Name/identifier — resolve via list_projects()
    try:
        resolved = rm.resolve_project_id(project_id)
        return str(resolved)
    except Exception as e:
        logger.warning("[RISK TOOLS] Could not resolve project %r: %s", project_id, e)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# RISK 1 — Overdue Issues
# ─────────────────────────────────────────────────────────────────────────────

@tool
def detect_overdue_issues(project_id: str = "") -> str:
    """
    RISK DETECTION: Find all open issues that are past their due date.
    project_id: optional filter by project name, identifier, or numeric ID.
    """
    resolved = _resolve(project_id) if project_id else None
    issues = _get_issues(resolved, "open")

    overdue = []
    for i in issues:
        due = i.get("due_date")
        if due and due < TODAY.isoformat():
            days_late = (TODAY - date.fromisoformat(due)).days
            overdue.append((days_late, i))

    if not overdue:
        return "✅ RISK CHECK PASSED: No overdue issues found."

    overdue.sort(reverse=True, key=lambda x: x[0])
    lines = [f"🚨 RISK: {len(overdue)} OVERDUE ISSUE(S) DETECTED\n"]
    for days_late, i in overdue:
        assignee = i.get("assigned_to", {}).get("name", "unassigned")
        project = i.get("project", {}).get("name", "?")
        lines.append(
            f"  ⏰ #{i['id']} [{project}] {i['subject']}\n"
            f"     → Overdue by {days_late} day(s) | Assignee: {assignee} "
            f"| Due: {i['due_date']} | Status: {i['status']['name']}"
        )
    lines.append(f"\n📊 IMPACT: {len(overdue)} issue(s) are blocking on-time delivery.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# RISK 2 — High Priority Issues Due Soon
# ─────────────────────────────────────────────────────────────────────────────

@tool
def detect_urgent_due_soon(project_id: str = "", days_threshold: int = 3) -> str:
    """
    RISK DETECTION: Find high/urgent priority issues due within the next N days.
    project_id: optional project filter.
    days_threshold: number of days to look ahead (default: 3).
    """
    resolved = _resolve(project_id) if project_id else None
    issues = _get_issues(resolved, "open")  # cache hit — no second HTTP call

    HIGH_PRIORITY_IDS = {3, 4, 5}
    HIGH_PRIORITY_NAMES = {"high", "urgent", "immediate"}

    at_risk = []
    for i in issues:
        due = i.get("due_date")
        if not due:
            continue
        priority_name = i.get("priority", {}).get("name", "").lower()
        priority_id_val = i.get("priority", {}).get("id", 0)
        is_high = priority_id_val in HIGH_PRIORITY_IDS or priority_name in HIGH_PRIORITY_NAMES
        if not is_high:
            continue
        days_left = _days_until(due)
        if 0 <= days_left <= days_threshold:
            at_risk.append((days_left, i))

    if not at_risk:
        return f"✅ RISK CHECK PASSED: No high-priority issues due within {days_threshold} days."

    at_risk.sort(key=lambda x: x[0])
    lines = [f"🚨 RISK: {len(at_risk)} HIGH-PRIORITY ISSUE(S) DUE IN ≤{days_threshold} DAYS\n"]
    for days_left, i in at_risk:
        assignee = i.get("assigned_to", {}).get("name", "unassigned")
        priority = i.get("priority", {}).get("name", "?")
        project = i.get("project", {}).get("name", "?")
        urgency = "TODAY" if days_left == 0 else f"in {days_left} day(s)"
        lines.append(
            f"  🚨 #{i['id']} [{project}] {i['subject']}\n"
            f"     → Due {urgency} | Priority: {priority} | Assignee: {assignee}"
        )
    lines.append(f"\n⚡ RECOMMENDATION: Escalate these issues immediately to avoid breach.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# RISK 3 — Stuck / Not Updated Issues
# ─────────────────────────────────────────────────────────────────────────────

@tool
def detect_stuck_issues(project_id: str = "", stale_days: int = 5) -> str:
    """
    RISK DETECTION: Find in-progress issues with no updates for N+ days.
    project_id: optional project filter.
    stale_days: inactivity threshold in days (default: 5).
    """
    resolved = _resolve(project_id) if project_id else None
    issues = _get_issues(resolved, "*")  # separate cache key from "open"

    IN_PROGRESS_NAMES = {"in progress", "in_progress", "doing", "wip", "started"}

    stuck = []
    for i in issues:
        status_name = i.get("status", {}).get("name", "").lower()
        if not any(s in status_name for s in IN_PROGRESS_NAMES):
            continue
        updated = i.get("updated_on", "")
        if not updated:
            continue
        days_idle = _days_since(updated)
        if days_idle >= stale_days:
            stuck.append((days_idle, i))

    if not stuck:
        return f"✅ RISK CHECK PASSED: No stuck issues (all in-progress updated within {stale_days} days)."

    stuck.sort(reverse=True, key=lambda x: x[0])
    lines = [f"⚠️ RISK: {len(stuck)} STUCK ISSUE(S) WITH NO PROGRESS ≥{stale_days} DAYS\n"]
    for days_idle, i in stuck:
        assignee = i.get("assigned_to", {}).get("name", "unassigned")
        project = i.get("project", {}).get("name", "?")
        lines.append(
            f"  💤 #{i['id']} [{project}] {i['subject']}\n"
            f"     → No update for {days_idle} day(s) | Assignee: {assignee} "
            f"| Status: {i['status']['name']}"
        )
    lines.append(
        f"\n🔍 RECOMMENDATION: Check with assignees for blockers. "
        f"Consider reassigning or breaking issues into smaller tasks."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# RISK 4 — Unassigned Open Issues
# ─────────────────────────────────────────────────────────────────────────────

@tool
def detect_unassigned_issues(project_id: str = "") -> str:
    """
    RISK DETECTION: Find open issues with no assignee.
    project_id: optional project filter.
    """
    resolved = _resolve(project_id) if project_id else None
    issues = _get_issues(resolved, "open")  # cache hit

    unassigned = [i for i in issues if not i.get("assigned_to")]
    if not unassigned:
        return "✅ RISK CHECK PASSED: All open issues are assigned."

    lines = [f"📌 RISK: {len(unassigned)} UNASSIGNED OPEN ISSUE(S)\n"]
    for i in unassigned:
        due = i.get("due_date", "no due date")
        project = i.get("project", {}).get("name", "?")
        priority = i.get("priority", {}).get("name", "?")
        lines.append(
            f"  👤 #{i['id']} [{project}] {i['subject']}\n"
            f"     → Priority: {priority} | Due: {due} | Status: {i['status']['name']}"
        )
    lines.append(
        f"\n📋 RECOMMENDATION: Assign these issues to team members to ensure accountability."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# RISK 5 — Issues Without Due Dates
# ─────────────────────────────────────────────────────────────────────────────

@tool
def detect_no_due_date_issues(project_id: str = "") -> str:
    """
    RISK DETECTION: Find open issues with no due date assigned.
    project_id: optional project filter.
    """
    resolved = _resolve(project_id) if project_id else None
    issues = _get_issues(resolved, "open")  # cache hit

    no_due = [
        i for i in issues
        if not i.get("due_date")
        and i.get("priority", {}).get("id", 0) >= 2
    ]

    if not no_due:
        return "✅ RISK CHECK PASSED: All significant open issues have due dates."

    lines = [f"⚠️ RISK: {len(no_due)} ISSUE(S) WITHOUT DUE DATE (priority: Normal+)\n"]
    for i in no_due:
        assignee = i.get("assigned_to", {}).get("name", "unassigned")
        project = i.get("project", {}).get("name", "?")
        priority = i.get("priority", {}).get("name", "?")
        lines.append(
            f"  ❗ #{i['id']} [{project}] {i['subject']}\n"
            f"     → Priority: {priority} | Assignee: {assignee}"
        )
    lines.append(
        f"\n📅 RECOMMENDATION: Set due dates so these issues appear in sprint planning."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# RISK 6 — Overloaded Assignees
# ─────────────────────────────────────────────────────────────────────────────

@tool
def detect_overloaded_assignees(project_id: str = "", threshold: int = 10) -> str:
    """
    RISK DETECTION: Identify team members with too many open tasks.
    project_id: optional project filter.
    threshold: max acceptable open issues per person (default: 10).
    """
    resolved = _resolve(project_id) if project_id else None
    issues = _get_issues(resolved, "open")  # cache hit

    load = defaultdict(list)
    for i in issues:
        assignee = i.get("assigned_to")
        if assignee:
            load[assignee["name"]].append(i)

    overloaded = {name: items for name, items in load.items() if len(items) > threshold}

    if not overloaded:
        return f"✅ RISK CHECK PASSED: No team member has more than {threshold} open tasks."

    lines = [f"👤 RISK: {len(overloaded)} OVERLOADED TEAM MEMBER(S) (threshold: {threshold} tasks)\n"]
    for name, items in sorted(overloaded.items(), key=lambda x: -len(x[1])):
        overdue_count = sum(
            1 for i in items
            if i.get("due_date") and i["due_date"] < TODAY.isoformat()
        )
        lines.append(
            f"  🔴 {name}: {len(items)} open issue(s)"
            + (f" — including {overdue_count} overdue!" if overdue_count else "")
        )
    lines.append(
        f"\n⚖️ RECOMMENDATION: Redistribute tasks or adjust sprint scope to reduce bottlenecks."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# RISK 7 — Milestone / Version at Risk
# ─────────────────────────────────────────────────────────────────────────────

@tool
def detect_milestone_risk(project_id: str = "") -> str:
    """
    RISK DETECTION: Find clusters of issues all due in the same week.
    project_id: optional project filter.
    """
    resolved = _resolve(project_id) if project_id else None
    issues = _get_issues(resolved, "open")  # cache hit

    week_buckets = defaultdict(list)
    for i in issues:
        due = i.get("due_date")
        if due:
            try:
                d = date.fromisoformat(due)
                if d >= TODAY:
                    week_key = d.strftime("%Y-W%V")
                    week_buckets[week_key].append(i)
            except Exception:
                pass

    if not week_buckets:
        return "✅ RISK CHECK PASSED: No upcoming deadline clusters detected."

    CRUNCH_THRESHOLD = 5
    crunch_weeks = {w: items for w, items in week_buckets.items() if len(items) >= CRUNCH_THRESHOLD}

    if not crunch_weeks:
        return (
            f"✅ RISK CHECK PASSED: No week has {CRUNCH_THRESHOLD}+ issues due simultaneously.\n"
            f"   Upcoming weeks: " + ", ".join(f"{w} ({len(v)} issues)" for w, v in sorted(week_buckets.items()))
        )

    lines = [f"📅 RISK: {len(crunch_weeks)} DEADLINE CRUNCH WEEK(S) DETECTED\n"]
    for week, items in sorted(crunch_weeks.items()):
        high_prio = sum(1 for i in items if i.get("priority", {}).get("id", 0) >= 3)
        unassigned = sum(1 for i in items if not i.get("assigned_to"))
        lines.append(
            f"  📌 Week {week}: {len(items)} issues due"
            + (f" | {high_prio} high-priority" if high_prio else "")
            + (f" | {unassigned} unassigned ⚠️" if unassigned else "")
        )
    lines.append(
        "\n🗓️ RECOMMENDATION: Review sprint capacity for crunch weeks and redistribute work early."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# RISK 8 — Long-Running Issues
# ─────────────────────────────────────────────────────────────────────────────

@tool
def detect_long_running_issues(project_id: str = "", max_days: int = 30) -> str:
    """
    RISK DETECTION: Find open issues that have been open for more than N days.
    project_id: optional project filter.
    max_days: threshold in days (default: 30).
    """
    resolved = _resolve(project_id) if project_id else None
    issues = _get_issues(resolved, "open")  # cache hit

    long_running = []
    for i in issues:
        created = i.get("created_on", "")
        if not created:
            continue
        age_days = _days_since(created)
        if age_days >= max_days:
            long_running.append((age_days, i))

    if not long_running:
        return f"✅ RISK CHECK PASSED: No open issues older than {max_days} days."

    long_running.sort(reverse=True, key=lambda x: x[0])
    lines = [f"🕰️ RISK: {len(long_running)} ISSUE(S) OPEN FOR ≥{max_days} DAYS\n"]
    for age, i in long_running[:15]:
        assignee = i.get("assigned_to", {}).get("name", "unassigned")
        project = i.get("project", {}).get("name", "?")
        lines.append(
            f"  ⏳ #{i['id']} [{project}] {i['subject']}\n"
            f"     → Open for {age} day(s) | Assignee: {assignee} | Status: {i['status']['name']}"
        )
    lines.append(
        f"\n🔎 RECOMMENDATION: Review and either close, break down, or re-prioritize these issues."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# MASTER — Full Risk Scan
# ─────────────────────────────────────────────────────────────────────────────

@tool
def run_full_risk_scan(project_id: str = "") -> str:
    """
    Run ALL risk detection checks and return a consolidated risk report.
    project_id: optional filter by project name, identifier, or numeric ID.
    """
    clear_issue_cache()

    label = f" for project '{project_id}'" if project_id else " (all projects)"
    report = [
        f"{'='*60}",
        f"📋 FULL RISK SCAN REPORT{label}",
        f"🗓️  Date: {TODAY.isoformat()}",
        f"{'='*60}\n",
    ]

    checks = [
        ("🚨 OVERDUE ISSUES", detect_overdue_issues),
        ("⚡ URGENT & DUE SOON", detect_urgent_due_soon),
        ("💤 STUCK / STALE ISSUES", detect_stuck_issues),
        ("👤 UNASSIGNED ISSUES", detect_unassigned_issues),
        ("❗ MISSING DUE DATES", detect_no_due_date_issues),
        ("🔴 OVERLOADED MEMBERS", detect_overloaded_assignees),
        ("📅 DEADLINE CRUNCH WEEKS", detect_milestone_risk),
        ("🕰️  LONG-RUNNING ISSUES", detect_long_running_issues),
    ]

    risk_count = 0
    for title, check_fn in checks:
        report.append(f"── {title} {'─'*30}")
        try:
            result = check_fn.invoke({"project_id": project_id})
            report.append(result)
            if "RISK:" in result:
                risk_count += 1
        except Exception as e:
            report.append(f"   ⚠️ Check failed: {e}")
        report.append("")

    report.append(f"{'='*60}")
    if risk_count == 0:
        report.append("✅ ALL CHECKS PASSED — Project health looks good!")
    else:
        report.append(f"⚠️  {risk_count}/{len(checks)} RISK AREA(S) DETECTED — Review recommended.")
    report.append(f"{'='*60}")

    return "\n".join(report)