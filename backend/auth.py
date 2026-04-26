"""
auth.py — JWT verification + role resolution from Supabase profiles.
"""

import os
import jwt
from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from supabase import create_client, Client
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

SUPABASE_JWT_SECRET = os.environ["SUPABASE_JWT_SECRET"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

supabase_admin: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

bearer_scheme = HTTPBearer(auto_error=False)  # auto_error=False so we can give a cleaner message


@dataclass
class CurrentUser:
    id: str
    role: str
    email: str
    is_redmine_connected: bool = False


def verify_token(
    credentials: HTTPAuthorizationCredentials = Security(bearer_scheme),
) -> CurrentUser:
    # Missing Authorization header entirely
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Step 1 — verify the JWT signature + expiry
    try:
        payload = jwt.decode(
            token,
            SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Token expired — please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload: missing sub")

    # Step 2 — fetch role + redmine status from profiles table
    result = (
        supabase_admin.table("profiles")
        .select("role, is_redmine_connected")
        .eq("id", user_id)
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=403,
            detail="User profile not found — contact your administrator.",
        )

    role = result.data.get("role")
    if not role:
        raise HTTPException(
            status_code=403,
            detail="No role assigned to this account.",
        )

    return CurrentUser(
        id=user_id,
        role=role,
        email=payload.get("email", ""),
        is_redmine_connected=result.data.get("is_redmine_connected", False),
    )