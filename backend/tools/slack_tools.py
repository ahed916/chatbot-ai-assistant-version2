"""
agents/tools/slack_tools.py

Slack notification tools for the risk monitor agent.
Bot token and channel are loaded from config.py / .env — never hardcoded.
"""
import logging
from langchain_core.tools import tool
from config import SLACK_BOT_TOKEN, SLACK_CHANNEL_ID

logger = logging.getLogger(__name__)


@tool
def send_slack_risk_alert(message: str, channel_id: str = "") -> str:
    """
    Send a risk alert message to the project manager via Slack.

    The risk agent calls this when it identifies critical or high-severity risks.
    The message should be concise, specific, and actionable.

    Args:
        message: The formatted alert message to send. Should include:
                 - Which risks were found (specific issue IDs/names)
                 - Severity level
                 - Recommended immediate actions
        channel_id: Slack channel ID (leave empty to use default from config)

    Returns: Confirmation or error message.
    """
    if not SLACK_BOT_TOKEN:
        return "Slack not configured (SLACK_BOT_TOKEN not set in .env) — skipping notification."

    target_channel = channel_id or SLACK_CHANNEL_ID
    if not target_channel:
        return "Slack channel not configured (SLACK_CHANNEL_ID not set in .env) — skipping notification."

    try:
        import httpx
        response = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={
                "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "channel": target_channel,
                "text": message,
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"🚨 *RedMind Risk Alert*\n\n{message}"
                        }
                    }
                ]
            },
            timeout=10,
        )
        data = response.json()
        if data.get("ok"):
            logger.info(f"[SLACK] Alert sent to {target_channel}")
            return f"✅ Slack alert sent successfully to channel {target_channel}."
        else:
            error = data.get("error", "unknown error")
            logger.error(f"[SLACK] Send failed: {error}")
            return f"Slack send failed: {error}"
    except Exception as e:
        logger.error(f"[SLACK] Exception: {e}")
        return f"Slack notification failed: {e}"


# ── Exported list for risk agent ─────────────────────────────────────────────
SLACK_TOOLS = [send_slack_risk_alert]
