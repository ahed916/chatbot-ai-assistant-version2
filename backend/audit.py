"""
audit.py — Structured audit logging.

Every agent action, tool call, and Redmine mutation is logged here.
Log format: JSONL (one JSON object per line) → easy to grep, parse, ship to ELK/Datadog.

What is logged:
- Who asked (user context / session)
- What the agent decided
- Which tools were called
- What changed in Redmine
- Latency of each operation
"""
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from config import AUDIT_LOG_FILE

# Ensure log directory exists
Path(AUDIT_LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

_file_handler = logging.FileHandler(AUDIT_LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(message)s"))

_audit_logger = logging.getLogger("redmind.audit")
_audit_logger.setLevel(logging.INFO)
_audit_logger.addHandler(_file_handler)
_audit_logger.propagate = False  # don't double-log to root logger


def log_event(
    event_type: str,
    *,
    agent: str = "supervisor",
    user_input: str = "",
    tool_called: str = "",
    tool_args: dict = None,
    tool_result: str = "",
    redmine_action: str = "",
    latency_ms: float = 0.0,
    success: bool = True,
    error: str = "",
    extra: dict = None,
):
    """Write a structured audit event to the JSONL log file."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "agent": agent,
        "success": success,
        "latency_ms": round(latency_ms, 2),
    }
    if user_input:
        record["user_input"] = user_input[:500]  # truncate for log size
    if tool_called:
        record["tool"] = tool_called
    if tool_args:
        record["tool_args"] = tool_args
    if tool_result:
        record["tool_result"] = str(tool_result)[:300]
    if redmine_action:
        record["redmine_action"] = redmine_action
    if error:
        record["error"] = error
    if extra:
        record.update(extra)

    _audit_logger.info(json.dumps(record, ensure_ascii=False))


class TimedAudit:
    """Context manager that logs start + end of an operation with latency."""

    def __init__(self, event_type: str, **kwargs):
        self.event_type = event_type
        self.kwargs = kwargs
        self._start = 0.0

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        latency_ms = (time.perf_counter() - self._start) * 1000
        success = exc_type is None
        error = str(exc_val) if exc_val else ""
        log_event(
            self.event_type,
            latency_ms=latency_ms,
            success=success,
            error=error,
            **self.kwargs,
        )
        return False  # don't suppress exceptions
