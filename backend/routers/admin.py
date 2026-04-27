"""
admin.py — Admin router

Endpoints:
  GET  /admin/users                              — list all PMs
  POST /admin/users                              — create PM
  DELETE /admin/users/{user_id}                  — delete PM
  GET  /admin/users/{user_id}/conversations      — list conversations for a PM
  GET  /admin/conversations/{conv_id}/messages   — messages for a conversation
  GET  /admin/redmine-stats                      — global Redmine stats (cached)
  GET  /admin/chat-activity                      — conversation counts last 7 days

Performance notes:
  - /admin/redmine-stats: projects + issues fetched in PARALLEL via asyncio.gather,
    result cached for REDMINE_CACHE_TTL seconds (default 120s) so repeated page
    loads are instant.
  - /admin/chat-activity: single Supabase query, grouped in Python — very fast.
"""

import asyncio
import time
import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr

import httpx

from auth import supabase_admin
from dependencies import require_admin, CurrentUser
from config import REDMINE_URL, REDMINE_API_KEY

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger(__name__)

# ── In-process TTL cache for Redmine stats ────────────────────────────────────
# Avoids hammering Redmine on every page load.
# Two page loads within REDMINE_CACHE_TTL seconds share the same result.

REDMINE_CACHE_TTL = 120  # seconds — tune to taste (120s = 2 min)

_redmine_cache: dict = {
    "data":       None,
    "expires_at": 0.0,
}


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str


# ── User management ───────────────────────────────────────────────────────────

@router.get("/users")
async def list_users(_: CurrentUser = Depends(require_admin)):
    profiles = (
        supabase_admin.table("profiles")
        .select("id, full_name, role, redmine_user_id, is_redmine_connected, created_at")
        .eq("role", "project_manager")
        .order("created_at", desc=True)
        .execute()
    )

    result = []
    for p in profiles.data:
        try:
            auth_user = supabase_admin.auth.admin.get_user_by_id(p["id"])
            email = auth_user.user.email if auth_user.user else ""
        except Exception:
            email = ""
        result.append({**p, "email": email})

    return result


@router.post("/users", status_code=201)
async def create_user(
    body: CreateUserRequest,
    _: CurrentUser = Depends(require_admin),
):
    try:
        auth_user = supabase_admin.auth.admin.create_user({
            "email": body.email,
            "password": body.password,
            "email_confirm": True,
        })
    except Exception as e:
        err_str = str(e).lower()
        if any(k in err_str for k in ("already registered", "already exists", "duplicate")):
            raise HTTPException(status_code=400, detail="A user with this email already exists.")
        raise HTTPException(status_code=400, detail=f"Failed to create auth user: {e}")

    try:
        supabase_admin.table("profiles").insert({
            "id": auth_user.user.id,
            "role": "project_manager",
            "full_name": body.full_name,
            "redmine_api_key": None,
            "redmine_user_id": None,
            "is_redmine_connected": False,
        }).execute()
    except Exception as e:
        supabase_admin.auth.admin.delete_user(auth_user.user.id)
        raise HTTPException(status_code=500, detail=f"Failed to create profile: {e}")

    return {"id": auth_user.user.id, "email": body.email}


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    _: CurrentUser = Depends(require_admin),
):
    profile = (
        supabase_admin.table("profiles")
        .select("role")
        .eq("id", user_id)
        .single()
        .execute()
    )
    if not profile.data:
        raise HTTPException(status_code=404, detail="User not found.")
    if profile.data.get("role") == "admin":
        raise HTTPException(status_code=403, detail="Cannot delete admin accounts.")

    try:
        supabase_admin.auth.admin.delete_user(user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete user: {e}")


# ── Chat log endpoints ────────────────────────────────────────────────────────

@router.get("/users/{user_id}/conversations")
async def list_user_conversations(
    user_id: str,
    _: CurrentUser = Depends(require_admin),
):
    profile = (
        supabase_admin.table("profiles")
        .select("role")
        .eq("id", user_id)
        .single()
        .execute()
    )
    if not profile.data:
        raise HTTPException(status_code=404, detail="User not found.")
    if profile.data.get("role") != "project_manager":
        raise HTTPException(status_code=403, detail="User is not a project manager.")

    result = (
        supabase_admin.table("conversations")
        .select("id, title, session_id, created_at, updated_at")
        .eq("user_id", user_id)
        .order("updated_at", desc=True)
        .execute()
    )
    return result.data or []


@router.get("/conversations/{conversation_id}/messages")
async def get_conversation_messages(
    conversation_id: str,
    _: CurrentUser = Depends(require_admin),
):
    conv = (
        supabase_admin.table("conversations")
        .select("id")
        .eq("id", conversation_id)
        .single()
        .execute()
    )
    if not conv.data:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    messages = (
        supabase_admin.table("messages")
        .select("role, content, created_at")
        .eq("conversation_id", conversation_id)
        .order("created_at")
        .execute()
    )
    return messages.data or []


# ── Chat activity (last 7 days) ───────────────────────────────────────────────

@router.get("/chat-activity")
async def get_chat_activity(_: CurrentUser = Depends(require_admin)):
    """
    Returns conversation counts grouped by day for the last 7 days.
    Single Supabase query — fast.
    """
    today = date.today()
    days  = [today - timedelta(days=i) for i in range(6, -1, -1)]  # oldest → newest

    result = (
        supabase_admin.table("conversations")
        .select("created_at")
        .gte("created_at", days[0].isoformat())
        .execute()
    )

    counts: dict[str, int] = {d.isoformat(): 0 for d in days}
    for row in (result.data or []):
        day_str = row["created_at"][:10]
        if day_str in counts:
            counts[day_str] += 1

    return [
        {
            "date":          d.isoformat(),
            "day":           d.strftime("%a"),
            "conversations": counts[d.isoformat()],
        }
        for d in days
    ]


# ── Redmine overview stats ────────────────────────────────────────────────────

async def _fetch_redmine_stats_uncached() -> dict:
    """
    Fetches projects + issues from Redmine IN PARALLEL using asyncio.gather.
    Previously these were two sequential awaits — this halves the network wait.
    """
    if not REDMINE_API_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "REDMINE_API_KEY is not configured on the server. "
                "Add it to your .env file to enable this dashboard."
            ),
        )

    headers = {"X-Redmine-API-Key": REDMINE_API_KEY}

    async with httpx.AsyncClient(timeout=15) as client:
        # ── Fire both requests at the same time ──
        try:
            projects_res, issues_res = await asyncio.gather(
                client.get(
                    f"{REDMINE_URL}/projects.json",
                    headers=headers,
                    params={"limit": 100},
                ),
                client.get(
                    f"{REDMINE_URL}/issues.json",
                    headers=headers,
                    params={"limit": 100, "status_id": "*"},
                ),
            )
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Redmine is not responding.")
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Could not reach Redmine: {e}")

    if projects_res.status_code == 401 or issues_res.status_code == 401:
        raise HTTPException(status_code=401, detail="Admin Redmine API key is invalid.")

    projects_res.raise_for_status()
    issues_res.raise_for_status()

    today    = date.today().isoformat()
    projects = projects_res.json().get("projects", [])
    issues   = issues_res.json().get("issues", [])

    by_status:  dict[str, int] = {}
    by_tracker: dict[str, int] = {}
    by_project: dict[str, int] = {}
    overdue = 0

    for issue in issues:
        status  = issue.get("status",  {}).get("name", "Unknown")
        tracker = issue.get("tracker", {}).get("name", "Unknown")
        project = issue.get("project", {}).get("name", "Unknown")
        due     = issue.get("due_date")

        by_status[status]   = by_status.get(status, 0) + 1
        by_tracker[tracker] = by_tracker.get(tracker, 0) + 1
        by_project[project] = by_project.get(project, 0) + 1

        if due and due < today:
            overdue += 1

    return {
        "total_issues":   len(issues),
        "total_projects": len(projects),
        "overdue":        overdue,
        "by_status":      by_status,
        "by_tracker":     by_tracker,
        "by_project":     by_project,
    }


@router.get("/redmine-stats")
async def get_redmine_stats(_: CurrentUser = Depends(require_admin)):
    """
    Returns global Redmine statistics.

    Cache behaviour:
      - First call hits Redmine (projects + issues fetched in parallel).
      - Subsequent calls within REDMINE_CACHE_TTL seconds return the cached result instantly.
      - Cache is per-process (in-memory dict) — lightweight, no Redis needed.
    """
    now = time.monotonic()

    # ── Cache hit ──
    if _redmine_cache["data"] is not None and now < _redmine_cache["expires_at"]:
        logger.debug("redmine-stats: cache hit")
        return _redmine_cache["data"]

    # ── Cache miss — fetch and store ──
    logger.debug("redmine-stats: cache miss, fetching from Redmine")
    data = await _fetch_redmine_stats_uncached()

    _redmine_cache["data"]       = data
    _redmine_cache["expires_at"] = now + REDMINE_CACHE_TTL

    return data