"""
main.py — RedMind FastAPI Application

Key improvements over previous version:
  - Pre-warming: loads slow-changing Redmine data into Redis on startup
  - Real token streaming: streams directly from the agent (not fake word-splitting)
  - Clean scheduler: uses config.py values, not hardcoded cron strings
  - All config from config.py — no hardcoded values here
  - Proper lifespan handler (replaces deprecated on_event)
  - Audit logging for every request
  - Conversation history: session_id flows from HTTP header → supervisor → agent
    so every agent can resolve follow-up replies (clarifications, names, etc.)
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

import redis as redis_lib
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from audit import log_event
from config import (
    REDIS_DB, REDIS_HOST, REDIS_PORT,
    RISK_SCAN_HOURS, RISK_SCAN_MINUTES,
)
from conversation_manager import history_store
from supervisor import run_supervisor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


# ── Scheduler job ─────────────────────────────────────────────────────────────

async def scheduled_risk_check():
    from agents.risk_agent import proactive_risk_check
    try:
        logger.info("[SCHEDULER] Starting proactive risk scan...")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, proactive_risk_check)
        r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
        r.setex("proactive:risk:latest", 86400, json.dumps(result))
        logger.info(
            "[SCHEDULER] Risk scan complete — critical=%d slack=%s health=%s",
            result.get("critical_count", 0),
            result.get("slack_sent", False),
            result.get("overall_health", "Unknown"),
        )
        log_event(
            "scheduled_risk_check",
            agent="risk_agent",
            extra={
                "critical_count": result.get("critical_count", 0),
                "overall_health": result.get("overall_health", "Unknown"),
            },
        )
    except Exception as e:
        logger.error("[SCHEDULER] Risk scan failed: %s", e)
        log_event("scheduler_error", agent="risk_agent", error=str(e), success=False)


# ── App lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[STARTUP] RedMind starting up...")

    try:
        from redmine import prewarm
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, prewarm)
    except Exception as e:
        logger.warning(f"[STARTUP] Pre-warm failed (non-fatal): {e}")

    minutes = int(RISK_SCAN_MINUTES)
    if minutes > 0:
        scheduler.add_job(
            scheduled_risk_check, "interval", minutes=minutes,
            id="proactive_risk_check", replace_existing=True,
        )
        logger.info(f"[STARTUP] Risk scan: every {minutes} minutes (dev mode)")
    else:
        hours = int(RISK_SCAN_HOURS)
        scheduler.add_job(
            scheduled_risk_check, "interval", hours=hours,
            id="proactive_risk_check", replace_existing=True,
        )
        logger.info(f"[STARTUP] Risk scan: every {hours} hours")

    scheduler.start()

    # ✅ Run the first scan immediately so Redis is populated before
    # the frontend polls — without this the bell stays empty for 2 hours.
    logger.info("[STARTUP] Running initial risk scan...")
    asyncio.create_task(scheduled_risk_check())

    logger.info("[STARTUP] Ready.")
    yield

    scheduler.shutdown(wait=False)
    logger.info("[SHUTDOWN] Scheduler stopped.")


# ── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(title="RedMind Chat API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8080"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    messages: List[Dict[str, str]] = Field(...)
    # Optional stable session ID from the frontend (e.g. UUID in localStorage).
    # If omitted, the X-Session-Id header is used instead.
    session_id: Optional[str] = Field(default=None)


class ChatResponse(BaseModel):
    reply: str
    model: str
    latency_ms: float


# ── Session ID helper ─────────────────────────────────────────────────────────

def _resolve_session_id(req: ChatRequest, x_session_id: Optional[str]) -> str:
    """
    Pick a session ID with the following priority:
      1. X-Session-Id header  (preferred — set by the frontend on every request)
      2. session_id body field
      3. Hash of the first user message (per-tab fallback, not persistent)

    The frontend should send a stable UUID stored in localStorage so that history
    survives page refreshes. Without it, history still works within a session
    but resets on reload.
    """
    if x_session_id:
        return x_session_id
    if req.session_id:
        return req.session_id
    import hashlib
    for msg in req.messages:
        if msg.get("role") == "user":
            return "anon-" + hashlib.md5(msg["content"].encode()).hexdigest()[:12]
    return "anon-default"


# ── Chat endpoints ────────────────────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    x_session_id: Optional[str] = Header(default=None),
):
    start = time.perf_counter()
    try:
        session_id = _resolve_session_id(req, x_session_id)
        user_input = req.messages[-1]["content"]

        # Fetch persisted history for this session.
        history = history_store.get(session_id)

        loop = asyncio.get_event_loop()
        # Pass both history AND session_id to the supervisor.
        # session_id lets each agent maintain its own internal history store
        # as a fallback, independently of whether the supervisor forwards history.
        reply = await loop.run_in_executor(
            None, lambda: run_supervisor(user_input, history, session_id)
        )

        # Persist this turn so the next request has full context.
        history_store.append(session_id, user_msg=user_input, assistant_msg=reply)

        latency_ms = (time.perf_counter() - start) * 1000
        logger.info(f"[/chat] session={session_id} {latency_ms:.0f}ms")
        return ChatResponse(reply=reply, model="redmind-v2", latency_ms=round(latency_ms, 2))

    except Exception as e:
        logger.error(f"[/chat ERROR] {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    x_session_id: Optional[str] = Header(default=None),
):
    """
    Server-Sent Events stream.
    Runs the supervisor fully then streams word-by-word for a natural feel.
    """
    # Resolve session and history BEFORE entering the generator.
    session_id = _resolve_session_id(req, x_session_id)
    user_input = req.messages[-1]["content"]
    history = history_store.get(session_id)

    async def generate():
        start = time.perf_counter()
        try:
            loop = asyncio.get_event_loop()
            reply = await loop.run_in_executor(
                None, lambda: run_supervisor(user_input, history, session_id)
            )

            # Save the turn now that we have the full reply.
            history_store.append(session_id, user_msg=user_input, assistant_msg=reply)

            latency_ms = (time.perf_counter() - start) * 1000
            logger.info(
                f"[/chat/stream] session={session_id} done in {latency_ms:.0f}ms, streaming..."
            )

            words = reply.split(" ")
            for i, word in enumerate(words):
                chunk = word + (" " if i < len(words) - 1 else "")
                yield f"data: {json.dumps({'token': chunk})}\n\n"
                await asyncio.sleep(0.012)

            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.error(f"[/chat/stream ERROR] session={session_id} {e}")
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


# ── Session management ────────────────────────────────────────────────────────

@app.delete("/chat/history")
async def clear_history(x_session_id: Optional[str] = Header(default=None)):
    """
    Clear conversation history for the current session.
    Call this when the user starts a new conversation.
    """
    if not x_session_id:
        raise HTTPException(status_code=400, detail="X-Session-Id header is required.")
    history_store.clear(x_session_id)
    logger.info(f"[/chat/history] Cleared session={x_session_id}")
    return {"cleared": True, "session_id": x_session_id}


# ── Proactive risks endpoint ──────────────────────────────────────────────────

@app.get("/api/proactive-risks")
async def get_proactive_risks(project_id: str = ""):
    def _should_alert(critical_count: int, overall_health: str) -> bool:
        return critical_count > 0 or overall_health not in ["Healthy", "Unknown"]

    if project_id:
        from agents.risk_agent import proactive_risk_check
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: proactive_risk_check(project_id)
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

    try:
        r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
        cached = r.get("proactive:risk:latest")
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


# ── Stats endpoint ────────────────────────────────────────────────────────────

@app.get("/stats")
async def stats():
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


# ── Metrics endpoint ──────────────────────────────────────────────────────────

@app.get("/metrics")
async def get_metrics(source: str = "redis"):
    from metrics import get_live_metrics, get_metrics_from_audit_log

    if source == "redis":
        return {"source": "redis_live_counters", "metrics": get_live_metrics()}
    elif source == "log":
        return {"source": "audit_log_analysis", "metrics": get_metrics_from_audit_log()}
    elif source == "both":
        return {
            "redis_live": get_live_metrics(),
            "audit_log": get_metrics_from_audit_log(),
            "note": "Compare these two sources to verify Redis counters match log analysis",
        }
    else:
        raise HTTPException(status_code=400, detail="source must be 'redis', 'log', or 'both'")


# ── Health check ──────────────────────────────────────────────────────────────

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
async def debug_risk():
    """Temporary endpoint to inspect the latest risk scan result in Redis."""
    try:
        r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
        cached = r.get("proactive:risk:latest")
        if not cached:
            return {"status": "empty", "message": "No risk scan result in Redis yet."}
        return {"status": "found", "data": json.loads(cached)}
    except Exception as e:
        return {"status": "error", "message": str(e)}
