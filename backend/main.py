"""
main.py — RedMind FastAPI Application

Key fixes vs previous version:
  1. ContextVar instead of threading.local() → key survives run_in_executor()
  2. Per-PM risk scanning — each PM sees risks for their own projects only
  3. /api/proactive-risks reads from per-PM Redis cache (proactive:risk:{user_id})
  4. Slack alerting is per-PM (each PM's webhook or a shared channel with @mention)
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
from dotenv import load_dotenv

from redmine import prewarm
load_dotenv()

import redis as redis_lib
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from audit import log_event
from auth import supabase_admin
from routers import chat, admin, profile, conversations
from config import (
    REDIS_DB, REDIS_HOST, REDIS_PORT,
    RISK_SCAN_HOURS, RISK_SCAN_MINUTES,
)
from conversation_manager import history_store
from dependencies import require_project_manager, CurrentUser
from scheduler import scheduled_risk_check_for_all_pms
from supervisor import run_supervisor
from user_context import get_user_redmine_key, set_background_context
import mlflow_config


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    set_background_context()
    logger.info("[STARTUP] RedMind starting up...")

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, prewarm)
    except Exception as e:
        logger.warning(f"[STARTUP] Pre-warm failed (non-fatal): {e}")

    minutes = int(RISK_SCAN_MINUTES)
    if minutes > 0:
        scheduler.add_job(
            scheduled_risk_check_for_all_pms,
            "interval",
            minutes=minutes,
            id="proactive_risk_check",
            replace_existing=True,
        )
        logger.info(f"[STARTUP] Risk scan: every {minutes} minutes (dev mode)")
    else:
        hours = int(RISK_SCAN_HOURS)
        scheduler.add_job(
            scheduled_risk_check_for_all_pms,
            "interval",
            hours=hours,
            id="proactive_risk_check",
            replace_existing=True,
        )
        logger.info(f"[STARTUP] Risk scan: every {hours} hours")

    scheduler.start()

    logger.info("[STARTUP] Running initial risk scan for all PMs...")
    asyncio.create_task(scheduled_risk_check_for_all_pms())

    logger.info("[STARTUP] Ready.")
    yield

    scheduler.shutdown(wait=False)
    logger.info("[SHUTDOWN] Scheduler stopped.")


# ── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(title="RedMind Chat API", version="2.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(admin.router)
app.include_router(chat.router)
app.include_router(profile.router)
app.include_router(conversations.router)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    messages: List[Dict[str, str]] = Field(...)
    session_id: Optional[str] = Field(default=None)
    conversation_id: Optional[str] = Field(default=None)


class ChatResponse(BaseModel):
    reply: str
    model: str
    latency_ms: float


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_session_id(user: CurrentUser) -> str:
    return user.id


async def _save_message(
    user_id: str,
    conversation_id: str,
    title: str,
    user_input: str,
    reply: str,
):
    def _sync():
        try:
            existing = supabase_admin.table("conversations") \
                .select("id") \
                .eq("user_id", user_id) \
                .eq("session_id", conversation_id) \
                .limit(1) \
                .execute()

            if existing.data:
                db_conv_id = existing.data[0]["id"]
                supabase_admin.table("conversations") \
                    .update({"updated_at": "now()"}) \
                    .eq("id", db_conv_id) \
                    .execute()
            else:
                new_conv = supabase_admin.table("conversations") \
                    .insert({
                        "user_id": user_id,
                        "session_id": conversation_id,
                        "title": title[:60],
                    }) \
                    .execute()
                db_conv_id = new_conv.data[0]["id"]

            supabase_admin.table("messages").insert([
                {"conversation_id": db_conv_id, "role": "user", "content": user_input},
                {"conversation_id": db_conv_id, "role": "assistant", "content": reply},
            ]).execute()

        except Exception as e:
            logger.error(f"[STORAGE] Failed to save message: {e}")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sync)


# ── Chat endpoints ────────────────────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    req: ChatRequest,
    x_session_id: Optional[str] = Header(default=None),
    user: CurrentUser = Depends(require_project_manager),
):
    start = time.perf_counter()
    try:
        # This sets the ContextVar — will propagate into run_in_executor threads
        get_user_redmine_key(user)
        session_id = _resolve_session_id(user)
        user_input = req.messages[-1]["content"]
        history = history_store.get(session_id)

        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(
            None, lambda: run_supervisor(user_input, history, session_id)
        )

        history_store.append(session_id, user_msg=user_input, assistant_msg=reply)
        title = next(
            (m["content"] for m in req.messages if m.get("role") == "user"),
            user_input,
        )
        await _save_message(user.id, session_id, title, user_input, reply)

        latency_ms = (time.perf_counter() - start) * 1000
        logger.info(f"[/chat] user={user.id} {latency_ms:.0f}ms")
        return ChatResponse(reply=reply, model="redmind-v2", latency_ms=round(latency_ms, 2))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[/chat ERROR] {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    x_session_id: Optional[str] = Header(default=None),
    user: CurrentUser = Depends(require_project_manager),
):
    # Set ContextVar BEFORE entering the async generator — it propagates from here
    get_user_redmine_key(user)

    conversation_id = req.conversation_id or _resolve_session_id(user)
    user_input = req.messages[-1]["content"]
    history = history_store.get(conversation_id)
    title = next(
        (m["content"] for m in req.messages if m.get("role") == "user"),
        user_input,
    )

    async def generate():
        start = time.perf_counter()
        try:
            loop = asyncio.get_event_loop()
            reply = await loop.run_in_executor(
                None, lambda: run_supervisor(user_input, history, conversation_id)
            )
            history_store.append(conversation_id, user_msg=user_input, assistant_msg=reply)
            await _save_message(user.id, conversation_id, title, user_input, reply)

            latency_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"[/chat/stream] user={user.id} conv={conversation_id} done in {latency_ms:.0f}ms"
            )

            words = reply.split(" ")
            for i, word in enumerate(words):
                chunk = word + (" " if i < len(words) - 1 else "")
                yield f"data: {json.dumps({'token': chunk})}\n\n"
                await asyncio.sleep(0.012)

            yield "data: [DONE]\n\n"

        except HTTPException as e:
            logger.error(f"[/chat/stream HTTP ERROR] user={user.id} {e.detail}")
            yield f"data: {json.dumps({'error': e.detail, 'status': e.status_code})}\n\n"
        except Exception as e:
            logger.error(f"[/chat/stream ERROR] user={user.id} {e}")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.delete("/chat/history")
async def clear_history(
    x_session_id: Optional[str] = Header(default=None),
    user: CurrentUser = Depends(require_project_manager),
):
    session_id = _resolve_session_id(user)
    history_store.clear(session_id)
    logger.info(f"[/chat/history] Cleared session={session_id}")
    return {"cleared": True, "session_id": session_id}


# ── Proactive risks endpoint — now per-user ───────────────────────────────────

@app.get("/api/proactive-risks")
async def get_proactive_risks(
    project_id: str = "",
    user: CurrentUser = Depends(require_project_manager),
):
    """
    Returns the latest risk scan result for THIS user only.
    Redis key: proactive:risk:{user.id}
    
    If project_id is passed, runs a live scan for that project instead.
    """
    def _should_alert(critical_count: int, overall_health: str) -> bool:
        return critical_count > 0 or overall_health not in ["Healthy", "Unknown"]

    if project_id:
        get_user_redmine_key(user)
        from agents.risk_agent import proactive_risk_check
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: proactive_risk_check(project_id=project_id)
            )
            return {
                "has_alert": _should_alert(
                    result["critical_count"], result.get("overall_health", "Unknown")
                ),
                "message": result["proactive_message"],
                "critical_count": result["critical_count"],
                "slack_sent": result["slack_sent"],
                "overall_health": result.get("overall_health", "Unknown"),
                "recommendations": result.get("recommendations", []),
                "checked_at": result.get("checked_at"),
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Read from per-user Redis cache
    try:
        r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
        cached = r.get(f"proactive:risk:{user.id}")   # ← per-user key
        if cached:
            data = json.loads(cached)
            return {
                "has_alert": _should_alert(
                    data["critical_count"], data.get("overall_health", "Unknown")
                ),
                "message": data["proactive_message"],
                "critical_count": data["critical_count"],
                "slack_sent": data["slack_sent"],
                "overall_health": data.get("overall_health", "Unknown"),
                "recommendations": data.get("recommendations", []),
                "checked_at": data.get("checked_at"),
            }
    except Exception as e:
        logger.warning(f"[REDIS] Proactive risk read failed: {e}")

    return {
        "has_alert": False,
        "message": None,
        "critical_count": 0,
        "slack_sent": False,
        "overall_health": "Unknown",
        "recommendations": [],
        "checked_at": None,
    }


# ── Stats, Metrics, Health, Debug — unchanged ─────────────────────────────────

@app.get("/stats")
async def stats(user: CurrentUser = Depends(require_project_manager)):
    get_user_redmine_key(user)
    import redmine as rm
    from datetime import date
    try:
        all_issues = rm.list_issues(status="*", limit=100)
        projects = rm.list_projects()
        today = date.today().isoformat()

        by_status: Dict[str, int] = {}
        by_tracker: Dict[str, int] = {}
        by_project: Dict[str, int] = {}
        overdue = 0

        for i in all_issues:
            s = i["status"]["name"]
            t = i.get("tracker", {}).get("name", "Unknown")
            p = i.get("project", {}).get("name", "Unknown")
            by_status[s] = by_status.get(s, 0) + 1
            by_tracker[t] = by_tracker.get(t, 0) + 1
            by_project[p] = by_project.get(p, 0) + 1
            if i.get("due_date") and i["due_date"] < today:
                overdue += 1

        return {
            "total_issues": len(all_issues),
            "total_projects": len(projects),
            "overdue": overdue,
            "by_status": by_status,
            "by_tracker": by_tracker,
            "by_project": by_project,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/metrics")
async def get_metrics(
    source: str = "redis",
    user: CurrentUser = Depends(require_project_manager),
):
    from metrics import get_live_metrics, get_metrics_from_audit_log
    if source == "redis":
        return {"source": "redis_live_counters", "metrics": get_live_metrics()}
    elif source == "log":
        return {"source": "audit_log_analysis", "metrics": get_metrics_from_audit_log()}
    elif source == "both":
        return {
            "redis_live": get_live_metrics(),
            "audit_log": get_metrics_from_audit_log(),
        }
    else:
        raise HTTPException(status_code=400, detail="source must be 'redis', 'log', or 'both'")


@app.get("/health")
async def health():
    import redmine as rm
    redis_ok = redmine_ok = False
    try:
        redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB).ping()
        redis_ok = True
    except Exception:
        pass
    try:
        rm.list_projects()
        redmine_ok = True
    except Exception:
        pass
    return {
        "status": "ok",
        "redis": "connected" if redis_ok else "disconnected",
        "redmine": "connected" if redmine_ok else "disconnected",
    }


@app.get("/debug/risk")
async def debug_risk(user: CurrentUser = Depends(require_project_manager)):
    """Inspect the latest risk scan result for the calling user."""
    try:
        r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
        cached = r.get(f"proactive:risk:{user.id}")
        if not cached:
            return {"status": "empty", "message": "No risk scan result yet for your account."}
        return {"status": "found", "data": json.loads(cached)}
    except Exception as e:
        return {"status": "error", "message": str(e)}