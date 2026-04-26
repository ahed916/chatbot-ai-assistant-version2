"""
dependencies.py — FastAPI dependency injectors for role-based access.

FIXES:
  - Guard against user.role being None (verify_token may only decode JWT,
    not fetch the profile row — a None role caused silent 403s for all users).
  - Clearer error messages that distinguish "not a PM" from "role not loaded".
"""

from fastapi import Depends, HTTPException
from auth import verify_token, CurrentUser


def require_project_manager(user: CurrentUser = Depends(verify_token)) -> CurrentUser:
    """
    Requires:
      1. Valid JWT (handled by verify_token)
      2. role == "project_manager" in the profiles table

    Does NOT require is_redmine_connected — that's checked separately in
    get_user_redmine_key() so we can return a specific 400 with a helpful message
    rather than a generic 403.
    """
    # FIX: role can be None if verify_token only decoded the JWT without
    # fetching the profile row. Treat that as a server-side config error,
    # not a permission error, so it surfaces clearly in logs.
    if user.role is None:
        raise HTTPException(
            status_code=500,
            detail=(
                "User role could not be determined. "
                "Ensure verify_token fetches the profiles row and sets CurrentUser.role."
            ),
        )

    if user.role != "project_manager":
        raise HTTPException(
            status_code=403,
            detail="Project manager access required.",
        )
    return user


def require_redmine_connected(user: CurrentUser = Depends(require_project_manager)) -> CurrentUser:
    """
    Stricter dependency for endpoints that absolutely need a Redmine key.
    Use this on chat/stream endpoints so the frontend can show the
    'connect your Redmine account' prompt instead of a generic error.
    """
    if not user.is_redmine_connected:
        raise HTTPException(
            status_code=400,
            detail="Redmine account not connected. Please set your API key in Settings.",
        )
    return user


def require_admin(user: CurrentUser = Depends(verify_token)) -> CurrentUser:
    if user.role is None:
        raise HTTPException(
            status_code=500,
            detail="User role could not be determined.",
        )
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="Admin access required.",
        )
    return user