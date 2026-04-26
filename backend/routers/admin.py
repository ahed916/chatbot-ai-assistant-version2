from fastapi import APIRouter, Depends, HTTPException
import httpx
from pydantic import BaseModel, EmailStr
from auth import supabase_admin
from dependencies import require_admin, CurrentUser

router = APIRouter(prefix="/admin", tags=["admin"])


class CreateUserRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    # No Redmine fields — PM sets those themselves


@router.get("/users")
async def list_users(_: CurrentUser = Depends(require_admin)):
    # Fetch profiles with a simple join instead of loading ALL auth users
    profiles = (
        supabase_admin.table("profiles")
        .select("id, full_name, role, redmine_user_id, is_redmine_connected, created_at")
        .eq("role", "project_manager")
        .execute()
    )

    # Only fetch auth info for the specific user IDs we need
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
    # Don't call list_users() — it loads everyone
    # Just try to create and catch the duplicate error from Supabase
    try:
        auth_user = supabase_admin.auth.admin.create_user({
            "email": body.email,
            "password": body.password,
            "email_confirm": True,
        })
    except Exception as e:
        err_str = str(e).lower()
        if "already registered" in err_str or "already exists" in err_str or "duplicate" in err_str:
            raise HTTPException(status_code=400, detail="A user with this email already exists.")
        raise HTTPException(status_code=400, detail=f"Failed to create auth user: {str(e)}")

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
        raise HTTPException(status_code=500, detail=f"Failed to create profile: {str(e)}")

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
        raise HTTPException(status_code=500, detail=f"Failed to delete user: {str(e)}")