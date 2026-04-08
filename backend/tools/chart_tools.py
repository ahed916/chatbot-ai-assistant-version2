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


CHART_TOOLS = [generate_dashboard_json, generate_quick_stat]
