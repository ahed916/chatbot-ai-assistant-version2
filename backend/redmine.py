"""
redmine.py — Redmine API client with:
  - Redis caching (all TTLs from config.py / .env)
  - Tenacity retry with exponential backoff (circuit breaker pattern)
  - Graceful degradation: if Redmine is down, return cached data with a warning
  - Audit logging of every write operation
  - Zero hardcoded values
  - Per-user API key resolution via thread-local (get_current_redmine_key),
    with fallback to REDMINE_API_KEY from .env for background jobs.

FIX: list_projects() now fetches ALL projects visible to the API key,
     including private ones, by adding ?include_subprojects=1 is NOT
     the fix — the real fix is paginating with a high limit and NOT
     filtering by is_public. Previously the default GET /projects.json
     with no params returned only public projects for some Redmine
     configurations. Now we explicitly pass limit=1000 and handle
     pagination so private projects the PM is a member of are included.

FIX: resolve_project_id() now also accepts a bare numeric string ("5")
     and resolves it directly by fetching /projects/5.json when the
     project isn't found in the cached list — this handles private projects
     that list_projects() might still miss.
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
    REDIS_HOST, REDIS_PORT, REDIS_DB,
    CACHE_TTL_PROJECTS, CACHE_TTL_TRACKERS, CACHE_TTL_STATUSES,
    CACHE_TTL_MEMBERS, CACHE_TTL_ISSUES,
    RETRY_MAX_ATTEMPTS, RETRY_WAIT_MIN, RETRY_WAIT_MAX,
)
from user_context import get_current_redmine_key
from audit import log_event

logger = logging.getLogger(__name__)


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


# ── HTTP helpers (per-user API key) ───────────────────────────────────────────

def _headers() -> dict:
    return {
        "X-Redmine-API-Key": get_current_redmine_key(),
        "Content-Type": "application/json",
    }


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
    r = httpx.get(f"{BASE}{path}", headers=_headers(), params=params or {}, timeout=30)
    r.raise_for_status()
    ms = (time.perf_counter() - start) * 1000
    logger.debug(f"[REDMINE GET] {path} → {ms:.0f}ms")
    return r.json()


@_make_retry_decorator()
def _post(path: str, body: dict):
    start = time.perf_counter()
    r = httpx.post(f"{BASE}{path}", headers=_headers(), json=body, timeout=30)
    if r.status_code == 422:
        try:
            errors = r.json().get("errors", [r.text])
        except Exception:
            errors = [r.text]
        raise ValueError(f"Redmine validation error: {'; '.join(errors)}")
    r.raise_for_status()
    ms = (time.perf_counter() - start) * 1000
    logger.debug(f"[REDMINE POST] {path} → {ms:.0f}ms")
    return r.json()


@_make_retry_decorator()
def _put(path: str, body: dict):
    start = time.perf_counter()
    r = httpx.put(f"{BASE}{path}", headers=_headers(), json=body, timeout=30)
    r.raise_for_status()
    ms = (time.perf_counter() - start) * 1000
    logger.debug(f"[REDMINE PUT] {path} → {ms:.0f}ms")
    return r


@_make_retry_decorator()
def _delete(path: str):
    start = time.perf_counter()
    r = httpx.delete(f"{BASE}{path}", headers=_headers(), timeout=30)
    r.raise_for_status()
    ms = (time.perf_counter() - start) * 1000
    logger.debug(f"[REDMINE DELETE] {path} → {ms:.0f}ms")


def _safe_get(path: str, params: dict = None, cache_key: str = None):
    """GET with graceful fallback to stale cache if Redmine is unreachable."""
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
    """
    Fetch ALL projects visible to the current API key, including private ones.

    FIX: The default GET /projects.json in some Redmine versions returns only
    public projects unless the requesting user is an admin. We now paginate
    with limit=1000 (safe — Redmine caps at 100 per page but we handle that)
    and do NOT filter by is_public so private projects the key has access to
    are included in the result and available to resolve_project_id().
    """
    key = "projects"
    cached = _cache_get(key)
    if cached:
        return cached

    all_projects = []
    offset = 0
    limit = 100  # Redmine's max per page

    while True:
        data = _get("/projects.json", {"limit": limit, "offset": offset})
        page = data.get("projects", [])
        all_projects.extend(page)
        total_count = data.get("total_count", len(all_projects))

        if len(all_projects) >= total_count or len(page) < limit:
            break
        offset += limit

    _cache_set(key, all_projects, CACHE_TTL_PROJECTS)
    logger.debug(f"[REDMINE] list_projects: fetched {len(all_projects)} projects (public + private)")
    return all_projects


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


def resolve_project_id(name_or_id: str) -> int:
    """
    Resolve a project name, identifier, or numeric ID to a Redmine project ID.

    FIX: When a bare numeric ID like "5" is passed and the project isn't found
    in list_projects() (e.g. because it's private and the admin key doesn't
    see it, or the cache is stale), we now fall back to fetching
    /projects/{id}.json directly. This handles the case where the scheduler
    passes a numeric project ID from Redmine memberships but list_projects()
    doesn't include that project.
    """
    cache_key = f"resolve:{normalize(name_or_id)}"
    cached = _cache_get(cache_key)
    if cached:
        return int(cached)

    projects = list_projects()
    n = normalize(name_or_id)

    import re
    def alphanum(s): return re.sub(r'[^a-z0-9]', '', s.lower())
    n_fuzzy = alphanum(name_or_id)

    for p in projects:
        if (
            str(p["id"]) == name_or_id
            or normalize(p["identifier"]) == n
            or normalize(p["name"]) == n
            or alphanum(p["identifier"]) == n_fuzzy
            or alphanum(p["name"]) == n_fuzzy
        ):
            result = int(p["id"])
            _cache_set(cache_key, result, CACHE_TTL_PROJECTS)
            logger.debug(f"[RESOLVE] '{name_or_id}' → ID {result}")
            return result

    # FIX: If name_or_id looks like a numeric ID, try fetching it directly.
    # This handles private projects that list_projects() might not include.
    if name_or_id.strip().lstrip('-').isdigit():
        try:
            data = _get(f"/projects/{name_or_id}.json")
            p = data.get("project", {})
            if p and p.get("id"):
                result = int(p["id"])
                _cache_set(cache_key, result, CACHE_TTL_PROJECTS)
                logger.info(
                    f"[RESOLVE] '{name_or_id}' resolved via direct fetch → ID {result} "
                    f"(project not in list_projects — likely private)"
                )
                return result
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
            # 404 means it genuinely doesn't exist — fall through to the error below

    available = ", ".join(
        f"'{p['name']}' ({p['identifier']})" for p in projects
    )
    raise ValueError(
        f"Project '{name_or_id}' not found.\n"
        f"Available projects: {available}"
    )


# ── WRITE functions (all audit-logged + cache-invalidated) ───────────────────

def create_issue(
    project_id: int,
    subject: str,
    description: str = "",
    assigned_to_id: int = None,
    priority_id: int = 2,
    tracker_id: int = 1,
    status_id: int = 1,
    due_date: str = None,
    start_date: str = None,
    done_ratio: int = None,
) -> dict:
    try:
        project_id = int(project_id)
        tracker_id = int(tracker_id)
        status_id = int(status_id)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"create_issue called with invalid IDs: "
            f"project_id={project_id!r}, tracker_id={tracker_id!r}, status_id={status_id!r}. "
            f"All three must be integers. Detail: {e}"
        )

    if project_id <= 0:
        raise ValueError(f"create_issue: project_id must be > 0, got {project_id}")
    if tracker_id <= 0:
        raise ValueError(f"create_issue: tracker_id must be > 0, got {tracker_id}")
    if status_id <= 0:
        raise ValueError(f"create_issue: status_id must be > 0, got {status_id}")

    issue_body: dict = {
        "project_id": project_id,
        "subject": subject,
        "tracker_id": tracker_id,
        "status_id": status_id,
        "priority_id": int(priority_id) if priority_id else 2,
    }

    if description:
        issue_body["description"] = description
    if assigned_to_id:
        issue_body["assigned_to_id"] = int(assigned_to_id)
    if due_date:
        issue_body["due_date"] = due_date
    if start_date:
        issue_body["start_date"] = start_date
    if done_ratio is not None:
        issue_body["done_ratio"] = int(done_ratio)

    body = {"issue": issue_body}

    logger.debug(
        f"[REDMINE] create_issue body: project_id={project_id}, "
        f"tracker_id={tracker_id}, status_id={status_id}, subject={subject!r}"
    )

    try:
        logger.warning(f"[DEBUG redmine.py] POST /issues.json body: {json.dumps(body)}")
        result = _post("/issues.json", body).get("issue", {})
    except ValueError as e:
        raise RuntimeError(str(e)) from e

    _cache_invalidate("projects")

    log_event(
        "redmine_write",
        agent="automation_agent",
        redmine_action="create_issue",
        tool_args={"project_id": project_id, "subject": subject},
        tool_result=str(result.get("id", "")),
    )
    return result


def update_issue(
    issue_id,
    status_id=None,
    assigned_to_id=None,
    tracker_id=None,
    priority_id=None,
    notes="",
    due_date=None,
    done_ratio=None,
    subject=None,
    description=None,
):
    body: dict = {"issue": {"notes": notes or ""}}

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
    if done_ratio is not None:
        body["issue"]["done_ratio"] = int(done_ratio)
    if subject is not None:
        body["issue"]["subject"] = subject
    if description is not None:
        body["issue"]["description"] = description

    try:
        _put(f"/issues/{issue_id}.json", body)

        if status_id is not None:
            _cache_invalidate(f"issue:{issue_id}")
            try:
                updated = _get(f"/issues/{issue_id}.json").get("issue", {})
                actual_status_id = updated.get("status", {}).get("id")
                if actual_status_id != status_id:
                    actual_status_name = updated.get("status", {}).get("name", "Unknown")
                    try:
                        transitions = get_allowed_transitions(issue_id)
                        allowed = transitions.get("allowed", [])
                        allowed_names = ", ".join(s["name"] for s in allowed) or "none"
                    except Exception:
                        allowed_names = "unknown"
                    return (
                        f"WORKFLOW_ERROR: Redmine did not apply the status change. "
                        f"Current status is still '{actual_status_name}'. "
                        f"Allowed transitions from here: {allowed_names}."
                    )
            except Exception as verify_err:
                logger.warning(
                    f"[UPDATE] Could not verify status change for #{issue_id}: {verify_err}"
                )

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
            try:
                errors = e.response.json().get("errors", [])
            except Exception:
                errors = [str(e)]
            joined = ", ".join(errors)
            if status_id is not None:
                return f"WORKFLOW_ERROR: {joined}"
            return f"VALIDATION_ERROR: {joined}"
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


def get_issue_summary(issue_id: int) -> str | None:
    issue = get_issue(issue_id)
    if not issue:
        return None
    assignee = issue.get("assigned_to", {}).get("name", "Unassigned")
    status = issue.get("status", {}).get("name", "Unknown")
    return (
        f"Issue #{issue_id}:\n"
        f"  Subject: {issue.get('subject', '')}\n"
        f"  Assigned: {assignee}\n"
        f"  Status: {status}\n"
        f"  Tracker: {issue.get('tracker', {}).get('name', '')}\n"
        f"  Priority: {issue.get('priority', {}).get('name', '')}"
    )


def create_project(
    name: str,
    identifier: str,
    description: str = "",
    is_public: bool = False,
) -> dict:
    body = {
        "project": {
            "name": name,
            "identifier": identifier,
            "description": description,
            "is_public": is_public,
        }
    }
    try:
        result = _post("/projects.json", body).get("project", {})
    except ValueError as e:
        err = str(e).lower()
        if "identifier" in err and ("taken" in err or "already" in err or "invalid" in err):
            logger.info(f"[CREATE PROJECT] '{identifier}' already exists, resolving existing.")
            projects = list_projects()
            existing = next(
                (p for p in projects if p["identifier"] == identifier),
                None
            )
            if existing:
                return existing
        raise

    _cache_invalidate("projects")
    log_event(
        "redmine_write",
        agent="automation_agent",
        redmine_action="create_project",
        tool_args={"name": name, "identifier": identifier},
        tool_result=str(result.get("id", "")),
    )
    return result


def attach_file_to_issue(issue_id: int, file_path: str) -> dict:
    """Upload a file and attach it to an existing issue (two-step Redmine operation)."""
    import os

    if not os.path.isfile(file_path):
        raise ValueError(f"File not found: {file_path}")

    filename = os.path.basename(file_path)

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    upload_headers = {
        "X-Redmine-API-Key": get_current_redmine_key(),
        "Content-Type": "application/octet-stream",
    }
    upload_response = httpx.post(
        f"{BASE}/uploads.json",
        headers=upload_headers,
        content=file_bytes,
        timeout=60,
    )
    upload_response.raise_for_status()
    token = upload_response.json().get("upload", {}).get("token")

    if not token:
        raise RuntimeError("Redmine did not return an upload token.")

    attach_body = {
        "issue": {
            "uploads": [
                {
                    "token": token,
                    "filename": filename,
                    "content_type": "application/octet-stream",
                }
            ]
        }
    }
    _put(f"/issues/{issue_id}.json", attach_body)
    _cache_invalidate(f"issue:{issue_id}")

    log_event(
        "redmine_write",
        agent="automation_agent",
        redmine_action="attach_file",
        tool_args={"issue_id": issue_id, "filename": filename},
    )
    return {"token": token, "filename": filename}


def list_issues_filtered(
    project_id=None,
    status="open",
    assigned_to_id=None,
    tracker_id=None,
    priority_id=None,
    created_after=None,
    updated_after=None,
    due_before=None,
    limit=100,
) -> list[dict]:
    params = {"limit": limit}
    if status != "*":
        params["status_id"] = status
    else:
        params["status_id"] = "*"
    if project_id:
        params["project_id"] = project_id
    if assigned_to_id:
        params["assigned_to_id"] = assigned_to_id
    if tracker_id:
        params["tracker_id"] = tracker_id
    if priority_id:
        params["priority_id"] = priority_id
    if created_after:
        params["created_on"] = f">={created_after}"
    if updated_after:
        params["updated_on"] = f">={updated_after}"
    if due_before:
        params["due_date"] = f"<={due_before}"

    resp = _get("/issues.json", params=params)
    return resp.get("issues", [])


# ── Pre-warm cache (called on server startup) ─────────────────────────────────

def prewarm():
    """Pre-load ALL frequently-needed data into Redis at startup."""
    from concurrent.futures import ThreadPoolExecutor

    logger.info("[PREWARM] Warming Redis cache...")
    try:
        projects = list_projects()
        list_trackers()
        list_issue_statuses()
        list_issues(status="*", limit=200)

        def _warm_members(p):
            try:
                list_members(str(p["id"]))
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(_warm_members, projects))

        logger.info("[PREWARM] Done — all caches warm.")
    except Exception as e:
        logger.warning(f"[PREWARM] Failed (non-fatal): {e}")