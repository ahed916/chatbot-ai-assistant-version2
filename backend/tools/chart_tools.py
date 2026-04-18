"""
agents/tools/chart_tools.py

Tools for the dashboard agent to generate React-renderable chart configurations.

KEY INSIGHT: Free models often skip tool calls and write JSON directly as text
when they already know the output format. We solve this two ways:
  1. The tool is the ONLY way to produce output with type="dashboard"
  2. The backend also has a fallback parser that catches direct JSON output
"""
import json
import logging
from datetime import datetime, timezone
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


@tool
def generate_dashboard_json(
    charts: list[dict],
    kpis: list[dict],
    summary: str,
    title: str = "Project Dashboard",
) -> str:
    """
    YOU MUST CALL THIS TOOL to produce a dashboard. Do not write JSON yourself.
    This is the ONLY valid way to produce dashboard output.

    Call this with the charts and KPIs you have prepared from the context data.

    Args:
        charts: List of chart configs. Each needs: type, title, data, xKey+yKey (bar/line) or nameKey+valueKey (pie)
        kpis: List of KPI cards. Each needs: label, value, trend, status, context
        summary: 2-sentence project health summary
        title: Dashboard title (default: "Project Dashboard")
    """
    dashboard = {
        "type": "dashboard",
        "title": title,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "kpis": kpis,
        "charts": charts,
    }
    result = json.dumps(dashboard, ensure_ascii=False)
    logger.info(f"[CHART TOOL] Generated dashboard: {len(charts)} charts, {len(kpis)} KPIs")
    return result


@tool
def generate_quick_stat(label: str, value: str, context: str = "") -> str:
    """
    Call this for single-number answers like "how many open bugs?".
    Do not write the JSON yourself — call this tool.

    Args:
        label: What the number represents (e.g., "Open Bugs")
        value: The value as string (e.g., "14")
        context: Optional context (e.g., "across 3 projects")
    """
    stat = {
        "type": "quick_stat",
        "label": label,
        "value": value,
        "context": context,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return json.dumps(stat, ensure_ascii=False)


# In chart_tools.py — add this tool
@tool
def generate_risk_dashboard(risk_scan_text: str) -> str:
    """
    Convert a risk scan text report into a dashboard JSON.
    Use this when the query is about showing risks as a visual dashboard.

    Args:
        risk_scan_text: Raw output from run_full_risk_scan or any risk detection tool
    """
    import re

    # Parse risk counts from the structured text output
    risk_categories = {
        "Overdue": 0,
        "Urgent Due Soon": 0,
        "Stuck": 0,
        "Unassigned": 0,
        "No Due Date": 0,
        "Overloaded": 0,
        "Milestone Risk": 0,
        "Long Running": 0,
    }

    patterns = {
        "Overdue": r"(\d+) OVERDUE ISSUE",
        "Urgent Due Soon": r"(\d+) HIGH-PRIORITY ISSUE",
        "Stuck": r"(\d+) STUCK ISSUE",
        "Unassigned": r"(\d+) UNASSIGNED OPEN ISSUE",
        "No Due Date": r"(\d+) ISSUE\(S\) WITHOUT DUE DATE",
        "Overloaded": r"(\d+) OVERLOADED TEAM MEMBER",
        "Milestone Risk": r"(\d+) DEADLINE CRUNCH WEEK",
        "Long Running": r"(\d+) ISSUE\(S\) OPEN FOR",
    }

    for category, pattern in patterns.items():
        match = re.search(pattern, risk_scan_text, re.IGNORECASE)
        if match:
            risk_categories[category] = int(match.group(1))

    # Filter to only non-zero risks
    active_risks = {k: v for k, v in risk_categories.items() if v > 0}
    total_risks = sum(active_risks.values())

    charts = []
    if active_risks:
        charts.append({
            "type": "bar",
            "title": "Risk Issues by Category",
            "data": [{"label": k, "value": v} for k, v in active_risks.items()],
            "xKey": "label",
            "yKey": "value",
            "insight": f"{max(active_risks, key=active_risks.get)} is the highest risk area"
        })
        charts.append({
            "type": "pie",
            "title": "Risk Distribution",
            "data": [{"name": k, "value": v} for k, v in active_risks.items()],
            "nameKey": "name",
            "valueKey": "value",
        })

    # Determine health
    if total_risks == 0:
        health, health_status = "Healthy", "good"
    elif total_risks <= 5:
        health, health_status = "Needs Attention", "warning"
    elif total_risks <= 15:
        health, health_status = "At Risk", "warning"
    else:
        health, health_status = "Critical", "critical"

    kpis = [
        {"label": "Overall Health", "value": health, "status": health_status},
        {"label": "Total Risk Issues", "value": str(total_risks), "status": health_status},
        *[
            {"label": k, "value": str(v), "status": "critical" if k == "Overdue" else "warning"}
            for k, v in list(active_risks.items())[:3]
        ]
    ]

    dashboard = {
        "type": "dashboard",
        "title": "Project Risk Dashboard",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": f"Project health is {health} with {total_risks} total risk issues detected across {len(active_risks)} categories.",
        "kpis": kpis,
        "charts": charts,
    }
    return json.dumps(dashboard, ensure_ascii=False)


CHART_TOOLS = [generate_dashboard_json, generate_quick_stat, generate_risk_dashboard]
