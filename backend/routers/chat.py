from fastapi import APIRouter, Depends
from pydantic import BaseModel
import httpx
from auth import supabase_admin
from redmine import _headers
from dependencies import require_project_manager, CurrentUser
from user_context import get_user_redmine_key

router = APIRouter(prefix="/chat", tags=["chat"])

class MessageRequest(BaseModel):
    conversation_id: str | None = None
    message: str

class MessageResponse(BaseModel):
    conversation_id: str
    reply: str

@router.post("/message", response_model=MessageResponse)
async def send_message(
    body: MessageRequest,
    user: CurrentUser = Depends(require_project_manager),
):
    redmine_key = get_user_redmine_key(user)

    # Get or create conversation
    if body.conversation_id:
        # Verify ownership (RLS handles this, but explicit check is cleaner)
        conv = supabase_admin.table("conversations") \
            .select("id") \
            .eq("id", body.conversation_id) \
            .eq("user_id", user.id) \
            .single() \
            .execute()
        if not conv.data:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail="Conversation not found")
        conversation_id = body.conversation_id
    else:
        new_conv = supabase_admin.table("conversations") \
            .insert({"user_id": user.id, "title": body.message[:60]}) \
            .execute()
        conversation_id = new_conv.data[0]["id"]

    # Save user message
    supabase_admin.table("messages").insert({
        "conversation_id": conversation_id,
        "role": "user",
        "content": body.message,
    }).execute()

    # Call Redmine with user's own key (never shared)
    async with httpx.AsyncClient() as client:
        redmine_response = await client.get(
            "https://your-redmine.com/issues.json",
            headers=_headers(),
            params={"assigned_to_id": "me", "limit": 10},
        )

    # Your existing chatbot logic here
    # Pass redmine_response.json() as context to your LLM
    reply = f"[Your chatbot reply using Redmine data]"

    # Save assistant reply
    supabase_admin.table("messages").insert({
        "conversation_id": conversation_id,
        "role": "assistant",
        "content": reply,
    }).execute()

    return MessageResponse(conversation_id=conversation_id, reply=reply)

@router.get("/conversations")
async def list_conversations(user: CurrentUser = Depends(require_project_manager)):
    result = supabase_admin.table("conversations") \
        .select("id, title, created_at, updated_at") \
        .eq("user_id", user.id) \
        .order("updated_at", desc=True) \
        .execute()
    return result.data

@router.get("/conversations/{conv_id}/messages")
async def get_messages(
    conv_id: str,
    user: CurrentUser = Depends(require_project_manager),
):
    # Verify ownership first
    conv = supabase_admin.table("conversations") \
        .select("id") \
        .eq("id", conv_id) \
        .eq("user_id", user.id) \
        .single() \
        .execute()
    if not conv.data:
        from fastapi import HTTPException
        raise HTTPException(status_code=404)

    result = supabase_admin.table("messages") \
        .select("role, content, created_at") \
        .eq("conversation_id", conv_id) \
        .order("created_at") \
        .execute()
    return result.data