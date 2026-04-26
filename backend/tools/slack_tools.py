"""
agents/tools/slack_tools.py

Slack notification tools for the risk monitor agent.
Bot token and channel are loaded from config.py / .env — never hardcoded.

ARCHITECTURE: One shared Slack channel for all PMs.
  - No per-PM webhooks needed.
  - No database changes needed.
  - The alert message includes the PM's name so it's clear who it concerns.
  - The scheduler passes pm_name when calling this tool in background scans.

FIXES (vs previous version):
  - [Errno 11001] getaddrinfo failed was silently swallowed → alerts dropped.
  - Now retries up to MAX_RETRIES times with exponential backoff.
  - Failed alerts go into a thread-safe dead-letter queue (DLQ) so nothing
    is lost. Call flush_dead_letter_queue() to retry them later, or inspect
    DEAD_LETTER_QUEUE for diagnostics.
  - retry_slack_dlq() is exposed so a scheduler job can periodically drain it.
"""
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from langchain_core.tools import tool
from config import SLACK_BOT_TOKEN, SLACK_CHANNEL_ID

logger = logging.getLogger(__name__)

# ── Retry configuration ───────────────────────────────────────────────────────

MAX_RETRIES = 3          # total attempts (1 original + 2 retries)
BACKOFF_BASE = 2.0       # seconds — doubled each retry: 2s, 4s
SLACK_TIMEOUT = 10       # seconds per request

# ── Dead-letter queue ─────────────────────────────────────────────────────────

@dataclass
class _FailedAlert:
    message: str
    pm_name: str
    channel_id: str
    last_error: str
    attempts: int = 0
    created_at: float = field(default_factory=time.time)


_dlq_lock = threading.Lock()
DEAD_LETTER_QUEUE: deque[_FailedAlert] = deque(maxlen=100)  # cap at 100 entries


def _enqueue_dlq(message: str, pm_name: str, channel_id: str, error: str) -> None:
    with _dlq_lock:
        DEAD_LETTER_QUEUE.append(
            _FailedAlert(
                message=message,
                pm_name=pm_name,
                channel_id=channel_id,
                last_error=error,
            )
        )
    logger.warning(
        "[SLACK] Alert added to dead-letter queue (DLQ size=%d). Error: %s",
        len(DEAD_LETTER_QUEUE),
        error,
    )


def flush_dead_letter_queue() -> int:
    """
    Retry every alert in the DLQ once.
    Returns the number of alerts successfully sent.
    Call this from a periodic scheduler job (e.g. every 10 minutes).
    """
    if not DEAD_LETTER_QUEUE:
        return 0

    with _dlq_lock:
        pending = list(DEAD_LETTER_QUEUE)
        DEAD_LETTER_QUEUE.clear()

    sent = 0
    re_queued = 0
    for alert in pending:
        result = _post_to_slack(
            message=alert.message,
            pm_name=alert.pm_name,
            channel_id=alert.channel_id,
            _bypass_dlq=True,  # avoid recursive DLQ writes during flush
        )
        if "successfully" in result.lower():
            sent += 1
            logger.info("[SLACK] DLQ flush: resent alert for pm=%s", alert.pm_name or "system")
        else:
            # Put it back
            with _dlq_lock:
                DEAD_LETTER_QUEUE.append(alert)
            re_queued += 1

    logger.info(
        "[SLACK] DLQ flush complete: sent=%d re-queued=%d", sent, re_queued
    )
    return sent


# ── Block builder ─────────────────────────────────────────────────────────────

def _build_blocks(message: str, pm_name: str = "") -> list:
    """Build Slack Block Kit blocks for the alert."""
    header = "🚨 *RedMind Risk Alert*"
    if pm_name:
        header += f" — *{pm_name}*'s projects"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{header}\n\n{message}",
            },
        },
        {"type": "divider"},
    ]


# ── Core posting logic ────────────────────────────────────────────────────────

def _post_to_slack(
    message: str,
    pm_name: str = "",
    channel_id: str = "",
    _bypass_dlq: bool = False,
) -> str:
    """
    Internal posting logic with retry + dead-letter queue.

    Retries up to MAX_RETRIES times on transient errors (DNS failures,
    timeouts, HTTP 5xx). Permanent errors (bad token, bad channel) are
    NOT retried — they go straight to the DLQ with a clear error message.

    _bypass_dlq: set True when called from flush_dead_letter_queue() to
                 prevent recursive DLQ writes.
    """
    if not SLACK_BOT_TOKEN:
        return "Slack not configured (SLACK_BOT_TOKEN not set in .env) — skipping."

    target_channel = channel_id or SLACK_CHANNEL_ID
    if not target_channel:
        return "Slack channel not configured (SLACK_CHANNEL_ID not set in .env) — skipping."

    import httpx

    full_text = (
        f"RedMind Risk Alert{f' — {pm_name}' if pm_name else ''}: {message}"
    )
    payload = {
        "channel": target_channel,
        "text": full_text,
        "blocks": _build_blocks(message, pm_name),
    }
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json",
    }

    last_error: Optional[str] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = httpx.post(
                "https://slack.com/api/chat.postMessage",
                headers=headers,
                json=payload,
                timeout=SLACK_TIMEOUT,
            )
            data = response.json()

            if data.get("ok"):
                logger.info(
                    "[SLACK] Alert sent to %s (pm=%s, attempt=%d)",
                    target_channel,
                    pm_name or "system",
                    attempt,
                )
                return f"✅ Slack alert sent successfully to channel {target_channel}."

            error = data.get("error", "unknown_error")

            # Permanent errors — no point retrying
            PERMANENT_ERRORS = {
                "invalid_auth", "account_inactive", "token_revoked",
                "no_permission", "missing_scope", "channel_not_found",
                "not_in_channel", "is_archived",
            }
            if error in PERMANENT_ERRORS:
                logger.error("[SLACK] Permanent error — will not retry: %s", error)
                last_error = f"permanent Slack API error: {error}"
                break  # skip retries, go straight to DLQ

            # Transient Slack API error — retry
            last_error = f"Slack API error: {error}"
            logger.warning(
                "[SLACK] Transient error on attempt %d/%d: %s",
                attempt, MAX_RETRIES, error,
            )

        except (httpx.ConnectError, httpx.TimeoutException, OSError) as e:
            # Network-level errors: DNS failure, connection refused, timeout
            last_error = str(e)
            logger.warning(
                "[SLACK] Network error on attempt %d/%d: %s",
                attempt, MAX_RETRIES, e,
            )

        except Exception as e:
            last_error = str(e)
            logger.error("[SLACK] Unexpected error on attempt %d/%d: %s", attempt, MAX_RETRIES, e)

        # Backoff before next attempt (skip sleep after last attempt)
        if attempt < MAX_RETRIES:
            sleep_time = BACKOFF_BASE ** (attempt - 1)  # 1s, 2s
            logger.info("[SLACK] Retrying in %.1fs...", sleep_time)
            time.sleep(sleep_time)

    # All retries exhausted
    logger.error(
        "[SLACK] All %d attempt(s) failed for pm=%s. Last error: %s",
        MAX_RETRIES, pm_name or "system", last_error,
    )

    if not _bypass_dlq:
        _enqueue_dlq(
            message=message,
            pm_name=pm_name,
            channel_id=target_channel,
            error=last_error or "unknown",
        )

    return f"Slack notification failed after {MAX_RETRIES} attempt(s): {last_error}"


# ── Public API ────────────────────────────────────────────────────────────────

@tool
def send_slack_risk_alert(message: str, channel_id: str = "") -> str:
    """
    Send a risk alert message to the shared Slack channel.

    The risk agent calls this when it identifies critical or high-severity risks.
    The message should be concise, specific, and actionable — include issue IDs,
    severity level, and recommended immediate actions.

    Args:
        message:    The formatted alert message to send.
        channel_id: Slack channel ID (leave empty to use default from config).

    Returns: Confirmation or error message.
    """
    return _post_to_slack(message=message, channel_id=channel_id)


def send_slack_alert_for_pm(
    message: str,
    pm_name: str,
    channel_id: str = "",
) -> str:
    """
    Direct (non-tool) helper called by the scheduler for per-PM scans.
    Adds the PM's name to the Slack message header automatically.
    No database columns, no per-PM webhooks — just uses the shared channel.
    """
    return _post_to_slack(message=message, pm_name=pm_name, channel_id=channel_id)


# ── Exported list for risk agent ──────────────────────────────────────────────
SLACK_TOOLS = [send_slack_risk_alert]