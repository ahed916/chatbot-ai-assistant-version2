"""
scheduler.py — Per-PM risk scanning using a single shared Slack channel.

FIX: Invalid project IDs (membership exists in Redmine user record but
project was deleted/archived) previously caused 5 WARNING log lines per
bad project per PM per scan cycle. Now we validate project IDs via a quick
HEAD/GET before scanning and skip any that don't resolve.

FIX: run_tools_for_project() is now called with the numeric project ID
string from Redmine memberships, not the symbolic identifier. This avoids
the "Project '5' not found" error that occurred when resolve_project_id()
only knew about symbolic names and received a raw numeric ID.

HOW SLACK WORKS HERE (no per-PM webhooks, no DB changes):
  - There is ONE Slack channel configured in .env.
  - Each PM's alert message includes their name as the header.
  - Results cached at proactive:risk:{user_id} — per PM, never shared.
"""

import asyncio
import json
import logging
from typing import Optional

import redis as redis_lib

from auth import supabase_admin
from config import REDIS_DB, REDIS_HOST, REDIS_PORT
from user_context import set_background_context

logger = logging.getLogger(__name__)

_REDIS_TTL = 86400  # 24 h


async def scheduled_risk_check_for_all_pms():
    """
    Run one risk scan per active Project Manager, scoped to their own projects.
    Posts one Slack message per PM who has critical issues.
    """
    set_background_context()

    try:
        result = (
            supabase_admin.table("profiles")
            .select("id, redmine_api_key, redmine_user_id, full_name")
            .eq("role", "project_manager")
            .eq("is_redmine_connected", True)
            .execute()
        )
        pms = result.data or []
    except Exception as e:
        logger.error("[SCHEDULER] Could not fetch PM list: %s", e)
        return

    if not pms:
        logger.info("[SCHEDULER] No connected PMs — skipping risk scan.")
        return

    logger.info("[SCHEDULER] Running risk scan for %d PM(s)...", len(pms))

    try:
        r = redis_lib.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True
        )
    except Exception as e:
        logger.error("[SCHEDULER] Redis unavailable: %s", e)
        r = None

    for pm in pms:
        user_id = pm["id"]
        redmine_key = pm.get("redmine_api_key")
        redmine_user_id = pm.get("redmine_user_id")
        pm_name = pm.get("full_name") or f"PM ({user_id[:8]})"

        if not redmine_key:
            logger.warning("[SCHEDULER] PM %s has no Redmine key — skipping.", pm_name)
            continue

        try:
            loop = asyncio.get_event_loop()
            scan_result = await loop.run_in_executor(
                None,
                lambda key=redmine_key, name=pm_name, rm_uid=redmine_user_id: _run_scan_for_pm(
                    redmine_api_key=key,
                    pm_name=name,
                    redmine_user_id=rm_uid,
                ),
            )

            if r:
                r.setex(
                    f"proactive:risk:{user_id}",
                    _REDIS_TTL,
                    json.dumps(scan_result),
                )

            logger.info(
                "[SCHEDULER] PM=%s critical=%d health=%s slack=%s",
                pm_name,
                scan_result.get("critical_count", 0),
                scan_result.get("overall_health", "Unknown"),
                scan_result.get("slack_sent", False),
            )

        except Exception as e:
            logger.error("[SCHEDULER] Scan failed for PM %s: %s", pm_name, e)

    _try_flush_slack_dlq()


def _try_flush_slack_dlq() -> None:
    try:
        from tools.slack_tools import DEAD_LETTER_QUEUE, flush_dead_letter_queue
        if DEAD_LETTER_QUEUE:
            logger.info(
                "[SCHEDULER] Flushing %d failed Slack alert(s) from DLQ...",
                len(DEAD_LETTER_QUEUE),
            )
            sent = flush_dead_letter_queue()
            logger.info("[SCHEDULER] DLQ flush: %d alert(s) recovered.", sent)
    except Exception as e:
        logger.warning("[SCHEDULER] DLQ flush failed: %s", e)


def _fetch_pm_project_ids(redmine_api_key: str, redmine_user_id: Optional[int]) -> list[str]:
    """
    Return validated Redmine project IDs this PM belongs to.
    Uses numeric IDs (not symbolic names) to avoid resolve_project_id() failures.

    FIX: Previously returned raw IDs from memberships without checking if the
    project still exists. Now validates each ID with a quick GET and skips
    deleted/archived projects silently.
    """
    if not redmine_user_id:
        return []

    try:
        import httpx
        from config import REDMINE_URL

        resp = httpx.get(
            f"{REDMINE_URL}/users/{redmine_user_id}.json",
            headers={"X-Redmine-API-Key": redmine_api_key},
            params={"include": "memberships"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(
                "[SCHEDULER] Could not fetch memberships for user %s: HTTP %d",
                redmine_user_id, resp.status_code,
            )
            return []

        memberships = resp.json().get("user", {}).get("memberships", [])
        raw_ids = [
            str(m["project"]["id"])
            for m in memberships
            if m.get("project", {}).get("id")
        ]

        if not raw_ids:
            return []

        # FIX: validate each project ID with a lightweight GET before scanning.
        # This eliminates the 5×WARNING flood for projects that no longer exist.
        valid_ids = _validate_project_ids(raw_ids, redmine_api_key)

        logger.info(
            "[SCHEDULER] PM user_id=%s: %d/%d project(s) valid: %s",
            redmine_user_id, len(valid_ids), len(raw_ids), valid_ids,
        )
        return valid_ids

    except Exception as e:
        logger.warning("[SCHEDULER] _fetch_pm_project_ids failed: %s", e)
        return []


def _validate_project_ids(project_ids: list[str], redmine_api_key: str) -> list[str]:
    """
    Filter out project IDs that no longer exist in Redmine.
    One GET per project — fast because Redmine project endpoints are lightweight.
    Invalid IDs are skipped with a single INFO log (not 5 WARNINGs each).
    """
    import httpx
    from config import REDMINE_URL

    valid = []
    for pid in project_ids:
        try:
            resp = httpx.get(
                f"{REDMINE_URL}/projects/{pid}.json",
                headers={"X-Redmine-API-Key": redmine_api_key},
                timeout=5,
            )
            if resp.status_code == 200:
                valid.append(pid)
            else:
                logger.info(
                    "[SCHEDULER] Project %r returned HTTP %d — skipping.",
                    pid, resp.status_code,
                )
        except Exception as e:
            logger.info("[SCHEDULER] Could not validate project %r: %s — skipping.", pid, e)

    return valid


def _merge_scan_results(results: list[dict]) -> dict:
    """
    Merge multiple per-project scan results into a single summary.
    Takes the highest critical_count and worst overall_health.
    """
    if not results:
        return {
            "critical_count": 0,
            "overall_health": "Unknown",
            "proactive_message": "No scan results available.",
            "recommendations": [],
            "slack_sent": False,
            "risks": [],
        }

    if len(results) == 1:
        return results[0]

    HEALTH_RANK = {"Healthy": 0, "Needs Attention": 1, "At Risk": 2, "Critical": 3, "Unknown": -1}

    total_critical = sum(r.get("critical_count", 0) for r in results)
    worst_result = max(
        results,
        key=lambda r: HEALTH_RANK.get(r.get("overall_health", "Unknown"), -1),
    )
    worst_health = worst_result.get("overall_health", "Unknown")

    seen = set()
    merged_recs = []
    for r in results:
        for rec in r.get("recommendations", []):
            if rec not in seen:
                seen.add(rec)
                merged_recs.append(rec)
                if len(merged_recs) >= 5:
                    break
        if len(merged_recs) >= 5:
            break

    slack_sent = any(r.get("slack_sent", False) for r in results)
    checked_at = worst_result.get("checked_at", "risk_merged_000000")

    return {
        "critical_count": total_critical,
        "overall_health": worst_health,
        "proactive_message": worst_result.get("proactive_message", ""),
        "recommendations": merged_recs,
        "slack_sent": slack_sent,
        "risks": [],
        "checked_at": checked_at,
    }


def _run_scan_for_pm(
    redmine_api_key: str,
    pm_name: str,
    redmine_user_id: Optional[int] = None,
) -> dict:
    """
    Collect tool output across all of this PM's valid projects, then make
    exactly ONE LLM summarization call for the whole PM.

    FIX: Previously each project triggered proactive_risk_check() which
    contained its own LLM call, meaning N projects = N LLM calls per PM.
    Now all tool output is concatenated first, then summarized in one call.
    """
    import contextvars
    from user_context import _redmine_key_var, _is_background_var
    from agents.risk_agent import run_tools_for_project, summarize_risk_results

    project_ids = _fetch_pm_project_ids(redmine_api_key, redmine_user_id)
    if not project_ids:
        # Fallback: scan everything visible to this key (scoped by Redmine permissions)
        project_ids = [""]

    all_tool_output_parts = []

    for project_id in project_ids:
        ctx = contextvars.copy_context()

        def _collect(pid=project_id):
            _redmine_key_var.set(redmine_api_key)
            _is_background_var.set(False)
            try:
                return run_tools_for_project(project_id=pid)
            except Exception as e:
                logger.error(
                    "[SCHEDULER] Tool scan failed for PM=%s project=%r: %s",
                    pm_name, pid, e,
                )
                return f"⚠️ Scan failed for project {pid!r}: {e}"

        part = ctx.run(_collect)
        all_tool_output_parts.append(part)

    combined_text = "\n\n".join(all_tool_output_parts)

    # ONE LLM call for the entire PM across all projects
    ctx = contextvars.copy_context()

    def _summarize():
        _redmine_key_var.set(redmine_api_key)
        _is_background_var.set(False)
        return summarize_risk_results(
            combined_text=combined_text,
            pm_name=pm_name,
        )

    return ctx.run(_summarize)