"""
agents/action_executor.py

Validates and executes ActionPlan objects against the Redmine API.
The LLM never reaches this layer — it only outputs JSON plans.

Key guarantees:
  - All required fields are checked before any API call
  - Redmine IDs are resolved in Python (users, statuses, trackers, projects)
  - Status workflow transitions are validated before update calls
  - Failures are deterministic, human-readable, and stop the plan early

Fix: create_project → create_issue chaining
  When a plan creates a project then immediately creates an issue in it,
  the cache won't have the new project yet. The executor now tracks
  newly-created projects within the plan and passes the real Redmine ID
  directly to subsequent create_issue actions — no cache lookup needed.

Fix: tracker inference from subject keywords
  Words like "task", "bug", "feature", "improvement" in the subject or
  explicit tracker field are mapped to Redmine tracker names automatically.

Fix: default status/priority when not specified
  If the LLM omits status, it defaults to "New" (status_id=1).
  If the LLM omits priority, it defaults to "Normal" (priority_id=2).

Fix: create_issue payload — project_id, tracker_id, status_id never None
  _exec_create_issue now validates that all three IDs resolved to a real
  integer before calling rm.create_issue, and raises a clear ValidationError
  instead of sending a blank/None value that Redmine silently rejects.

Fix: tracker name normalisation
  Redmine tracker names vary by instance ("Task" vs "Tasks" etc.).
  _resolve_tracker now does case-insensitive prefix + substring matching
  so "Task" matches "Tasks", "Bug" matches "Bugs", etc.
"""
from __future__ import annotations
import logging
from typing import Any

import redmine as rm
from action_schema import Action, ActionPlan
from datetime import date, timedelta
import re as _re

logger = logging.getLogger(__name__)


# ── Required field registry ───────────────────────────────────────────────────

REQUIRED_FIELDS: dict[str, list[tuple[str, str]]] = {
    "create_issue": [
        ("project", "the project name or identifier (e.g. 'website', 'alpha')"),
        ("subject", "the issue title (e.g. 'Login page crash')"),
    ],
    "create_project": [
        ("name", "the full project name (e.g. 'Website Redesign')"),
        ("identifier", "a short lowercase slug, no spaces (e.g. 'website-redesign')"),
    ],
    "update_issue": [
        ("issue_id", "the issue number to update (e.g. 42)"),
    ],
    "bulk_update_issues": [
        ("issue_ids", "a list of issue numbers to update (e.g. [12, 13, 14])"),
    ],
    "delete_issue": [
        ("issue_id", "the issue number to delete (e.g. 99)"),
    ],
    "add_file_to_issue": [
        ("issue_id", "the issue number"),
        ("file_path", "the path to the file to attach"),
    ],
}


class ValidationError(Exception):
    """Raised before any API call when input cannot be safely resolved."""


# ── Tracker keyword inference ─────────────────────────────────────────────────

_TRACKER_KEYWORDS: list[tuple[list[str], str]] = [
    (["bug", "defect", "error", "crash", "fix", "broken"], "Bug"),
    (["feature", "enhancement", "request", "new feature"], "Feature"),
    (["improvement", "improve", "optimise", "optimize", "refactor", "upgrade"], "Feature"),
    (["support", "help", "question", "how to"], "Support"),
    (["task", "todo", "chore", "work", "add", "create", "implement",
     "setup", "set up", "configure", "write", "update", "migrate",
      "functionality", "integration", "page", "screen", "module",
      "component", "service", "endpoint", "api"], "Task"),
]


def _infer_tracker(params: dict) -> str:
    """
    Infer the Redmine tracker name from explicit tracker field or subject keywords.

    Priority:
      1. Explicit tracker field (if it's a known canonical name)
      2. Keyword match in tracker field
      3. Keyword match in subject
      4. Keyword match in description
      5. Default → "Task"
    """
    raw_tracker = (params.get("tracker") or "").strip()
    if raw_tracker:
        # Exact canonical match (case-insensitive)
        canonical_map = {
            "bug": "Bug",
            "feature": "Feature",
            "task": "Task",
            "support": "Support",
        }
        if raw_tracker.lower() in canonical_map:
            return canonical_map[raw_tracker.lower()]
        # Keyword match in the tracker field itself
        for keywords, tracker_name in _TRACKER_KEYWORDS:
            if any(kw in raw_tracker.lower() for kw in keywords):
                logger.info(f"[TRACKER INFER] tracker field '{raw_tracker}' → '{tracker_name}'")
                return tracker_name

    # Combine subject + description for keyword scanning
    subject = (params.get("subject") or "").lower()
    description = (params.get("description") or "").lower()
    combined = f"{subject} {description}"

    for keywords, tracker_name in _TRACKER_KEYWORDS:
        if any(kw in combined for kw in keywords):
            logger.info(f"[TRACKER INFER] text '{combined[:60]}' → '{tracker_name}'")
            return tracker_name

    return "Task"  # Safe default — most user requests are tasks


# ── ID resolvers ───────────────────────────────────────────────────────────────

def _resolve_user(name: str) -> int:
    name_lower = name.lower().strip()
    candidates: list[tuple[str, int]] = []

    try:
        for project in rm.list_projects():
            for member in rm.list_members(str(project["id"])):
                user = member.get("user")
                if not user or "id" not in user:
                    continue
                uname = user.get("name", "").lower().strip()
                if (
                    name_lower in uname
                    or uname in name_lower
                    or set(name_lower.split()) & set(uname.split())
                ):
                    if not any(uid == user["id"] for _, uid in candidates):
                        candidates.append((user["name"], user["id"]))
    except Exception as e:
        raise ValidationError(f"Could not look up user '{name}': {e}")

    if len(candidates) == 1:
        return candidates[0][1]
    if len(candidates) > 1:
        names = ", ".join(f"'{n}'" for n, _ in candidates)
        raise ValidationError(
            f"The name '{name}' matches multiple team members: {names}.\n"
            "Please use the full name so I know exactly who you mean."
        )
    raise ValidationError(
        f"I couldn't find a team member named '{name}'.\n"
        "Please check the spelling or confirm they've been added to a project."
    )


def _resolve_status(name: str) -> int:
    statuses = rm.list_issue_statuses()
    name_lower = name.lower().strip()

    match = next((s for s in statuses if s["name"].lower() == name_lower), None)
    if match:
        return match["id"]

    for s in statuses:
        sname = s["name"].lower()
        query_words = set(name_lower.split()) - {"for", "the", "a", "an", "to", "is", "ready", "set"}
        status_words = set(sname.split())
        if query_words and query_words.issubset(status_words):
            logger.info(f"[RESOLVE STATUS] '{name}' → '{s['name']}' (semantic match)")
            return s["id"]
        if sname in name_lower or name_lower in sname:
            logger.info(f"[RESOLVE STATUS] '{name}' → '{s['name']}' (substring match)")
            return s["id"]

    available = ", ".join(s["name"] for s in statuses)
    raise ValidationError(
        f"I don't recognise the status '{name}'.\n"
        f"Available statuses are: {available}"
    )


def _resolve_tracker(name: str) -> int:
    """
    Resolve a tracker name to its Redmine ID.

    Uses a multi-pass strategy to handle instance-specific naming:
      1. Exact match (case-insensitive)
      2. Prefix match  ("Task" matches "Tasks")
      3. Substring match ("Bug" matches "Bug Reports")
      4. Raises ValidationError with available names listed
    """
    trackers = rm.list_trackers()
    name_lower = name.lower().strip()

    # Pass 1: exact
    match = next((t for t in trackers if t["name"].lower() == name_lower), None)
    if match:
        return match["id"]

    # Pass 2: prefix (handles "Task" → "Tasks", "Bug" → "Bugs")
    match = next((t for t in trackers if t["name"].lower().startswith(name_lower)), None)
    if match:
        logger.info(f"[RESOLVE TRACKER] '{name}' → '{match['name']}' (prefix match)")
        return match["id"]

    # Pass 3: substring
    match = next((t for t in trackers if name_lower in t["name"].lower()), None)
    if match:
        logger.info(f"[RESOLVE TRACKER] '{name}' → '{match['name']}' (substring match)")
        return match["id"]

    available = ", ".join(t["name"] for t in trackers)
    raise ValidationError(
        f"There's no tracker called '{name}'.\n"
        f"Available trackers: {available}"
    )


def _resolve_project(identifier: str, created_projects: dict[str, int] = None) -> int:
    """
    Resolve a project name/identifier to its Redmine ID.

    created_projects: optional dict of {identifier: project_id} for projects
    created earlier in the same plan execution. This avoids a cache miss when
    a plan creates a project and immediately creates an issue in it.
    """
    if created_projects:
        norm_input = identifier.lower().strip()
        norm_input_hyphen = norm_input.replace(" ", "-")
        for created_key, pid in created_projects.items():
            norm_key = created_key.lower().strip()
            if norm_input == norm_key or norm_input_hyphen == norm_key:
                logger.info(f"[RESOLVE PROJECT] '{identifier}' → ID {pid} (from plan context)")
                return pid

    try:
        return rm.resolve_project_id(identifier)
    except ValueError as e:
        raise ValidationError(str(e))
    except Exception as e:
        raise ValidationError(f"Could not look up project '{identifier}': {e}")


def _validate_status_transition(issue_id: int, new_status_id: int) -> None:
    try:
        data = rm.get_allowed_transitions(issue_id)
        allowed = data.get("allowed", [])
        if not allowed:
            return
        allowed_ids = {s["id"] for s in allowed}
        if new_status_id not in allowed_ids:
            current = data.get("current_status_name", "its current status")
            allowed_names = ", ".join(s["name"] for s in allowed)
            raise ValidationError(
                f"Issue #{issue_id} can't be moved from '{current}' to that status.\n"
                f"The allowed next statuses from '{current}' are: {allowed_names}.\n"
                f"This is controlled by the workflow rules set by your Redmine admin."
            )
    except ValidationError:
        raise
    except Exception:
        pass


# ── Priority map ───────────────────────────────────────────────────────────────

PRIORITY_MAP = {
    "low": 1,
    "normal": 2,
    "high": 3,
    "urgent": 4,
    "immediate": 5,
}


# ── Required field checker ─────────────────────────────────────────────────────

def _check_required(action_type: str, params: dict) -> None:
    fields = REQUIRED_FIELDS.get(action_type, [])
    missing = [(fname, fdesc) for fname, fdesc in fields if not params.get(fname)]
    if not missing:
        return
    lines = [f"To {action_type.replace('_', ' ')}, I need the following information:\n"]
    for fname, fdesc in missing:
        lines.append(f"  • {fname}: {fdesc}")
    raise ValidationError("\n".join(lines))


# ── Action handlers ────────────────────────────────────────────────────────────

def _exec_create_issue(params: dict, created_projects: dict[str, int] = None) -> str:
    _check_required("create_issue", params)
    logger.warning(
        f"[DEBUG create_issue] params={params} | "
        f"created_projects={created_projects}"
    )

    # ── Tracker: infer from keywords ─────────────────────────────────────────
    tracker_name = _infer_tracker(params)

    # ── Priority: default to Normal ──────────────────────────────────────────
    priority_str = (params.get("priority") or "normal").lower().strip()
    priority_id = PRIORITY_MAP.get(priority_str, 2)  # 2 = Normal

    # ── Resolve project ID ───────────────────────────────────────────────────
    project_id = _resolve_project(params["project"], created_projects)
    if not isinstance(project_id, int) or project_id <= 0:
        raise ValidationError(
            f"Could not resolve project '{params['project']}' to a valid Redmine ID. "
            "Please check the project name or identifier."
        )

    logger.warning(
        f"[DEBUG create_issue] resolved project_id={project_id} "
        f"tracker_name={tracker_name}"
    )

    # ── Resolve tracker ID ───────────────────────────────────────────────────
    tracker_id = _resolve_tracker(tracker_name)
    if not isinstance(tracker_id, int) or tracker_id <= 0:
        raise ValidationError(
            f"Could not resolve tracker '{tracker_name}' to a valid Redmine ID. "
            "Please check your Redmine tracker configuration."
        )

    logger.warning(f"[DEBUG create_issue] resolved tracker_id={tracker_id}")

    # ── Resolve status ID — default "New" ────────────────────────────────────
    status_id: int | None = None
    if params.get("status"):
        status_id = _resolve_status(params["status"])
    else:
        # Try to resolve "New" from Redmine's actual status list
        try:
            status_id = _resolve_status("New")
        except ValidationError:
            # If "New" doesn't exist, use ID 1 (Redmine's universal default)
            status_id = 1

    if not isinstance(status_id, int) or status_id <= 0:
        status_id = 1  # Final fallback

    # ── Log resolved IDs for debugging ───────────────────────────────────────
    logger.debug(
        f"[EXECUTOR] create_issue resolved IDs — "
        f"project_id={project_id}, tracker_id={tracker_id}, status_id={status_id}"
    )

    # ── Build payload with guaranteed non-None integer IDs ──────────────────
    payload: dict[str, Any] = {
        "project_id": project_id,
        "subject": params["subject"].strip(),
        "tracker_id": tracker_id,
        "priority_id": priority_id,
        "status_id": status_id,
    }

    if params.get("description"):
        payload["description"] = params["description"]
    if params.get("assignee"):
        payload["assigned_to_id"] = _resolve_user(params["assignee"])
    if params.get("due_date"):
        payload["due_date"] = _resolve_date(params["due_date"])
    if params.get("done_ratio") is not None:
        ratio = int(params["done_ratio"])
        if not (0 <= ratio <= 100):
            raise ValidationError("Progress must be a number between 0 and 100.")
        payload["done_ratio"] = ratio

    logger.warning(
        f"[DEBUG create_issue] FINAL payload going to rm.create_issue: "
        f"project_id={project_id}, tracker_id={tracker_id}, status_id={status_id}, "
        f"subject={params['subject']!r}"
    )
    result = rm.create_issue(**payload)

    if not result:
        raise RuntimeError(
            "The issue could not be created — Redmine returned an empty response.\n"
            "Please verify the project exists and your API key has permission to create issues."
        )

    lines = [
        f"✅ Issue #{result['id']} created: \"{result.get('subject')}\"\n",
        f"   Project:  {params['project']} \n",
        f"   Tracker:  {tracker_name}\n",
        f"   Priority: {priority_str.capitalize()}\n",
        f"   Assignee: {params.get('assignee', 'Unassigned')}\n",
        f"   Due date: {params.get('due_date', 'Not set')}\n",
    ]
    return "\n".join(lines)


def _exec_update_issue(params: dict, **_) -> str:
    _check_required("update_issue", params)
    issue_id = params["issue_id"]
    kwargs: dict[str, Any] = {}

    if params.get("status"):
        status_id = _resolve_status(params["status"])
        _validate_status_transition(issue_id, status_id)
        kwargs["status_id"] = status_id

    if params.get("assignee"):
        kwargs["assigned_to_id"] = _resolve_user(params["assignee"])

    if params.get("priority"):
        priority_id = PRIORITY_MAP.get(params["priority"].lower())
        if priority_id is None:
            raise ValidationError(
                f"'{params['priority']}' is not a valid priority.\n"
                "Valid options are: Low, Normal, High, Urgent, Immediate."
            )
        kwargs["priority_id"] = priority_id

    if params.get("tracker"):
        kwargs["tracker_id"] = _resolve_tracker(_infer_tracker(params))

    if params.get("due_date"):
        kwargs["due_date"] = _resolve_date(params["due_date"])

    if params.get("subject"):
        kwargs["subject"] = params["subject"].strip()

    if params.get("description"):
        kwargs["description"] = params["description"]

    if params.get("done_ratio") is not None:
        ratio = int(params["done_ratio"])
        if not (0 <= ratio <= 100):
            raise ValidationError("Progress must be a number between 0 and 100.")
        kwargs["done_ratio"] = ratio

    if params.get("notes"):
        kwargs["notes"] = params["notes"]

    if not kwargs:
        raise ValidationError(
            "No fields to update were provided.\n"
            "You can update: status, assignee, priority, tracker, due date, "
            "subject, description, progress (0–100), or add a note."
        )

    logger.debug(f"[EXECUTOR] update_issue #{issue_id} payload: {kwargs}")
    error = rm.update_issue(issue_id, **kwargs)

    if error == "NOT_FOUND":
        raise ValidationError(
            f"Issue #{issue_id} doesn't exist — please check the issue number."
        )
    if error and error.startswith("WORKFLOW_ERROR"):
        detail = error.replace("WORKFLOW_ERROR: ", "")
        raise ValidationError(
            f"Redmine rejected the status change on issue #{issue_id}.\n"
            f"Reason: {detail}\n"
            f"Your role may not allow this transition — check workflow settings with your admin."
        )
    if error and error.startswith("VALIDATION_ERROR"):
        detail = error.replace("VALIDATION_ERROR: ", "")
        raise ValidationError(
            f"Redmine rejected the update on issue #{issue_id}.\n"
            f"Reason: {detail}"
        )

    field_labels = {
        "status_id": f"status → {params.get('status')}\n",
        "assigned_to_id": f"assignee → {params.get('assignee')}\n",
        "priority_id": f"priority → {params.get('priority')}\n",
        "tracker_id": f"tracker → {params.get('tracker')}\n",
        "due_date": f"due date → {params.get('due_date')}\n",
        "subject": f"subject → \"{params.get('subject')}\"\n",
        "description": "description updated\n",
        "done_ratio": f"progress → {params.get('done_ratio')}%\n",
        "notes": "note added",
    }
    changes = [label for key, label in field_labels.items() if key in kwargs]
    return f"\n✅ Issue #{issue_id} updated: {', '.join(changes)}\n"


def _exec_search_and_update(params: dict, **_) -> str:
    search_kwargs: dict[str, Any] = {}

    if params.get("filter_project"):
        search_kwargs["project_id"] = _resolve_project(params["filter_project"])

    if params.get("filter_tracker"):
        search_kwargs["tracker_id"] = _resolve_tracker(params["filter_tracker"])

    if params.get("filter_status"):
        search_kwargs["status"] = _resolve_status(params["filter_status"])
    else:
        search_kwargs["status"] = "open"

    issues = rm.list_issues(**search_kwargs, limit=100)

    if params.get("filter_priority"):
        priority_name = params["filter_priority"].lower()
        issues = [
            i for i in issues
            if i.get("priority", {}).get("name", "").lower() == priority_name
        ]

    if params.get("filter_assignee"):
        assignee_id = _resolve_user(params["filter_assignee"])
        issues = [
            i for i in issues
            if i.get("assigned_to", {}).get("id") == assignee_id
        ]

    if not issues:
        filters_desc = ", ".join(
            f"{k.replace('filter_', '')}={v}"
            for k, v in params.items()
            if k.startswith("filter_") and v
        )
        return f"⚠️ No issues found matching: {filters_desc}."

    issue_ids = [i["id"] for i in issues]
    update_params = {
        "issue_ids": issue_ids,
        **{k: v for k, v in params.items() if not k.startswith("filter_")},
    }
    update_result = _exec_bulk_update(update_params)
    return f"Found {len(issue_ids)} matching issue(s).\n\n{update_result}"


def _exec_bulk_update(params: dict, **_) -> str:
    _check_required("bulk_update_issues", params)
    issue_ids: list[int] = params["issue_ids"]

    status_id = _resolve_status(params["status"]) if params.get("status") else None
    assigned_to_id = _resolve_user(params["assignee"]) if params.get("assignee") else None
    priority_id = PRIORITY_MAP.get(params.get("priority", "").lower()) if params.get("priority") else None
    tracker_id = _resolve_tracker(_infer_tracker(params)) if params.get("tracker") else None
    done_ratio: int | None = None

    if params.get("done_ratio") is not None:
        done_ratio = int(params["done_ratio"])
        if not (0 <= done_ratio <= 100):
            raise ValidationError("Progress must be a number between 0 and 100.")

    if not any([status_id, assigned_to_id, priority_id, tracker_id,
                params.get("notes"), done_ratio is not None]):
        raise ValidationError(
            "No update fields were provided for the bulk update.\n"
            "You can bulk-update: status, assignee, priority, tracker, progress, or add a note."
        )

    success, failed = [], []
    for issue_id in issue_ids:
        if status_id is not None:
            try:
                _validate_status_transition(issue_id, status_id)
            except ValidationError as e:
                failed.append((issue_id, str(e)))
                continue

        err = rm.update_issue(
            issue_id,
            status_id=status_id,
            assigned_to_id=assigned_to_id,
            priority_id=priority_id,
            tracker_id=tracker_id,
            done_ratio=done_ratio,
            notes=params.get("notes", ""),
        )
        if err == "NOT_FOUND":
            failed.append((issue_id, f"Issue #{issue_id} doesn't exist — please check the issue number."))
        elif err:
            clean_err = err.replace("WORKFLOW_ERROR: ", "").replace("VALIDATION_ERROR: ", "")
            failed.append((issue_id, clean_err))
        else:
            success.append(issue_id)

    lines = [f"Bulk update: {len(success)} of {len(issue_ids)} issue(s) updated successfully.\n"]
    if success:
        lines.append(f"✅ Updated: {', '.join('#' + str(i) for i in success)}\n")
    if failed:
        lines.append("❌ The following could not be updated:")
        for issue_id, reason in failed:
            lines.append(f"   • Issue #{issue_id}: {reason}")
    return "\n".join(lines)


def _exec_delete_issue(params: dict, **_) -> str:
    _check_required("delete_issue", params)
    issue_id = params["issue_id"]
    error = rm.delete_issue(issue_id)
    if error == "NOT_FOUND":
        raise ValidationError(
            f"Issue #{issue_id} doesn't exist — it may have already been deleted."
        )
    return f"🗑️ Issue #{issue_id} has been permanently deleted."


def _exec_create_project(params: dict, **_) -> tuple[str, dict]:
    _check_required("create_project", params)
    identifier = params["identifier"].lower().replace(" ", "-")

    try:
        existing_id = rm.resolve_project_id(identifier)
        lines = [
            f"ℹ️ Project '{params['name']}' already exists — using the existing project.",
            f"   Identifier: {identifier}",
        ]
        return "\n".join(lines), {
            identifier: existing_id,
            params["name"]: existing_id,
            params["name"].lower(): existing_id,
        }
    except (ValueError, Exception):
        pass

    result = rm.create_project(
        name=params["name"],
        identifier=identifier,
        description=params.get("description", ""),
        is_public=params.get("is_public", False),
    )
    if not result:
        raise RuntimeError(
            "The project could not be created — Redmine returned an empty response.\n"
            "Make sure the identifier is unique and uses only lowercase letters, numbers, and hyphens."
        )

    new_id = result["id"]
    lines = [
        f"✅ Project '{params['name']}' created.",
        f"   Identifier: {identifier}",
        f"   Visibility: {'Public' if params.get('is_public') else 'Private'}",
    ]
    return "\n".join(lines), {
        identifier: new_id,
        params["name"]: new_id,
        params["name"].lower(): new_id,
    }


# ── Dispatch table ─────────────────────────────────────────────────────────────

_HANDLERS = {
    "create_issue": _exec_create_issue,
    "update_issue": _exec_update_issue,
    "bulk_update_issues": _exec_bulk_update,
    "delete_issue": _exec_delete_issue,
    "search_and_update": _exec_search_and_update,
    "create_project": _exec_create_project,
    # "add_file_to_issue": _exec_add_file,
}


# ── Main executor ──────────────────────────────────────────────────────────────

class ActionExecutor:
    def execute(self, plan: ActionPlan) -> str:
        results = []
        created_projects: dict[str, int] = {}

        for action in plan.actions:

            if action.type == "unsupported":
                return (
                    f"I'm not able to do that: "
                    f"{action.params.get('reason', action.description)}\n\n"
                    "Here's what I can help with:\n"
                    "  • Creating and updating issues (status, assignee, priority, tracker, progress, due date)\n"
                    "  • Bulk-updating multiple issues at once\n"
                    "  • Deleting issues (with confirmation)\n"
                    "  • Creating projects\n"
                    "  • Attaching files to issues"
                )

            if action.type == "needs_clarification":
                return action.params.get(
                    "question",
                    "Could you clarify what you'd like me to do?"
                )

            handler = _HANDLERS.get(action.type)
            if not handler:
                results.append(f"❌ Unknown action type: '{action.type}'")
                break

            try:
                # Always pass created_projects so chained create_project →
                # create_issue works even when the project was just created
                # in the same plan execution (cache hasn't updated yet).
                raw_result = handler(action.params, created_projects=created_projects)

                if isinstance(raw_result, tuple):
                    message, new_projects = raw_result
                    created_projects.update(new_projects)
                    results.append(message)
                else:
                    results.append(raw_result)

                logger.info(f"[EXECUTOR] {action.type} → success")

            except ValidationError as e:
                results.append(f"⚠️ {e}")
                logger.warning(f"[EXECUTOR] Validation error ({action.type}): {e}")
                break

            except Exception as e:
                results.append(
                    f"❌ Something went wrong while executing '{action.description or action.type}':\n   {e}"
                )
                logger.error(f"[EXECUTOR] Runtime error ({action.type}): {e}")
                break

        return "\n\n".join(results) if results else "No actions were executed."


# ── Date resolver ─────────────────────────────────────────────────────────────

def _resolve_date(value: str) -> str:
    if not value:
        return value
    if _re.match(r"^\d{4}-\d{2}-\d{2}$", value.strip()):
        return value.strip()

    v = value.lower().strip()
    today = date.today()

    if v == "today":
        return today.isoformat()
    if v == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for i, day in enumerate(weekdays):
        if day in v:
            days_ahead = (i - today.weekday() + 7) % 7
            if days_ahead == 0:
                days_ahead = 7
            return (today + timedelta(days=days_ahead)).isoformat()

    m = _re.search(r"in (\d+) (day|week)", v)
    if m:
        n = int(m.group(1))
        delta = n * 7 if "week" in m.group(2) else n
        return (today + timedelta(days=delta)).isoformat()

    raise ValidationError(
        f"I couldn't understand the date '{value}'.\n"
        "Please use a specific date like '2025-04-18' or say 'next Friday'."
    )
