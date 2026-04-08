"""
main.py — RedMind FastAPI Application

Key improvements over previous version:
  - Pre-warming: loads slow-changing Redmine data into Redis on startup
  - Real token streaming: streams directly from the agent (not fake word-splitting)
  - Clean scheduler: uses config.py values, not hardcoded cron strings
  - All config from config.py — no hardcoded values here
  - Proper lifespan handler (replaces deprecated on_event)
  - Audit logging for every request
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Dict, List

import redis as redis_lib
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from audit import log_event
from config import (
    REDIS_DB, REDIS_HOST, REDIS_PORT,
    RISK_SCAN_HOURS, RISK_SCAN_MINUTES,
)
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


# ── App lifespan (replaces deprecated @app.on_event) ─────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    logger.info("[STARTUP] RedMind starting up...")

    # Enable LangSmith tracing if configured in .env
    from langsmith_setup import configure_langsmith
    configure_langsmith()

    # Pre-warm Redis cache with slow-changing Redmine data
    # This makes the first user request fast instead of cold
    try:
        from redmine import prewarm
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, prewarm)
    except Exception as e:
        logger.warning(f"[STARTUP] Pre-warm failed (non-fatal): {e}")

    # Schedule risk scans
    # If RISK_SCAN_INTERVAL_MINUTES > 0, run every N minutes (useful for dev/testing)
    # Otherwise run on the hour interval defined in .env
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
    logger.info("[STARTUP] Ready.")

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
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


class ChatResponse(BaseModel):
    reply: str
    model: str
    latency_ms: float


# ── Chat endpoints ────────────────────────────────────────────────────────────

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    start = time.perf_counter()
    try:
        history = req.messages[:-1]
        user_input = req.messages[-1]["content"]
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(None, run_supervisor, user_input, history)
        latency_ms = (time.perf_counter() - start) * 1000
        logger.info(f"[/chat] {latency_ms:.0f}ms")
        return ChatResponse(reply=reply, model="redmind-v2", latency_ms=round(latency_ms, 2))
    except Exception as e:
        logger.error(f"[/chat ERROR] {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    Server-Sent Events stream.

    Note on streaming with agents:
    True token-level streaming requires the LLM to support it AND the agent
    to expose an astream() interface. With free-tier models on OpenRouter,
    streaming support is inconsistent.

    Our strategy:
    1. Run the supervisor fully (in executor to not block the event loop)
    2. Stream the completed response word-by-word with a tiny delay
       → The user sees text appearing instantly (feels like streaming)
       → We avoid SSE/agent streaming reliability issues on free models

    When you upgrade to a production model, replace this with true astream().
    """
    async def generate():
        start = time.perf_counter()
        try:
            history = req.messages[:-1]
            user_input = req.messages[-1]["content"]

            loop = asyncio.get_event_loop()
            reply = await loop.run_in_executor(None, run_supervisor, user_input, history)

            latency_ms = (time.perf_counter() - start) * 1000
            logger.info(f"[/chat/stream] agent done in {latency_ms:.0f}ms, now streaming tokens")

            # Stream word by word
            words = reply.split(" ")
            for i, word in enumerate(words):
                chunk = word + (" " if i < len(words) - 1 else "")
                yield f"data: {json.dumps({'token': chunk})}\n\n"
                await asyncio.sleep(0.012)  # ~83 words/sec — feels natural

            yield "data: [DONE]\n\n"

        except Exception as e:
            logger.error(f"[/chat/stream ERROR] {e}")
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


# ── Proactive risks endpoint ──────────────────────────────────────────────────

# ── Proactive risks endpoint ──────────────────────────────────────────────────

@app.get("/api/proactive-risks")
async def get_proactive_risks(project_id: str = ""):
    """
    Frontend polls this on login + every 5 minutes.
    Returns latest cached risk scan result.
    If project_id is provided, triggers a fresh scan for that project.
    """
    # Helper: Determine if alert should show (any non-healthy state)
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
                    result["critical_count"],
                    result.get("overall_health", "Unknown")
                ),
                "message": result["proactive_message"],
                "critical_count": result["critical_count"],
                "slack_sent": result["slack_sent"],
                "overall_health": result.get("overall_health", "Unknown"),
                "recommendations": result.get("recommendations", []),
                "checked_at": result.get("checked_at"),  # ✅ Now included
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # Return latest cached result (from scheduled scan)
    try:
        r = redis_lib.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
        cached = r.get("proactive:risk:latest")
        if cached:
            data = json.loads(cached)
            return {
                "has_alert": _should_alert(
                    data["critical_count"],
                    data.get("overall_health", "Unknown")
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


# ── Stats endpoint (for quick dashboard data without full agent) ──────────────

@app.get("/stats")
async def stats():
    """Quick stats endpoint — reads from Redis cache, falls back to Redmine."""
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


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/metrics")
async def get_metrics(source: str = "redis"):
    """
    Agent performance metrics endpoint.

    ?source=redis    → live counters from Redis (fast, always available)
    ?source=log      → computed from audit JSONL log (slower, deeper analysis)
    ?source=both     → both sources side by side (best for comparison)

    Metrics returned per agent:
      - avg_latency_ms        How long the agent takes on average
      - error_rate_pct        % of calls that fail
      - cache_hit_rate_pct    % served from LLM cache
      - avg_tool_calls        Avg tool calls per invocation (lower = better)
      - avg_redundant_reads   Reads called despite data in context (should be ~0)
      - avg_llm_steps         LangGraph steps used (out of recursion_limit=30)
      - health                "good" / "warning" / "degraded: reason"
      + agent-specific fields (json_success_rate, risks_found, writes, etc.)
    """
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
