"""
agents/dashboard_agent.py

Bypasses LangGraph ReAct loop entirely.
Flow: one LLM call to decide which tools to call → call tools directly → 
      one LLM call to build the dashboard JSON → done.
No "observation" step, no post-tool LLM roundtrip.
"""
import json
import logging
import concurrent.futures as _cf
from datetime import datetime, timezone
from langchain_core.messages import HumanMessage, SystemMessage
from datetime import date, timedelta

from config import load_prompt
from llm import get_llm
from tools.chart_tools import generate_dashboard_json, generate_quick_stat
from audit import log_event

import redmine as rm
from datetime import date

logger = logging.getLogger(__name__)


# ── Lightweight data fetcher (no LangChain tool overhead) ─────────────────────

def _fetch_project_data(project_identifier: str) -> dict:
    """Fetch everything needed for a single-project dashboard."""
    project_id = rm.resolve_project_id(project_identifier)
    issues = rm.list_issues(project_id=project_id, status="*", limit=100)
    members = rm.list_members(str(project_id))
    today = date.today().isoformat()

    by_status: dict[str, int] = {}
    by_assignee: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    overdue = []

    for i in issues:
        status = i.get("status", {}).get("name", "Unknown")
        by_status[status] = by_status.get(status, 0) + 1

        assignee = i.get("assigned_to", {}).get("name", "Unassigned")
        by_assignee[assignee] = by_assignee.get(assignee, 0) + 1

        priority = i.get("priority", {}).get("name", "Normal")
        by_priority[priority] = by_priority.get(priority, 0) + 1

        due = i.get("due_date")
        if due and due < today and i.get("status", {}).get("is_closed") is False:
            overdue.append(i)

    open_issues = [i for i in issues if not i.get("status", {}).get("is_closed", False)]

    return {
        "project_id": project_id,
        "total": len(issues),
        "open": len(open_issues),
        "overdue": len(overdue),
        "by_status": by_status,
        "by_assignee": by_assignee,
        "by_priority": by_priority,
        "members": len(members),
        "overdue_issues": overdue[:5],
    }


# ── Safe wrapper + error response builder ─────────────────────────────────────

def _fetch_project_data_safe(project_identifier: str) -> tuple[dict | None, str | None]:
    """Returns (data, error_message). On success error is None, on failure data is None."""
    try:
        return _fetch_project_data(project_identifier), None
    except Exception as e:
        return None, str(e)


def _build_not_found_response(project_name: str, error: str) -> str:
    import re
    names = re.findall(r"'([^']+)'\s*\(", error)

    if names:
        # Build a readable inline list instead of bullet points
        if len(names) == 1:
            suggestions = f'"{names[0]}"'
        elif len(names) == 2:
            suggestions = f'"{names[0]}" or "{names[1]}"'
        else:
            quoted = [f'"{n}"' for n in names]
            suggestions = ", ".join(quoted[:-1]) + f", or {quoted[-1]}"

        message = (
            f'I couldn\'t find a project called "{project_name}". '
            f"Did you mean {suggestions}?"
        )
    else:
        message = f'I couldn\'t find project "{project_name}". {error}'

    return json.dumps({"type": "clarification", "message": message})


def _fetch_all_projects_data() -> dict:
    """Fetch aggregated data across all projects."""
    projects = rm.list_projects()
    issues = rm.list_issues(status="*", limit=200)
    today = date.today().isoformat()

    by_project: dict[str, dict] = {}
    for p in projects:
        by_project[p["name"]] = {"total": 0, "open": 0, "overdue": 0, "high": 0}

    for i in issues:
        proj_name = i.get("project", {}).get("name", "Unknown")
        if proj_name not in by_project:
            by_project[proj_name] = {"total": 0, "open": 0, "overdue": 0, "high": 0}

        by_project[proj_name]["total"] += 1
        is_closed = i.get("status", {}).get("is_closed", False)
        if not is_closed:
            by_project[proj_name]["open"] += 1
        due = i.get("due_date")
        if due and due < today and not is_closed:
            by_project[proj_name]["overdue"] += 1
        if i.get("priority", {}).get("name") in ("High", "Urgent"):
            by_project[proj_name]["high"] += 1

    total_open = sum(v["open"] for v in by_project.values())
    total_overdue = sum(v["overdue"] for v in by_project.values())
    most_issues = max(by_project.items(), key=lambda x: x[1]["open"], default=("N/A", {}))
    most_overdue = max(by_project.items(), key=lambda x: x[1]["overdue"], default=("N/A", {}))

    return {
        "projects": len(projects),
        "by_project": by_project,
        "total_open": total_open,
        "total_overdue": total_overdue,
        "most_issues_project": most_issues[0],
        "most_issues_count": most_issues[1].get("open", 0),
        "most_overdue_project": most_overdue[0],
        "most_overdue_count": most_overdue[1].get("overdue", 0),
    }


def _fetch_workload_data(project_identifier: str = None) -> dict:
    """Fetch per-developer workload across all projects or a specific one."""
    today = date.today().isoformat()

    if project_identifier:
        project_id = rm.resolve_project_id(project_identifier)
        issues = rm.list_issues(project_id=project_id, status="open", limit=200)
    else:
        issues = rm.list_issues(status="open", limit=200)

    by_person: dict[str, dict] = {}
    unassigned = 0

    for i in issues:
        assignee = i.get("assigned_to", {}).get("name")
        if not assignee:
            unassigned += 1
            continue
        if assignee not in by_person:
            by_person[assignee] = {"open": 0, "overdue": 0, "high": 0}
        by_person[assignee]["open"] += 1
        due = i.get("due_date")
        if due and due < today:
            by_person[assignee]["overdue"] += 1
        if i.get("priority", {}).get("name") in ("High", "Urgent"):
            by_person[assignee]["high"] += 1

    most_loaded = max(by_person.items(), key=lambda x: x[1]["open"], default=("N/A", {}))
    most_overdue = max(by_person.items(), key=lambda x: x[1]["overdue"], default=("N/A", {}))

    return {
        "by_person": by_person,
        "unassigned": unassigned,
        "most_loaded_name": most_loaded[0],
        "most_loaded_count": most_loaded[1].get("open", 0),
        "most_overdue_name": most_overdue[0],
        "most_overdue_count": most_overdue[1].get("overdue", 0),
    }


def _build_workload_dashboard(data: dict, scope_label: str = "All Projects") -> str:
    by_person = data["by_person"]
    sorted_people = sorted(by_person.items(), key=lambda x: -x[1]["open"])

    open_chart = {
        "type": "bar",
        "title": "Open Issues per Developer",
        "data": [{"label": name, "value": stats["open"]} for name, stats in sorted_people],
        "xKey": "label",
        "yKey": "value",
        "insight": f"{data['most_loaded_name']} has the most open work ({data['most_loaded_count']} issues)",
    }
    overdue_chart = {
        "type": "bar",
        "title": "Overdue Issues per Developer",
        "data": [{"label": name, "value": stats["overdue"]} for name, stats in sorted_people if stats["overdue"] > 0],
        "xKey": "label",
        "yKey": "value",
        "insight": f"{data['most_overdue_name']} has the most overdue issues",
    }

    kpis = [
        {"label": "Team Members", "value": str(len(by_person)), "status": "info"},
        {"label": "Most Loaded",
            "value": f"{data['most_loaded_name']} ({data['most_loaded_count']})", "status": "warning"},
        {"label": "Most Overdue",
            "value": f"{data['most_overdue_name']} ({data['most_overdue_count']})", "status": "critical" if data["most_overdue_count"] > 0 else "good"},
        {"label": "Unassigned", "value": str(data["unassigned"]),
         "status": "warning" if data["unassigned"] > 0 else "good"},
    ]

    charts = [open_chart]
    if overdue_chart["data"]:
        charts.append(overdue_chart)

    dashboard = {
        "type": "dashboard",
        "title": f"Team Workload — {scope_label}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": (
            f"{data['most_loaded_name']} carries the heaviest load with {data['most_loaded_count']} open issues. "
            f"{data['unassigned']} issues are unassigned."
        ),
        "kpis": kpis,
        "charts": charts,
    }
    return json.dumps(dashboard, ensure_ascii=False)


def _build_overdue_dashboard(data: dict) -> str:
    by_project = data["by_project"]
    overdue_data = sorted(
        [{"label": k, "value": v["overdue"]} for k, v in by_project.items() if v["overdue"] > 0],
        key=lambda x: -x["value"]
    )

    charts = [{
        "type": "bar",
        "title": "Overdue Issues by Project",
        "data": overdue_data,
        "xKey": "label",
        "yKey": "value",
        "insight": f"{data['most_overdue_project']} is most behind schedule",
    }]

    kpis = [
        {"label": "Total Overdue", "value": str(data["total_overdue"]),
         "status": "critical" if data["total_overdue"] > 0 else "good"},
        {"label": "Most Overdue Project",
            "value": f"{data['most_overdue_project']} ({data['most_overdue_count']})", "status": "critical"},
        {"label": "Projects Affected", "value": str(
            sum(1 for v in by_project.values() if v["overdue"] > 0)), "status": "warning"},
        {"label": "On Track Projects", "value": str(
            sum(1 for v in by_project.values() if v["overdue"] == 0)), "status": "good"},
    ]

    dashboard = {
        "type": "dashboard",
        "title": "Overdue Issues Report",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": (
            f"{data['total_overdue']} issues are overdue across all projects. "
            f"{data['most_overdue_project']} has the most at {data['most_overdue_count']}."
        ),
        "kpis": kpis,
        "charts": charts,
    }
    return json.dumps(dashboard, ensure_ascii=False)


def _build_priority_dashboard(data: dict) -> str:
    # Re-aggregate by priority from the raw project data
    all_issues = rm.list_issues(status="open", limit=200)
    by_priority: dict[str, int] = {}
    for i in all_issues:
        p = i.get("priority", {}).get("name", "Normal")
        by_priority[p] = by_priority.get(p, 0) + 1

    priority_order = ["Urgent", "High", "Normal", "Low"]
    chart_data = [{"label": p, "value": by_priority.get(p, 0)} for p in priority_order if by_priority.get(p, 0) > 0]

    urgent = by_priority.get("Urgent", 0)
    high = by_priority.get("High", 0)
    total = sum(by_priority.values())

    kpis = [
        {"label": "Urgent", "value": str(urgent), "status": "critical" if urgent > 0 else "good"},
        {"label": "High", "value": str(high), "status": "warning" if high > 0 else "good"},
        {"label": "High+Urgent %", "value": f"{int((urgent + high) / max(total, 1) * 100)}%",
         "status": "warning" if (urgent + high) / max(total, 1) > 0.3 else "good"},
        {"label": "Total Open", "value": str(total), "status": "info"},
    ]

    dashboard = {
        "type": "dashboard",
        "title": "Priority Breakdown",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": (
            f"{urgent} urgent and {high} high-priority issues are open. "
            f"High/Urgent issues make up {int((urgent + high) / max(total, 1) * 100)}% of the backlog."
        ),
        "kpis": kpis,
        "charts": [{"type": "bar", "title": "Issues by Priority", "data": chart_data, "xKey": "label", "yKey": "value"}],
    }
    return json.dumps(dashboard, ensure_ascii=False)


# ── Dashboard builders (pure Python, no LLM needed) ───────────────────────────

def _build_project_dashboard(data: dict, project_name: str) -> str:
    charts = [
        {
            "type": "pie",
            "title": "Issues by Status",
            "data": [{"name": k, "value": v} for k, v in data["by_status"].items()],
            "nameKey": "name",
            "valueKey": "value",
            "insight": f"{data['open']} open issues out of {data['total']} total",
        },
        {
            "type": "bar",
            "title": "Issues by Assignee",
            "data": sorted(
                [{"label": k, "value": v} for k, v in data["by_assignee"].items()],
                key=lambda x: -x["value"]
            )[:10],
            "xKey": "label",
            "yKey": "value",
            "insight": "Workload distribution across team members",
        },
        {
            "type": "bar",
            "title": "Issues by Priority",
            "data": [{"label": k, "value": v} for k, v in data["by_priority"].items()],
            "xKey": "label",
            "yKey": "value",
        },
    ]

    overdue_count = data["overdue"]
    health = "critical" if overdue_count > 5 else "warning" if overdue_count > 0 else "good"

    kpis = [
        {"label": "Total Issues", "value": str(data["total"]), "status": "info"},
        {"label": "Open Issues", "value": str(data["open"]), "status": "warning" if data["open"] > 10 else "good"},
        {"label": "Overdue", "value": str(overdue_count), "status": health},
        {"label": "Team Size", "value": str(data["members"]), "status": "info"},
        {"label": "Completion Rate",
         "value": f"{int((data['total'] - data['open']) / max(data['total'], 1) * 100)}%",
         "status": "good"},
    ]

    dashboard = {
        "type": "dashboard",
        "title": f"{project_name} Dashboard",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": (
            f"{project_name} has {data['open']} open issues out of {data['total']} total. "
            f"{overdue_count} are overdue."
        ),
        "kpis": kpis,
        "charts": charts,
    }
    return json.dumps(dashboard, ensure_ascii=False)


def _build_global_dashboard(data: dict) -> str:
    by_project = data["by_project"]

    open_chart = {
        "type": "bar",
        "title": "Open Issues per Project",
        "data": sorted(
            [{"label": k, "value": v["open"]} for k, v in by_project.items() if v["open"] > 0],
            key=lambda x: -x["value"]
        ),
        "xKey": "label",
        "yKey": "value",
        "insight": f"{data['most_issues_project']} has the most open work ({data['most_issues_count']} issues)",
    }

    overdue_chart = {
        "type": "bar",
        "title": "Overdue Issues per Project",
        "data": sorted(
            [{"label": k, "value": v["overdue"]} for k, v in by_project.items() if v["overdue"] > 0],
            key=lambda x: -x["value"]
        ),
        "xKey": "label",
        "yKey": "value",
        "insight": f"{data['most_overdue_project']} has the most overdue issues",
    }

    kpis = [
        {"label": "Total Projects", "value": str(data["projects"]), "status": "info"},
        {"label": "Open Issues", "value": str(data["total_open"]),
         "status": "warning" if data["total_open"] > 20 else "good"},
        {"label": "Total Overdue", "value": str(
            data["total_overdue"]), "status": "critical" if data["total_overdue"] > 5 else "warning" if data["total_overdue"] > 0 else "good"},
        {"label": "Most Loaded",
            "value": f"{data['most_issues_project']} ({data['most_issues_count']})", "status": "warning"},
        {"label": "Most Overdue",
            "value": f"{data['most_overdue_project']} ({data['most_overdue_count']})", "status": "critical" if data["most_overdue_count"] > 0 else "good"},
    ]

    dashboard = {
        "type": "dashboard",
        "title": "All Projects Overview",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": (
            f"{data['projects']} projects tracked. "
            f"{data['total_open']} open issues total, {data['total_overdue']} overdue."
        ),
        "kpis": kpis,
        "charts": [open_chart, overdue_chart],
    }
    return json.dumps(dashboard, ensure_ascii=False)


# ── Intent classifier (one cheap LLM call) ────────────────────────────────────

_INTENT_SYSTEM = """You are a routing assistant. Classify the user's dashboard request.

Respond with JSON only — no markdown, no explanation:
{
  "scope": "project" | "global",
  "view": "global" | "project" | "workload" | "overdue" | "priority" | "status"
        | "person" | "tracker" | "recent" | "unassigned" | "risk" | "triage",
  "project_name": "<name or null>",
  "person_name": "<name or null>",
  "tracker_name": "<Bug|Feature|Task|null>",
  "days": <integer or null>,
  "event_type": "created" | "updated" | null
}

Rules:
- person name + issues/dashboard → view=person, person_name=<name>
- "workload", "developer", "who is", "assignee", "team", "overloaded" → view=workload
- "overdue", "late", "behind", "deadline" → view=overdue
- "priority", "urgent", "high priority" → view=priority
- "status", "progress", "pipeline" → view=status
- "bug", "bugs", "feature", "task", "tracker" → view=tracker, tracker_name=<type>
- "recent", "this week", "last N days", "what changed", "activity" → view=recent, days=7 (or N)
- "updated", "modified", "changed" → view=recent, event_type=updated
- "unassigned", "no owner", "without assignee" → view=unassigned
- "risk", "at risk", "biggest problems", "most problematic" → view=risk
- "triage", "what to fix first", "prioritize", "combining workload and overdue" → view=triage
- specific project named → scope=project, view=project, project_name=<name>
- "all projects", "global", "overview", no qualifier → view=global
"""


def _classify_intent(query: str) -> dict:
    try:
        llm = get_llm()
        response = llm.invoke([
            SystemMessage(content=_INTENT_SYSTEM),
            HumanMessage(content=query),
        ])
        raw = response.content.strip().strip("```json").strip("```").strip()
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"[DASHBOARD AGENT] Intent classification failed: {e}")
        return {"scope": "global", "view": "global", "project_name": None, "person_name": None}


# ── Main entry point ───────────────────────────────────────────────────────────

def run_dashboard_agent(
    query: str, history: list = None, session_id: str = "default"
) -> str:
    history = history or []

    def _build() -> str:
        intent = _classify_intent(query)
        logger.info(f"[DASHBOARD AGENT] intent={intent}")
        view = intent.get("view", "global")
        project_name = intent.get("project_name")
        person_name = intent.get("person_name")
        tracker_name = intent.get("tracker_name")
        days = intent.get("days") or 7
        event_type = intent.get("event_type") or "created"

        # ── person scoped ──────────────────────────────────────────────────────
        if view == "person" and person_name:
            try:
                data = _fetch_person_data(person_name, project_name)
                return _build_person_dashboard(data)
            except Exception as e:
                return _build_not_found_response(person_name, str(e))

        # ── project scoped ────────────────────────────────────────────────────
        elif view == "project" and project_name:
            data, error = _fetch_project_data_safe(project_name)
            if error:
                return _build_not_found_response(project_name, error)
            return _build_project_dashboard(data, project_name)

        # ── workload ──────────────────────────────────────────────────────────
        elif view == "workload":
            if project_name:
                try:
                    data = _fetch_workload_data(project_name)
                    return _build_workload_dashboard(data, project_name)
                except Exception as e:
                    return _build_not_found_response(project_name, str(e))
            data = _fetch_workload_data()
            return _build_workload_dashboard(data)

        # ── overdue ───────────────────────────────────────────────────────────
        elif view == "overdue":
            data = _fetch_all_projects_data()
            return _build_overdue_dashboard(data)

        # ── priority ──────────────────────────────────────────────────────────
        elif view == "priority":
            data = _fetch_all_projects_data()
            return _build_priority_dashboard(data)

        # ── risk ──────────────────────────────────────────────────────────────
        elif view in ("risk",):
            data = _fetch_risk_data(project_name)
            return _build_risk_dashboard(data)

        # ── triage ────────────────────────────────────────────────────────────
        elif view == "triage":
            data = _fetch_risk_data(project_name)
            return _build_triage_dashboard(data)

        # ── recent activity ───────────────────────────────────────────────────
        elif view == "recent":
            data = _fetch_recent_data(days=days, event_type=event_type,
                                      project_identifier=project_name)
            return _build_recent_dashboard(data)

        # ── unassigned ────────────────────────────────────────────────────────
        elif view == "unassigned":
            issues = rm.list_issues_filtered(
                project_id=rm.resolve_project_id(project_name) if project_name else None,
                status="open", limit=200
            )
            unassigned = [i for i in issues if not i.get("assigned_to")]
            today = date.today().isoformat()
            by_project: dict[str, int] = {}
            overdue_count = 0
            for i in unassigned:
                proj = i.get("project", {}).get("name", "Unknown")
                by_project[proj] = by_project.get(proj, 0) + 1
                if i.get("due_date") and i["due_date"] < today:
                    overdue_count += 1

            dashboard = {
                "type": "dashboard",
                "title": "Unassigned Issues",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "summary": f"{len(unassigned)} unassigned open issues. {overdue_count} are overdue.",
                "kpis": [
                    {"label": "Unassigned", "value": str(len(unassigned)), "status": "warning"},
                    {"label": "Overdue", "value": str(overdue_count),
                     "status": "critical" if overdue_count else "good"},
                    {"label": "Projects Affected", "value": str(len(by_project)), "status": "info"},
                ],
                "charts": [{
                    "type": "bar",
                    "title": "Unassigned Issues by Project",
                    "data": sorted([{"label": k, "value": v} for k, v in by_project.items()],
                                   key=lambda x: -x["value"]),
                    "xKey": "label",
                    "yKey": "value",
                }],
            }
            return json.dumps(dashboard, ensure_ascii=False)

        # ── tracker / type filter ─────────────────────────────────────────────
        elif view == "tracker" and tracker_name:
            trackers = rm.list_trackers()
            matched = next(
                (t for t in trackers if tracker_name.lower() in t["name"].lower()), None
            )
            if not matched:
                return json.dumps({"type": "clarification",
                                   "message": f'Unknown tracker "{tracker_name}".'})

            project_id = rm.resolve_project_id(project_name) if project_name else None
            issues = rm.list_issues_filtered(
                project_id=project_id, tracker_id=matched["id"], status="open", limit=100
            )
            today = date.today().isoformat()
            by_project: dict[str, int] = {}
            by_priority: dict[str, int] = {}
            overdue_count = 0
            for i in issues:
                proj = i.get("project", {}).get("name", "Unknown")
                by_project[proj] = by_project.get(proj, 0) + 1
                priority = i.get("priority", {}).get("name", "Normal")
                by_priority[priority] = by_priority.get(priority, 0) + 1
                if i.get("due_date") and i["due_date"] < today:
                    overdue_count += 1

            dashboard = {
                "type": "dashboard",
                "title": f"Open {matched['name']}s by Project",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "summary": f"{len(issues)} open {matched['name'].lower()}s. {overdue_count} overdue.",
                "kpis": [
                    {"label": f"Total {matched['name']}s", "value": str(len(issues)), "status": "info"},
                    {"label": "Overdue", "value": str(overdue_count),
                     "status": "critical" if overdue_count else "good"},
                    {"label": "Urgent/High",
                     "value": str(by_priority.get("Urgent", 0) + by_priority.get("High", 0)),
                     "status": "warning"},
                ],
                "charts": [
                    {
                        "type": "bar",
                        "title": f"{matched['name']}s by Project",
                        "data": sorted([{"label": k, "value": v} for k, v in by_project.items()],
                                       key=lambda x: -x["value"]),
                        "xKey": "label",
                        "yKey": "value",
                    },
                    {
                        "type": "pie",
                        "title": "Priority Distribution",
                        "data": [{"name": k, "value": v} for k, v in by_priority.items()],
                        "nameKey": "name",
                        "valueKey": "value",
                    },
                ],
            }
            return json.dumps(dashboard, ensure_ascii=False)

        # ── global / fallback ─────────────────────────────────────────────────
        else:
            data = _fetch_all_projects_data()
            return _build_global_dashboard(data)

    try:
        with _cf.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_build)
            result = future.result(timeout=60)

        log_event("agent_response", agent="dashboard_agent",
                  user_input=query, tool_result=result[:200])
        logger.info(f"[DASHBOARD AGENT] Done — is_dashboard={'\"type\": \"dashboard\"' in result}")
        return result

    except _cf.TimeoutError:
        logger.error("[DASHBOARD AGENT] Timeout (60s)")
        return json.dumps({
            "type": "no_data",
            "message": "Dashboard generation timed out. Please try again.",
        })
    except Exception as e:
        logger.error(f"[DASHBOARD AGENT] Error: {e}")
        return json.dumps({
            "type": "no_data",
            "message": f"Could not generate dashboard: {e}",
        })

# ── New data fetchers ─────────────────────────────────────────────────────────


def _fetch_person_data(person_name: str, project_identifier: str = None) -> dict:
    """Fetch all issues assigned to a person, with risk signals."""
    from redmine import resolve_project_id, list_issues_filtered

    # Reuse resolve_user_name logic inline (no LangChain overhead)
    projects = rm.list_projects()
    name_lower = person_name.lower()
    candidates = []
    for p in projects:
        for m in rm.list_members(str(p["id"])):
            user = m.get("user", {})
            uid, uname = user.get("id"), user.get("name", "")
            if uid and uname and name_lower in uname.lower():
                candidates.append((uid, uname))

    if not candidates:
        raise ValueError(f"User '{person_name}' not found in any project")

    # Pick best match (shortest name that contains the search term)
    user_id, resolved_name = min(candidates, key=lambda x: len(x[1]))

    project_id = rm.resolve_project_id(project_identifier) if project_identifier else None
    issues = rm.list_issues_filtered(
        project_id=project_id,
        assigned_to_id=user_id,
        status="open",
        limit=100,
    )

    today = date.today().isoformat()
    by_project: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    overdue = []

    for i in issues:
        proj = i.get("project", {}).get("name", "Unknown")
        by_project[proj] = by_project.get(proj, 0) + 1
        priority = i.get("priority", {}).get("name", "Normal")
        by_priority[priority] = by_priority.get(priority, 0) + 1
        due = i.get("due_date")
        if due and due < today:
            overdue.append(i)

    return {
        "resolved_name": resolved_name,
        "total": len(issues),
        "overdue": len(overdue),
        "by_project": by_project,
        "by_priority": by_priority,
        "overdue_issues": overdue[:5],
        "issues": issues,
    }


def _fetch_recent_data(days: int = 7, event_type: str = "created",
                       project_identifier: str = None) -> dict:
    """Fetch issues created or updated in the last N days."""
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    project_id = rm.resolve_project_id(project_identifier) if project_identifier else None

    kwargs = {"project_id": project_id, "status": "*", "limit": 100}
    if event_type == "updated":
        kwargs["updated_after"] = cutoff
    else:
        kwargs["created_after"] = cutoff

    issues = rm.list_issues_filtered(**kwargs)

    by_project: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}

    for i in issues:
        proj = i.get("project", {}).get("name", "Unknown")
        status = i.get("status", {}).get("name", "Unknown")
        priority = i.get("priority", {}).get("name", "Normal")
        by_project[proj] = by_project.get(proj, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
        by_priority[priority] = by_priority.get(priority, 0) + 1

    return {
        "days": days,
        "event_type": event_type,
        "total": len(issues),
        "by_project": by_project,
        "by_status": by_status,
        "by_priority": by_priority,
    }


def _fetch_risk_data(project_identifier: str = None) -> dict:
    """Compute per-project risk scores from overdue/high/unassigned signals."""
    today = date.today().isoformat()
    project_id = rm.resolve_project_id(project_identifier) if project_identifier else None
    issues = rm.list_issues_filtered(project_id=project_id, status="open", limit=200)

    by_project: dict[str, dict] = {}
    total_overdue = total_high = total_unassigned = 0

    for i in issues:
        proj = i.get("project", {}).get("name", "Unknown")
        if proj not in by_project:
            by_project[proj] = {"overdue": 0, "high": 0, "unassigned": 0, "total": 0, "risk_score": 0}

        by_project[proj]["total"] += 1
        due = i.get("due_date")
        if due and due < today:
            by_project[proj]["overdue"] += 1
            total_overdue += 1
        if i.get("priority", {}).get("name") in ("High", "Urgent"):
            by_project[proj]["high"] += 1
            total_high += 1
        if not i.get("assigned_to"):
            by_project[proj]["unassigned"] += 1
            total_unassigned += 1

    for proj in by_project:
        s = by_project[proj]
        s["risk_score"] = s["overdue"] * 3 + s["high"] * 2 + s["unassigned"]

    ranked = sorted(by_project.items(), key=lambda x: -x[1]["risk_score"])
    highest_risk_project = ranked[0][0] if ranked else "N/A"

    return {
        "by_project": by_project,
        "total_issues": len(issues),
        "total_overdue": total_overdue,
        "total_high": total_high,
        "total_unassigned": total_unassigned,
        "highest_risk_project": highest_risk_project,
        "highest_risk_score": ranked[0][1]["risk_score"] if ranked else 0,
        "ranked": ranked[:8],
    }


# ── New dashboard builders ────────────────────────────────────────────────────

def _build_person_dashboard(data: dict) -> str:
    name = data["resolved_name"]

    kpis = [
        {"label": "Open Issues", "value": str(data["total"]),
         "status": "warning" if data["total"] > 10 else "good"},
        {"label": "Overdue", "value": str(data["overdue"]),
         "status": "critical" if data["overdue"] > 0 else "good"},
        {"label": "Projects", "value": str(len(data["by_project"])), "status": "info"},
        {"label": "Urgent/High",
         "value": str(data["by_priority"].get("Urgent", 0) + data["by_priority"].get("High", 0)),
         "status": "warning"},
    ]

    charts = [
        {
            "type": "bar",
            "title": f"Issues by Project — {name}",
            "data": [{"label": k, "value": v} for k, v in
                     sorted(data["by_project"].items(), key=lambda x: -x[1])],
            "xKey": "label",
            "yKey": "value",
            "insight": f"{data['total']} open issues across {len(data['by_project'])} projects",
        },
        {
            "type": "pie",
            "title": f"Issues by Priority — {name}",
            "data": [{"name": k, "value": v} for k, v in data["by_priority"].items()],
            "nameKey": "name",
            "valueKey": "value",
        },
    ]

    dashboard = {
        "type": "dashboard",
        "title": f"{name}'s Issues",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": (
            f"{name} has {data['total']} open issues across "
            f"{len(data['by_project'])} projects. "
            f"{data['overdue']} are overdue."
        ),
        "kpis": kpis,
        "charts": charts,
    }
    return json.dumps(dashboard, ensure_ascii=False)


def _build_recent_dashboard(data: dict) -> str:
    event = data["event_type"]
    days = data["days"]

    kpis = [
        {"label": f"Issues {event.title()}", "value": str(data["total"]), "status": "info"},
        {"label": "Projects Affected", "value": str(len(data["by_project"])), "status": "info"},
        {"label": "Urgent/High",
         "value": str(data["by_priority"].get("Urgent", 0) + data["by_priority"].get("High", 0)),
         "status": "warning"},
    ]

    charts = [
        {
            "type": "bar",
            "title": f"Issues {event.title()} (last {days}d) — by Project",
            "data": [{"label": k, "value": v} for k, v in
                     sorted(data["by_project"].items(), key=lambda x: -x[1])],
            "xKey": "label",
            "yKey": "value",
        },
        {
            "type": "pie",
            "title": "Status Distribution",
            "data": [{"name": k, "value": v} for k, v in data["by_status"].items()],
            "nameKey": "name",
            "valueKey": "value",
        },
    ]

    dashboard = {
        "type": "dashboard",
        "title": f"Recent Activity — Last {days} Days",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": (
            f"{data['total']} issues {event} in the last {days} days "
            f"across {len(data['by_project'])} projects."
        ),
        "kpis": kpis,
        "charts": charts,
    }
    return json.dumps(dashboard, ensure_ascii=False)


def _build_risk_dashboard(data: dict) -> str:
    risk_chart_data = [
        {"label": proj, "value": stats["risk_score"]}
        for proj, stats in data["ranked"]
        if stats["risk_score"] > 0
    ]
    overdue_chart_data = [
        {"label": proj, "value": stats["overdue"]}
        for proj, stats in data["ranked"]
        if stats["overdue"] > 0
    ]

    kpis = [
        {"label": "Total Overdue", "value": str(data["total_overdue"]),
         "status": "critical" if data["total_overdue"] > 0 else "good"},
        {"label": "High/Urgent Open", "value": str(data["total_high"]),
         "status": "warning" if data["total_high"] > 0 else "good"},
        {"label": "Unassigned", "value": str(data["total_unassigned"]),
         "status": "warning" if data["total_unassigned"] > 0 else "good"},
        {"label": "Highest Risk", "value": data["highest_risk_project"],
         "status": "critical"},
    ]

    charts = []
    if risk_chart_data:
        charts.append({
            "type": "bar",
            "title": "Risk Score by Project (overdue×3 + high×2 + unassigned)",
            "data": risk_chart_data,
            "xKey": "label",
            "yKey": "value",
            "insight": f"{data['highest_risk_project']} is most at risk",
        })
    if overdue_chart_data:
        charts.append({
            "type": "bar",
            "title": "Overdue Issues by Project",
            "data": overdue_chart_data,
            "xKey": "label",
            "yKey": "value",
        })

    dashboard = {
        "type": "dashboard",
        "title": "Risk Overview",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": (
            f"{data['total_overdue']} overdue issues, {data['total_high']} high/urgent, "
            f"{data['total_unassigned']} unassigned. "
            f"{data['highest_risk_project']} is the most at-risk project."
        ),
        "kpis": kpis,
        "charts": charts,
    }
    return json.dumps(dashboard, ensure_ascii=False)


def _build_triage_dashboard(data: dict) -> str:
    """'What to fix first' — combines risk + workload signals."""
    kpis = [
        {"label": "Total Overdue", "value": str(data["total_overdue"]),
         "status": "critical" if data["total_overdue"] > 0 else "good"},
        {"label": "High/Urgent", "value": str(data["total_high"]), "status": "warning"},
        {"label": "Unassigned", "value": str(data["total_unassigned"]), "status": "warning"},
        {"label": "Fix First", "value": data["highest_risk_project"], "status": "critical"},
    ]

    charts = [
        {
            "type": "bar",
            "title": "Triage Priority by Project",
            "data": [
                {"label": proj, "value": stats["risk_score"]}
                for proj, stats in data["ranked"][:8]
            ],
            "xKey": "label",
            "yKey": "value",
            "insight": f"Start with {data['highest_risk_project']} — highest combined risk",
        },
    ]

    dashboard = {
        "type": "dashboard",
        "title": "Triage Dashboard — What to Fix First",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": (
            f"Focus on {data['highest_risk_project']} first (risk score: {data['highest_risk_score']}). "
            f"{data['total_overdue']} overdue, {data['total_high']} high/urgent across all projects."
        ),
        "kpis": kpis,
        "charts": charts,
    }
    return json.dumps(dashboard, ensure_ascii=False)
