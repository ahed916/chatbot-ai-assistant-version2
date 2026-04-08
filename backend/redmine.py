"""
redmine.py — Redmine API client with:
  - Redis caching (all TTLs from config.py / .env)
  - Tenacity retry with exponential backoff (circuit breaker pattern)
  - Graceful degradation: if Redmine is down, return cached data with a warning
  - Audit logging of every write operation
  - Zero hardcoded values
"""
import httpx
import json
import redis
import time
import logging
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
from config import (
    REDMINE_URL as BASE,
    REDMINE_API_KEY,
    REDIS_HOST, REDIS_PORT, REDIS_DB,
    CACHE_TTL_PROJECTS, CACHE_TTL_TRACKERS, CACHE_TTL_STATUSES,
    CACHE_TTL_MEMBERS, CACHE_TTL_ISSUES, CACHE_TTL_USERS,
    RETRY_MAX_ATTEMPTS, RETRY_WAIT_MIN, RETRY_WAIT_MAX,
)
from audit import log_event

logger = logging.getLogger(__name__)

HEADERS = {
    "X-Redmine-API-Key": REDMINE_API_KEY,
    "Content-Type": "application/json",
}

# ── Redis ─────────────────────────────────────────────────────────────────────

try:
    _redis = redis.Redis(
        host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    _redis.ping()
    logger.info("[REDIS] Connected successfully")
except Exception as e:
    logger.warning(f"[REDIS] Connection failed: {e} — running without cache")
    _redis = None


def _cache_get(key: str):
    if not _redis:
        return None
    try:
        val = _redis.get(key)
        if val:
            logger.debug(f"[CACHE HIT] {key}")
            return json.loads(val)
        logger.debug(f"[CACHE MISS] {key}")
        return None
    except Exception as e:
        logger.warning(f"[CACHE GET ERROR] {key}: {e}")
        return None


def _cache_set(key: str, data, ttl: int):
    if not _redis:
        return
    try:
        _redis.setex(key, ttl, json.dumps(data, default=str))
        logger.debug(f"[CACHE SET] {key} TTL={ttl}s")
    except Exception as e:
        logger.warning(f"[CACHE SET ERROR] {key}: {e}")


def _cache_invalidate(*keys: str):
    """Invalidate specific keys + all issue/resolve wildcard patterns."""
    if not _redis:
        return
    try:
        for key in keys:
            if key:
                _redis.delete(key)
        for pattern in ("issues:*", "resolve:*"):
            for k in _redis.scan_iter(pattern):
                _redis.delete(k)
        logger.debug(f"[CACHE INVALIDATED] {keys}")
    except Exception as e:
        logger.warning(f"[CACHE INVALIDATE ERROR]: {e}")


# ── HTTP with Tenacity retry (circuit breaker pattern) ────────────────────────

def _make_retry_decorator():
    return retry(
        stop=stop_after_attempt(RETRY_MAX_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=RETRY_WAIT_MIN, max=RETRY_WAIT_MAX),
        retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )


@_make_retry_decorator()
def _get(path: str, params: dict = None):
    start = time.perf_counter()
    r = httpx.get(f"{BASE}{path}", headers=HEADERS, params=params or {}, timeout=10)
    r.raise_for_status()
    ms = (time.perf_counter() - start) * 1000
    logger.debug(f"[REDMINE GET] {path} → {ms:.0f}ms")
    return r.json()


@_make_retry_decorator()
def _post(path: str, body: dict):
    start = time.perf_counter()
    r = httpx.post(f"{BASE}{path}", headers=HEADERS, json=body, timeout=10)
    r.raise_for_status()
    ms = (time.perf_counter() - start) * 1000
    logger.debug(f"[REDMINE POST] {path} → {ms:.0f}ms")
    return r.json()


@_make_retry_decorator()
def _put(path: str, body: dict):
    start = time.perf_counter()
    r = httpx.put(f"{BASE}{path}", headers=HEADERS, json=body, timeout=10)
    r.raise_for_status()
    ms = (time.perf_counter() - start) * 1000
    logger.debug(f"[REDMINE PUT] {path} → {ms:.0f}ms")
    return r


@_make_retry_decorator()
def _delete(path: str):
    start = time.perf_counter()
    r = httpx.delete(f"{BASE}{path}", headers=HEADERS, timeout=10)
    r.raise_for_status()
    ms = (time.perf_counter() - start) * 1000
    logger.debug(f"[REDMINE DELETE] {path} → {ms:.0f}ms")


def _safe_get(path: str, params: dict = None, cache_key: str = None):
    """GET with graceful fallback to cache if Redmine is unreachable."""
    try:
        return _get(path, params)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as e:
        logger.error(f"[REDMINE UNREACHABLE] {path}: {e}")
        if cache_key:
            stale = _cache_get(cache_key)
            if stale:
                logger.warning(f"[FALLBACK] Returning stale cache for {cache_key}")
                return {"_stale": True, "_data": stale}
        raise


# ── Normalizer ────────────────────────────────────────────────────────────────

def normalize(s: str) -> str:
    return s.lower().strip().replace("-", " ").replace("_", " ")


# ── READ functions ────────────────────────────────────────────────────────────

def list_projects():
    key = "projects"
    cached = _cache_get(key)
    if cached:
        return cached
    result = _get("/projects.json", {"limit": 100}).get("projects", [])
    _cache_set(key, result, CACHE_TTL_PROJECTS)
    return result


def list_trackers():
    key = "trackers"
    cached = _cache_get(key)
    if cached:
        return cached
    result = _get("/trackers.json").get("trackers", [])
    _cache_set(key, result, CACHE_TTL_TRACKERS)
    return result


def list_issue_statuses():
    key = "statuses"
    cached = _cache_get(key)
    if cached:
        return cached
    result = _get("/issue_statuses.json").get("issue_statuses", [])
    _cache_set(key, result, CACHE_TTL_STATUSES)
    return result


def list_members(project_id: str):
    key = f"members:{project_id}"
    cached = _cache_get(key)
    if cached:
        return cached
    try:
        result = _get(
            f"/projects/{project_id}/memberships.json", {"limit": 100}
        ).get("memberships", [])
        _cache_set(key, result, CACHE_TTL_MEMBERS)
        return result
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return []
        raise


def list_issues(
    project_id=None, status="open", limit=100,
    tracker_id=None, assigned_to_id=None,
):
    key = f"issues:{project_id}:{status}:{limit}:{tracker_id}:{assigned_to_id}"
    cached = _cache_get(key)
    if cached:
        return cached
    params = {"status_id": status, "limit": limit}
    if project_id:
        params["project_id"] = project_id
    if tracker_id:
        params["tracker_id"] = tracker_id
    if assigned_to_id:
        params["assigned_to_id"] = assigned_to_id
    result = _get("/issues.json", params).get("issues", [])
    _cache_set(key, result, CACHE_TTL_ISSUES)
    return result


def get_issue(issue_id: int):
    key = f"issue:{issue_id}"
    cached = _cache_get(key)
    if cached:
        return cached
    try:
        result = _get(f"/issues/{issue_id}.json").get("issue", {})
        if result:
            _cache_set(key, result, CACHE_TTL_ISSUES)
        return result
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {}
        raise


def get_allowed_transitions(issue_id: int) -> dict:
    try:
        data = _get(f"/issues/{issue_id}.json", {"include": "allowed_statuses"})
        issue = data.get("issue", {})
        return {
            "current_status_id": issue.get("status", {}).get("id"),
            "current_status_name": issue.get("status", {}).get("name"),
            "allowed": issue.get("allowed_statuses", []),
        }
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {}
        raise


def list_users():
    """
    Removed — requires admin API key (returns 403).
    Use context_builder._extract_users_from_issues_and_members() instead,
    which gets user data from issue assignees and memberships.
    """
    return []


def resolve_project_id(name_or_id: str) -> str:
    cache_key = f"resolve:{normalize(name_or_id)}"
    cached = _cache_get(cache_key)
    if cached:
        return cached
    projects = list_projects()
    n = normalize(name_or_id)
    for p in projects:
        if (
            str(p["id"]) == name_or_id
            or normalize(p["identifier"]) == n
            or normalize(p["name"]) == n
        ):
            result = str(p["id"])
            _cache_set(cache_key, result, CACHE_TTL_PROJECTS)
            logger.debug(f"[RESOLVE] '{name_or_id}' → ID {result}")
            return result
    logger.debug(f"[RESOLVE] '{name_or_id}' not found, using as-is")
    return name_or_id


# ── WRITE functions (all audit-logged + cache-invalidated) ───────────────────

def create_issue(
    project_id, subject, description="", assigned_to_id=None,
    priority_id=2, tracker_id=1, status_id=1, due_date=None,
):
    body = {
        "issue": {
            "project_id": project_id,
            "subject": subject,
            "description": description,
            "priority_id": priority_id,
            "tracker_id": tracker_id,
            "status_id": status_id,
        }
    }
    if assigned_to_id:
        body["issue"]["assigned_to_id"] = assigned_to_id
    if due_date:
        body["issue"]["due_date"] = due_date
    result = _post("/issues.json", body).get("issue", {})
    _cache_invalidate()
    log_event(
        "redmine_write",
        agent="automation_agent",
        redmine_action="create_issue",
        tool_args={"project_id": project_id, "subject": subject},
        tool_result=str(result.get("id", "")),
    )
    return result


def update_issue(
    issue_id, status_id=None, assigned_to_id=None,
    tracker_id=None, priority_id=None, notes="", due_date=None,
):
    body = {"issue": {"notes": notes}}
    if status_id is not None:
        body["issue"]["status_id"] = status_id
    if assigned_to_id is not None:
        body["issue"]["assigned_to_id"] = assigned_to_id
    if tracker_id is not None:
        body["issue"]["tracker_id"] = tracker_id
    if priority_id is not None:
        body["issue"]["priority_id"] = priority_id
    if due_date is not None:
        body["issue"]["due_date"] = due_date
    try:
        _put(f"/issues/{issue_id}.json", body)
        _cache_invalidate(f"issue:{issue_id}")
        log_event(
            "redmine_write",
            agent="automation_agent",
            redmine_action="update_issue",
            tool_args={"issue_id": issue_id, **body["issue"]},
        )
        return None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return "NOT_FOUND"
        if e.response.status_code == 422:
            errors = e.response.json().get("errors", [])
            return f"WORKFLOW_ERROR: {', '.join(errors)}"
        raise


def delete_issue(issue_id: int):
    try:
        _delete(f"/issues/{issue_id}.json")
        _cache_invalidate(f"issue:{issue_id}")
        log_event(
            "redmine_write",
            agent="automation_agent",
            redmine_action="delete_issue",
            tool_args={"issue_id": issue_id},
        )
        return None
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return "NOT_FOUND"
        raise


# ── Pre-warm cache (called on server startup) ─────────────────────────────────

def prewarm():
    """Pre-load slow-changing data into Redis so first requests are fast.

    Called once at startup from main.py.
    """
    logger.info("[PREWARM] Loading projects, trackers, statuses, users into cache...")
    try:
        list_projects()
        list_trackers()
        list_issue_statuses()
        logger.info("[PREWARM] Done.")
    except Exception as e:
        logger.warning(f"[PREWARM] Failed (non-fatal): {e}")


# In redmine.py — add this function

def resolve_user_id(name: str, project_id: str = None) -> int | None:
    name_lower = name.lower().strip()

    def score_match(user_name: str) -> int:
        """Higher score = better match"""
        user_lower = user_name.lower().strip()
        score = 0
        if user_lower == name_lower:
            score += 100  # exact
        elif name_lower in user_lower:
            score += 50  # substring
        elif user_lower in name_lower:
            score += 30  # reverse substring
        # Bonus for matching first/last name separately
        name_parts = set(name_lower.split())
        user_parts = set(user_lower.split())
        if name_parts & user_parts:
            score += 20 * len(name_parts & user_parts)
        return score

    candidates = []

    # Collect all possible users from memberships
    projects_to_check = [project_id] if project_id else [p["id"] for p in list_projects()]
    for pid in projects_to_check:
        for m in list_members(str(pid)):
            user = m.get("user", {})
            uid = user.get("id")
            uname = user.get("name", "")
            if uid and uname:
                score = score_match(uname)
                if score > 0:
                    candidates.append((score, uid, uname))

    if not candidates:
        return None

    # Return highest-scoring match
    candidates.sort(key=lambda x: -x[0])
    best_score, best_id, best_name = candidates[0]

    # Only auto-select if confidence is high
    if best_score >= 50:
        logger.debug(f"[RESOLVE] '{name}' → ID:{best_id} ({best_name}) [score:{best_score}]")
        return best_id

    # Low confidence → return None so agent can ask
    logger.warning(f"[RESOLVE] Ambiguous match for '{name}': {candidates[:3]}")
    return None
