"""
user_context.py — Async-safe Redmine key management.

PROBLEM WITH threading.local():
    FastAPI runs async endpoints on the main event loop, but run_in_executor()
    dispatches work to a thread pool. threading.local() values set in one thread
    are NOT visible in another thread — so the key set in the request handler
    disappears by the time the supervisor/agents run in the executor thread.

SOLUTION — contextvars.ContextVar:
    ContextVar propagates automatically into threads spawned via run_in_executor()
    because asyncio copies the current Context when scheduling executor calls.
    This means the key set in the async request handler IS visible inside the
    thread pool worker — no extra passing required.
"""

import contextvars
from fastapi import HTTPException
from auth import supabase_admin, CurrentUser
from config import REDMINE_API_KEY as _FALLBACK_KEY

# ContextVar — propagates into run_in_executor threads automatically
_redmine_key_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "redmine_api_key", default=None
)
_is_background_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "is_background", default=True
)


def set_background_context():
    """
    Call this in background jobs / scheduler / startup tasks.
    Allows fallback to the global REDMINE_API_KEY from .env.
    """
    _is_background_var.set(True)
    _redmine_key_var.set(None)


def set_redmine_key_for_request(key: str):
    """
    Called once per authenticated request after fetching the user's key.
    Marks this context as a user request — no fallback allowed.
    """
    _redmine_key_var.set(key)
    _is_background_var.set(False)


def clear_redmine_key():
    _redmine_key_var.set(None)


def get_current_redmine_key() -> str:
    """
    Resolves the Redmine API key for the current context:
      - Authenticated request  → user's personal key (required, never falls back)
      - Background job         → REDMINE_API_KEY from .env
    """
    key = _redmine_key_var.get()
    is_background = _is_background_var.get()

    if key:
        return key

    if is_background and _FALLBACK_KEY:
        return _FALLBACK_KEY

    if not is_background:
        raise HTTPException(
            status_code=400,
            detail="Redmine API key not configured. Please set it in your profile.",
        )

    raise RuntimeError("No Redmine API key available. Set REDMINE_API_KEY in .env.")


def get_user_redmine_key(user: CurrentUser) -> str:
    """
    Fetch user's Redmine key from Supabase, store it in the ContextVar,
    and mark this context as a user request (disables fallback).

    Call this once at the top of every authenticated endpoint.
    The value will automatically propagate into run_in_executor() threads.
    """
    result = (
        supabase_admin.table("profiles")
        .select("redmine_api_key, is_redmine_connected")
        .eq("id", user.id)
        .single()
        .execute()
    )

    data = result.data or {}
    key = data.get("redmine_api_key")

    if not key:
        raise HTTPException(
            status_code=400,
            detail="Redmine API key not configured. Please set it in your profile.",
        )

    if not data.get("is_redmine_connected", False):
        raise HTTPException(
            status_code=400,
            detail="Redmine account not connected. Please reconnect in your profile.",
        )

    set_redmine_key_for_request(key)
    return key


def mark_user_disconnected(user_id: str):
    """Call this when Redmine returns 401 — forces user to reconnect."""
    try:
        supabase_admin.table("profiles").update({
            "is_redmine_connected": False,
            "redmine_api_key": None,
        }).eq("id", user_id).execute()
    except Exception:
        pass