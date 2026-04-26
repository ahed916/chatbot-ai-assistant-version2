from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
import httpx
import logging
from auth import supabase_admin
from dependencies import require_project_manager, CurrentUser
from config import REDMINE_URL, REDMINE_API_KEY


router = APIRouter(prefix="/profile", tags=["profile"])
logger = logging.getLogger(__name__)


class SetRedmineKeyRequest(BaseModel):
    redmine_api_key: str


@router.post("/redmine-key")
async def set_redmine_key(
    body: SetRedmineKeyRequest,
    user: CurrentUser = Depends(require_project_manager),
):
    # Step 1 — validate the user's key and get their Redmine user ID
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{REDMINE_URL}/users/current.json",
                headers={"X-Redmine-API-Key": body.redmine_api_key},
                timeout=10,
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Redmine is not responding.")
    except httpx.RequestError:
        raise HTTPException(status_code=502, detail="Could not reach Redmine server.")

    if r.status_code == 401:
        raise HTTPException(status_code=400, detail="Invalid Redmine API key.")
    if r.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Redmine rejected the key (status {r.status_code}).")

    redmine_user = r.json().get("user", {})
    redmine_user_id = redmine_user.get("id")

    if not redmine_user_id:
        raise HTTPException(status_code=400, detail="Could not retrieve Redmine user ID.")

    # Step 2 — use the ADMIN key to fetch this user's memberships
    # Regular users cannot fetch their own memberships via the API — only admins can.
    try:
        async with httpx.AsyncClient() as client:
            r2 = await client.get(
                f"{REDMINE_URL}/users/{redmine_user_id}.json",
                headers={"X-Redmine-API-Key": REDMINE_API_KEY},  # ← admin key
                params={"include": "memberships"},
                timeout=10,
            )
    except Exception:
        raise HTTPException(status_code=502, detail="Could not retrieve Redmine memberships.")

    if r2.status_code != 200:
        raise HTTPException(status_code=400, detail="Could not retrieve Redmine user details.")

    memberships = r2.json().get("user", {}).get("memberships", [])
    PM_ROLES = {"Manager", "Project Manager"}
    is_pm = any(
        any(role.get("name") in PM_ROLES for role in m.get("roles", []))
        for m in memberships
    )

    if not is_pm:
        raise HTTPException(
            status_code=403,
            detail="Your Redmine role is not a Project Manager in any project.",
        )

    # Step 3 — save key and mark connected
    redmine_login = redmine_user.get("login", "")
    supabase_admin.table("profiles").update({
        "redmine_api_key": body.redmine_api_key,
        "redmine_user_id": redmine_user_id,
        "is_redmine_connected": True,
    }).eq("id", user.id).execute()

    return {
        "status": "connected",
        "redmine_user_id": redmine_user_id,
        "redmine_login": redmine_login,
        "display_name": f"{redmine_user.get('firstname', '')} {redmine_user.get('lastname', '')}".strip(),
    }


@router.get("/me")
async def get_my_profile(user: CurrentUser = Depends(require_project_manager)):
    result = supabase_admin.table("profiles") \
        .select("full_name, redmine_user_id, is_redmine_connected") \
        .eq("id", user.id) \
        .single() \
        .execute()
    data = result.data or {}
    return {
        "full_name": data.get("full_name"),
        "redmine_user_id": data.get("redmine_user_id"),
        "is_redmine_connected": data.get("is_redmine_connected", False),
    }


@router.post("/redmine-disconnect")
async def disconnect_redmine(user: CurrentUser = Depends(require_project_manager)):
    """Called when a Redmine API call fails — forces re-authentication."""
    supabase_admin.table("profiles").update({
        "is_redmine_connected": False,
        "redmine_api_key": None,
        "redmine_user_id": None,
    }).eq("id", user.id).execute()
    return {"status": "disconnected"}