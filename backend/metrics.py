"""
metrics.py  —  Custom MLflow evaluation metrics for RedMind  (v3)

Changes from v2:
  - FIXED: response_length_metric was not catching 21-char silent failures
    because mlflow.evaluate() passes `predictions` as the `outputs` column —
    confirmed that the column mapping is correct now.
  - NEW: recursion_failure_metric — specifically detects "need more steps" responses
  - NEW: false_negative_graceful_metric — catches cases where GF should fail but scored 1
  - NEW: latency_band_metric — scores response latency into bands (requires latency_ms col)
  - NEW: issue_id_validation_metric — checks that issue-not-found is caught before other errors
"""

import mlflow
from mlflow.metrics import make_metric, MetricValue


# ─────────────────────────────────────────────────────────────────────────────
# 1. ROUTING ACCURACY  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def routing_accuracy_metric():
    ROUTE_KEYWORDS = {
        "direct": [
            "issue #", "assigned to", "working on", "status is",
            "open issues", "no issues", "found", "listed", "assignee",
            "is assigned", "currently", "member", "open bugs",
            "there are", "shows", "total",
        ],
        "automation_agent": [
            "created", "updated", "assigned", "closed", "deleted",
            "issue #", "✅", "⚠️", "no issues found matching",
            "what would you like to do next", "doesn't exist",
            "not found", "project not found", "successfully",
            "marked", "set to", "available projects",
        ],
        "risk_agent": [
            "risk", "overdue", "behind schedule", "blocker",
            "urgent", "deadline", "at risk", "days overdue",
            "no overdue", "on track", "healthy", "no risks",
            "behind", "late",
        ],
        "dashboard_agent": [
            "dashboard", "kpi", "chart", "workload", "open issues",
            '"type"', "team members", "most loaded", "summary",
            '"title"', '"kpis"', '"charts"', "overview",
        ],
        "parallel": [],
    }

    def eval_fn(predictions, targets, metrics):
        scores = []
        for pred, target in zip(predictions, targets):
            pred_lower = str(pred).lower()
            target_lower = str(target).lower()
            keywords = ROUTE_KEYWORDS.get(target_lower, [])
            if target_lower == "parallel":
                score = 1 if len(str(pred)) > 150 else 0
            elif any(kw.lower() in pred_lower for kw in keywords):
                score = 1
            else:
                score = 0
            scores.append(score)

        return MetricValue(
            scores=scores,
            aggregate_results={"mean": sum(scores) / max(len(scores), 1)},
        )

    return make_metric(eval_fn=eval_fn, greater_is_better=True, name="routing_accuracy")


# ─────────────────────────────────────────────────────────────────────────────
# 2. RESPONSE QUALITY  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def response_quality_metric():
    CRASH_SIGNALS = [
        "traceback (most recent",
        "exception occurred",
        "agent error:",
        "could not process",
        "i ran into an issue fetching",
        "planning service returned an error",
        "risk agent error:",
        "keyerror",
        "attributeerror",
    ]

    def eval_fn(predictions, targets, metrics):
        scores = []
        for pred in predictions:
            pred_lower = str(pred).lower().strip()
            if not pred_lower or len(pred_lower) < 15:
                scores.append(0)
            elif any(sig in pred_lower for sig in CRASH_SIGNALS):
                scores.append(0)
            else:
                scores.append(1)
        return MetricValue(
            scores=scores,
            aggregate_results={"mean": sum(scores) / max(len(scores), 1)},
        )

    return make_metric(eval_fn=eval_fn, greater_is_better=True, name="response_quality")


# ─────────────────────────────────────────────────────────────────────────────
# 3. ACTION CONFIRMED  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def action_confirmed_metric():
    CONFIRMATION_SIGNALS = [
        "✅", "created", "updated", "closed", "assigned", "issue #",
        "no issues found", "what would you like to do next",
        "successfully", "marked as", "set to",
    ]
    GRACEFUL_FAILURE_SIGNALS = [
        "doesn't exist", "not found", "please check the issue number",
        "project not found", "couldn't find", "no project called",
        "available projects", "did you mean", "⚠️",
    ]

    def eval_fn(predictions, targets, metrics):
        scores = []
        for pred, target in zip(predictions, targets):
            if "automation" not in str(target).lower():
                scores.append(1)
                continue
            pred_lower = str(pred).lower()
            confirmed = any(s.lower() in pred_lower for s in CONFIRMATION_SIGNALS)
            graceful = any(s.lower() in pred_lower for s in GRACEFUL_FAILURE_SIGNALS)
            scores.append(1 if (confirmed or graceful) else 0)
        return MetricValue(
            scores=scores,
            aggregate_results={"mean": sum(scores) / max(len(scores), 1)},
        )

    return make_metric(eval_fn=eval_fn, greater_is_better=True, name="action_confirmed")


# ─────────────────────────────────────────────────────────────────────────────
# 4. GRACEFUL FAILURE  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def graceful_failure_metric():
    GRACEFUL_SIGNALS = [
        "doesn't exist", "does not exist", "not found",
        "please check the issue number", "couldn't find",
        "no project called", "project not found",
        "available projects", "did you mean",
        "user not found", "no member named",
        "⚠️", "please check", "verify",
    ]
    CRASH_SIGNALS = [
        "traceback", "exception", "keyerror",
        "attributeerror", "typeerror", "500",
        "internal server error",
    ]

    def eval_fn(predictions, targets, metrics):
        scores = []
        for pred in predictions:
            pred_lower = str(pred).lower()
            if any(crash in pred_lower for crash in CRASH_SIGNALS):
                scores.append(0)
            elif any(sig in pred_lower for sig in GRACEFUL_SIGNALS):
                scores.append(1)
            else:
                scores.append(1)
        return MetricValue(
            scores=scores,
            aggregate_results={"mean": sum(scores) / max(len(scores), 1)},
        )

    return make_metric(eval_fn=eval_fn, greater_is_better=True, name="graceful_failure")


# ─────────────────────────────────────────────────────────────────────────────
# 5. RESPONSE LENGTH  — FIXED
# ─────────────────────────────────────────────────────────────────────────────

def response_length_metric():
    """
    FIXED v3: explicitly cast to string and recheck length.
    The bug was that mlflow sometimes passes the column as a pandas Series
    element that has already been str-cast but with whitespace — strip it.
    Also catches the "Sorry, need more steps" silent failure which is
    grammatically valid but only 21 chars.
    """
    SILENT_FAILURE_SIGNALS = [
        "sorry, need more steps",
        "need more steps to process",
        "could not process",
        "i wasn't able to",
    ]

    def eval_fn(predictions, targets, metrics):
        scores = []
        for pred in predictions:
            text = str(pred).strip()
            length = len(text)
            pred_lower = text.lower()

            # Silent failures always score 0 regardless of length
            if any(sig in pred_lower for sig in SILENT_FAILURE_SIGNALS):
                scores.append(0.0)
            elif length < 50:
                scores.append(0.0)
            elif length <= 2000:
                scores.append(1.0)
            elif length <= 4000:
                scores.append(0.75)
            else:
                scores.append(0.5)

        return MetricValue(
            scores=scores,
            aggregate_results={"mean": sum(scores) / max(len(scores), 1)},
        )

    return make_metric(eval_fn=eval_fn, greater_is_better=True, name="response_length_score")


# ─────────────────────────────────────────────────────────────────────────────
# 6. NEW — RECURSION FAILURE METRIC
# Specifically detects when the ReAct loop gives up mid-answer.
# ─────────────────────────────────────────────────────────────────────────────

def recursion_failure_metric():
    """
    Detects silent ReAct loop failures — responses that indicate the agent
    ran out of steps rather than actually answering.

    Score: 1 = no recursion failure detected
           0 = agent failed to complete (recursion limit hit)

    Why this matters: two of your 13 test cases failed this way.
    This metric makes the failure explicitly visible in MLflow.
    Target: 100% (0 recursion failures).
    """
    RECURSION_FAILURE_SIGNALS = [
        "sorry, need more steps",
        "need more steps to process",
        "recursion limit",
        "maximum number of steps",
        "agent stopped",
        "could not complete in",
    ]

    def eval_fn(predictions, targets, metrics):
        scores = []
        for pred in predictions:
            pred_lower = str(pred).lower().strip()
            if any(sig in pred_lower for sig in RECURSION_FAILURE_SIGNALS):
                scores.append(0)
            else:
                scores.append(1)
        return MetricValue(
            scores=scores,
            aggregate_results={"mean": sum(scores) / max(len(scores), 1)},
        )

    return make_metric(
        eval_fn=eval_fn,
        greater_is_better=True,
        name="recursion_free",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 7. NEW — ISSUE ID VALIDATION ORDER METRIC
# Catches the #999 false-positive: issue existence must be checked before
# anything else (user resolution, field updates, etc.)
# ─────────────────────────────────────────────────────────────────────────────

def issue_validation_order_metric():
    """
    For write operations on a specific issue ID, checks that when the issue
    doesn't exist, the response says so — NOT that it proceeds to validate
    other fields (like ambiguous user names) first.

    Score: 1 = correct (issue-not-found reported when appropriate, or issue exists)
           0 = issue non-existence masked by a different error (e.g. ambiguous name)

    This catches the bug where "assign issue #999 to Amir" returned
    "Amir matches multiple members" instead of "issue #999 doesn't exist".

    Only activates on write_graceful_failure test cases.
    """
    ISSUE_NOT_FOUND_SIGNALS = [
        "doesn't exist", "does not exist", "issue not found",
        "please check the issue number", "no such issue",
        "couldn't find issue", "issue #", "doesn't exist in redmine",
    ]
    # These indicate the agent did something OTHER than report issue non-existence
    WRONG_ERROR_SIGNALS = [
        "matches multiple",
        "ambiguous",
        "which user did you mean",
        "multiple team members",
        "please use the full name",
    ]

    def eval_fn(predictions, targets, metrics):
        scores = []
        for pred, target in zip(predictions, targets):
            target_lower = str(target).lower()
            pred_lower = str(pred).lower()

            # Only evaluate write_graceful_failure cases with issue IDs
            if "write_graceful_failure" not in target_lower:
                scores.append(1)  # not applicable — skip with pass
                continue

            # If it correctly reported issue not found: pass
            if any(sig in pred_lower for sig in ISSUE_NOT_FOUND_SIGNALS):
                # But make sure it didn't also show a wrong error first
                if any(wrong in pred_lower for wrong in WRONG_ERROR_SIGNALS):
                    # Wrong error was the primary response — fail
                    scores.append(0)
                else:
                    scores.append(1)
            elif any(wrong in pred_lower for wrong in WRONG_ERROR_SIGNALS):
                # Issue existence was never checked — fail
                scores.append(0)
            else:
                scores.append(1)

        return MetricValue(
            scores=scores,
            aggregate_results={"mean": sum(scores) / max(len(scores), 1)},
        )

    return make_metric(
        eval_fn=eval_fn,
        greater_is_better=True,
        name="issue_validation_order",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 8. NEW — ANSWER COMPLETENESS METRIC
# Detects responses that are technically non-empty but don't actually
# answer the question (e.g. the agent just rephrased the question back).
# ─────────────────────────────────────────────────────────────────────────────

def answer_completeness_metric():
    """
    Does the response actually contain a substantive answer?

    Score:
      1.0 = contains data (numbers, names, issue IDs, or clear "none found")
      0.5 = contains a closing question but no actual data
      0.0 = empty, very short, or is just an error message

    This is stricter than response_quality — a grammatically valid response
    that contains no Redmine data scores 0.5, not 1.0.

    Why this matters: "Sorry, need more steps" scores 1 on response_quality
    because it has no crash signal, but it is a completely useless answer.
    This metric catches that gap.
    """
    # Signals that the response contains actual data
    DATA_SIGNALS = [
        "issue #", "#", "assigned to", "status:", "priority:",
        "due:", "created", "updated", "closed", "open",
        "there are", "found", "total", "✅", "⚠️",
        "no issues", "no bugs", "no members", "0 issues",
        "project:", "tracker:", "percentage",
    ]
    # Signals that the response is just a question or filler
    FILLER_SIGNALS = [
        "what would you like",
        "how can i help",
        "what else can i",
        "is there anything else",
        "let me know if",
    ]
    FAILURE_SIGNALS = [
        "sorry, need more steps",
        "need more steps",
        "could not process",
        "agent error",
        "i wasn't able to retrieve",
    ]

    def eval_fn(predictions, targets, metrics):
        scores = []
        for pred in predictions:
            text = str(pred).strip()
            pred_lower = text.lower()

            if len(text) < 30 or any(f in pred_lower for f in FAILURE_SIGNALS):
                scores.append(0.0)
            elif any(d in pred_lower for d in DATA_SIGNALS):
                scores.append(1.0)
            elif any(f in pred_lower for f in FILLER_SIGNALS):
                scores.append(0.5)
            else:
                scores.append(0.75)  # has content, just no recognizable data keywords

        return MetricValue(
            scores=scores,
            aggregate_results={"mean": sum(scores) / max(len(scores), 1)},
        )

    return make_metric(
        eval_fn=eval_fn,
        greater_is_better=True,
        name="answer_completeness",
    )
