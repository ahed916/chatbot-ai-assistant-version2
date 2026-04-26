from fastapi import APIRouter, Depends, HTTPException
from auth import supabase_admin
from dependencies import require_project_manager, CurrentUser

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("")
async def list_conversations(user: CurrentUser = Depends(require_project_manager)):
    result = supabase_admin.table("conversations") \
        .select("id, title, session_id, created_at, updated_at") \
        .eq("user_id", user.id) \
        .order("updated_at", desc=True) \
        .execute()
    return result.data


@router.delete("/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: str,
    user: CurrentUser = Depends(require_project_manager),
):
    # Verify ownership
    conv = supabase_admin.table("conversations") \
        .select("id") \
        .eq("id", conversation_id) \
        .eq("user_id", user.id) \
        .single() \
        .execute()

    if not conv.data:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    # Messages cascade-delete via FK
    supabase_admin.table("conversations") \
        .delete() \
        .eq("id", conversation_id) \
        .execute()


@router.get("/{conversation_id}/messages")
async def get_messages(
    conversation_id: str,
    user: CurrentUser = Depends(require_project_manager),
):
    conv = supabase_admin.table("conversations") \
        .select("id") \
        .eq("id", conversation_id) \
        .eq("user_id", user.id) \
        .single() \
        .execute()

    if not conv.data:
        raise HTTPException(status_code=404, detail="Conversation not found.")

    result = supabase_admin.table("messages") \
        .select("role, content, created_at") \
        .eq("conversation_id", conversation_id) \
        .order("created_at") \
        .execute()
    return result.data