"""
config.py — Single source of truth for all environment variables.

Every other module imports from here. This means:
- Zero hardcoding anywhere else in the codebase.
- To change a setting: edit .env, restart. Done.
- To add a new setting: add it here + in .env.example.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Redmine ───────────────────────────────────────────────────────────────────
REDMINE_URL: str = os.environ["REDMINE_URL"].rstrip("/")
REDMINE_API_KEY: str = os.environ["REDMINE_API_KEY"]

# ── LLM ───────────────────────────────────────────────────────────────────────
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY: str = os.environ["LLM_API_KEY"]
LLM_MODEL: str = os.getenv("LLM_MODEL", "nvidia/nemotron-super-49b-v1:free")
LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "4096"))
LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "60"))

# ── Agent Reasoning ───────────────────────────────────────────────────────────
AGENT_MAX_ITERATIONS: int = int(os.getenv("AGENT_MAX_ITERATIONS", "15"))
AGENT_MAX_EXECUTION_TIME: int = int(os.getenv("AGENT_MAX_EXECUTION_TIME", "180"))
# LangGraph recursion_limit must be 2× iterations because each tool call = 2 graph steps
# (one for the LLM deciding to call the tool, one for the tool result).
# "Sorry, need more steps" = this limit was too low.
AGENT_RECURSION_LIMIT: int = AGENT_MAX_ITERATIONS * 2  # = 30
PROMPT_VERSION: str = os.getenv("PROMPT_VERSION", "v1")

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_HOST: str = os.getenv("REDIS_HOST", "redis")
REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB: int = int(os.getenv("REDIS_DB", "0"))

# Cache TTLs (seconds)
CACHE_TTL_PROJECTS: int = int(os.getenv("CACHE_TTL_PROJECTS", "300"))
CACHE_TTL_TRACKERS: int = int(os.getenv("CACHE_TTL_TRACKERS", "600"))
CACHE_TTL_STATUSES: int = int(os.getenv("CACHE_TTL_STATUSES", "600"))
CACHE_TTL_MEMBERS: int = int(os.getenv("CACHE_TTL_MEMBERS", "120"))
CACHE_TTL_ISSUES: int = int(os.getenv("CACHE_TTL_ISSUES", "30"))
CACHE_TTL_USERS: int = int(os.getenv("CACHE_TTL_USERS", "3600"))
CACHE_TTL_LLM_RESPONSE: int = int(os.getenv("CACHE_TTL_LLM_RESPONSE", "1800"))

# ── Risk Monitor ──────────────────────────────────────────────────────────────
RISK_SCAN_HOURS: str = os.getenv("RISK_SCAN_INTERVAL_HOURS", "2")
RISK_SCAN_MINUTES: str = os.getenv("RISK_SCAN_INTERVAL_MINUTES", "0")

# ── Slack ─────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_ID: str = os.getenv("SLACK_CHANNEL_ID", "")

# ── Security / Roles ──────────────────────────────────────────────────────────
AUTOMATION_ALLOWED_ROLES: list[str] = [
    r.strip() for r in os.getenv("AUTOMATION_ALLOWED_ROLES", "project_manager,admin").split(",")
]
READ_ONLY_ROLES: list[str] = [
    r.strip() for r in os.getenv("READ_ONLY_ROLES", "viewer,client,guest").split(",")
]

# ── Tenacity / Circuit Breaker ────────────────────────────────────────────────
RETRY_MAX_ATTEMPTS: int = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_WAIT_MIN: int = int(os.getenv("RETRY_WAIT_MIN", "2"))
RETRY_WAIT_MAX: int = int(os.getenv("RETRY_WAIT_MAX", "10"))

# ── Audit Log ─────────────────────────────────────────────────────────────────
AUDIT_LOG_FILE: str = os.getenv("AUDIT_LOG_FILE", "logs/audit.jsonl")

# ── Prompts directory ─────────────────────────────────────────────────────────
PROMPTS_DIR: Path = Path(__file__).parent / "prompts" / PROMPT_VERSION


def load_prompt(name: str) -> str:
    """Load a prompt from prompts/{PROMPT_VERSION}/{name}.md at runtime.

    Prompts are intentionally NOT hardcoded in Python files.
    Edit the .md file and restart to change agent behavior.
    """
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt file not found: {path}\n"
            f"Create it or set PROMPT_VERSION in .env to a valid version."
        )
    return path.read_text(encoding="utf-8").strip()
