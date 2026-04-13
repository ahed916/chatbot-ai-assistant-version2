"""
context_builder.py — Lean context injection (schema + stats only)

ARCHITECTURE DECISION — why we changed from full context injection:

OLD APPROACH (wrong at scale):
  - Dump ALL issues + ALL members into every agent prompt
  - Context grows with data: 20 issues=3KB, 500 issues=75KB → context window exceeded
  - Slow even for simple queries ("who is assigned to #5" fetched all 500 issues)
  - Not scalable

NEW APPROACH (correct):
  - Inject ONLY schema (statuses, trackers, projects, user IDs — always tiny)
          and summary stats (counts, distributions — never grows past ~800 chars)
  - Agents call tools for specific issue details they actually need
  - Tools are fast because Redis always has warm cache (prewarmed at startup,
    refreshed on every write)
  - Tool call cost: LLM decides (~8s) + tool executes from cache (~2ms) = fast
  - Scales to thousands of issues with zero degradation
"""
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Optional

import redmine as rm

logger = logging.getLogger(__name__)


def _safe(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning(f"[CONTEXT] {fn.__name__} failed: {e}")
        return [] if "list" in fn.__name__ else {}


def _collect_users_from_memberships(projects: list) -> list:
    """Get user IDs+names from memberships — no admin key needed."""
    seen, users = set(), []
    for p in projects[:10]:
        try:
            for m in rm.list_members(str(p["id"])):
                u = m.get("user", {})
                uid = u.get("id")
                if uid and uid not in seen:
                    seen.add(uid)
                    users.append({"id": uid, "name": u.get("name", "?")})
        except Exception:
            pass
    return users


def build_schema_context() -> str:
    """
    Build SCHEMA-only context: project IDs, status IDs, tracker IDs, user IDs.
    Size: ~500-800 chars regardless of how many issues exist.
    All data is Redis-cached — typically instant after first call.
    """
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_projects = pool.submit(_safe, rm.list_projects)
        f_trackers = pool.submit(_safe, rm.list_trackers)
        f_statuses = pool.submit(_safe, rm.list_issue_statuses)
        projects = f_projects.result()
        trackers = f_trackers.result()
        statuses = f_statuses.result()

    users = _collect_users_from_memberships(projects)

    lines = [
        "=== REDMINE SCHEMA ===",
        "",
        f"PROJECTS ({len(projects)}):",
    ]
    for p in projects[:20]:
        lines.append(f"  [{p['id']}] {p['name']} (identifier: {p.get('identifier','?')})")

    lines += ["", "TRACKERS: " + " | ".join(f"{t['name']}(ID:{t['id']})" for t in trackers)]
    lines += ["", "STATUSES: " + " | ".join(f"{s['name']}(ID:{s['id']})" for s in statuses)]

    if users:
        lines += ["", "KNOWN USERS (IDs for assignments):"]
        for u in users[:30]:
            lines.append(f"  ID:{u['id']} — {u['name']}")

    lines.append("\n=== END SCHEMA ===")
    return "\n".join(lines)


def build_stats_context(project_identifier: Optional[str] = None) -> str:
    """
    Build STATS-only context: counts and distributions, NO issue lists.
    Size: ~600-900 chars regardless of issue count.
    Gives dashboard/risk agents a baseline without fetching individual issues.
    """
    today = date.today().isoformat()

    if project_identifier:
        pid = rm.resolve_project_id(project_identifier)
        issues = _safe(rm.list_issues, pid, "*", 200)
    else:
        issues = _safe(rm.list_issues, None, "*", 200)

    closed_names = {"closed", "rejected", "resolved"}
    open_issues = [i for i in issues
                   if i.get("status", {}).get("name", "").lower() not in closed_names]
    closed = [i for i in issues if i not in open_issues]
    overdue = [i for i in open_issues
               if i.get("due_date") and i["due_date"] < today]
    unassigned = [i for i in open_issues if not i.get("assigned_to")]
    high_prio = [i for i in open_issues
                 if i.get("priority", {}).get("name", "").lower()
                 in ("urgent", "immediate", "high")]

    status_dist: dict[str, int] = {}
    for i in issues:
        s = i.get("status", {}).get("name", "Unknown")
        status_dist[s] = status_dist.get(s, 0) + 1

    prio_dist: dict[str, int] = {}
    for i in open_issues:
        p = i.get("priority", {}).get("name", "Normal")
        prio_dist[p] = prio_dist.get(p, 0) + 1

    tracker_dist: dict[str, int] = {}
    for i in issues:
        t = i.get("tracker", {}).get("name", "Unknown")
        tracker_dist[t] = tracker_dist.get(t, 0) + 1

    workload: dict[str, int] = {}
    workload_overdue: dict[str, int] = {}
    for i in open_issues:
        name = i.get("assigned_to", {}).get("name")
        if name:
            workload[name] = workload.get(name, 0) + 1
            if i.get("due_date") and i["due_date"] < today:
                workload_overdue[name] = workload_overdue.get(name, 0) + 1

    scope = f"Project: {project_identifier}" if project_identifier else "All Projects"
    lines = [
        f"=== REDMINE STATS ({scope}, {today}) ===",
        "",
        f"TOTALS: {len(issues)} total | {len(open_issues)} open | {len(closed)} closed"
        f" | {len(overdue)} overdue | {len(unassigned)} unassigned | {len(high_prio)} high-priority",
        "",
        "STATUS: " + " | ".join(f"{k}:{v}" for k, v in
                                sorted(status_dist.items(), key=lambda x: -x[1])),
        "PRIORITY: " + " | ".join(f"{k}:{v}" for k, v in
                                  sorted(prio_dist.items(), key=lambda x: -x[1])),
        "TRACKER: " + " | ".join(f"{k}:{v}" for k, v in
                                 sorted(tracker_dist.items(), key=lambda x: -x[1])),
        "",
        "WORKLOAD (open issues per person):",
    ]
    for name, count in sorted(workload.items(), key=lambda x: -x[1]):
        od = workload_overdue.get(name, 0)
        flag = f" [{od} overdue]" if od else ""
        lines.append(f"  {name}: {count}{flag}")
    if unassigned:
        lines.append(f"  [Unassigned]: {len(unassigned)}")

    lines.append("\n=== END STATS ===")
    return "\n".join(lines)


def inject_context(
    user_query: str,
    project_identifier: Optional[str] = None,
    include_stats: bool = True,
) -> str:
    """
    Build enriched query for an agent.

    Always: schema (IDs for name resolution, ~600 chars, O(1) size)
    Optional: stats summary (~800 chars, O(status_count), not O(issues))
    Never: individual issue lists (agents use tools for those)

    include_stats=False for automation agent (needs IDs, not stats)
    include_stats=True for dashboard/risk agents (need distributions)
    """
    schema = build_schema_context()

    if include_stats:
        stats = build_stats_context(project_identifier)
        context = f"{schema}\n\n{stats}"
    else:
        context = schema

    return (
        f"{context}\n\n"
        f"---\n"
        f"USER REQUEST: {user_query}\n\n"
        f"Schema above gives you IDs to resolve names without extra tool calls.\n"
        f"Use tools to fetch specific issue details. Tools are fast (Redis-cached)."
    )


def inject_schema_only(user_query: str) -> str:
    """Schema only — for automation agent which needs IDs but not stats."""
    return inject_context(user_query, include_stats=False)
