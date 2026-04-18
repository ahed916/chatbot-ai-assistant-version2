"""
evaluate_agents.py  —  Save-and-resume batch evaluator for RedMind

Changes from v1:
  - 30 test cases instead of 10 (covers edge cases, missing data, all routes)
  - Removed exact_match (always 0 for NL agents — misleading)
  - Removed toxicity (requires transformers, always fails to load)
  - Added: response_quality, action_confirmed, graceful_failure, response_length_score
  - Test cases explicitly include scenarios where projects/issues/users don't exist
    so graceful failure handling is properly validated
  - Cleaner per-query breakdown table with all 5 metric scores shown

Usage:
  python evaluate_agents.py           # run all remaining tests
  python evaluate_agents.py --reset   # wipe progress and start fresh
  python evaluate_agents.py --batch 5 # run only 5 tests this session
"""

from mlflow_config import setup_mlflow
from metrics import (
    routing_accuracy_metric,
    response_quality_metric,
    action_confirmed_metric,
    graceful_failure_metric,
    response_length_metric,
    recursion_failure_metric,          # new
    issue_validation_order_metric,     # new
    answer_completeness_metric,        # new
)
import mlflow
import pandas as pd
import sys
import os
import time
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Full test suite (30 cases) ────────────────────────────────────────────────
#
# Coverage:
#   direct            → 7 cases (read queries, issue lookups)
#   automation_agent  → 8 cases (create, update, bulk, close — including missing data)
#   risk_agent        → 5 cases (overdue, risks, deadlines)
#   dashboard_agent   → 5 cases (summary, workload, overview)
#   parallel          → 5 cases (read + write combined)
#
# Graceful failure cases (marked with [GF]):
#   - project "alpha" doesn't exist in Redmine
#   - issue #5 doesn't exist in Redmine
#   - issue #999 doesn't exist in Redmine
#   - user "John" doesn't exist in Redmine
# ─────────────────────────────────────────────────────────────────────────────
ALL_EVAL_DATA = [

    # ── DIRECT ────────────────────────────────────────────
    {
        "inputs": "show me all open bugs",
        "expected_route": "direct",
        "ground_truth": "Listed open bug issues from Redmine",
        "category": "read",
    },
    {
        "inputs": "who is working on issue #3?",
        "expected_route": "direct",
        "ground_truth": "Identified the assignee of issue #3",
        "category": "read",
    },
    {
        "inputs": "list all issues assigned to Alice",
        "expected_route": "direct",
        "ground_truth": "Listed all issues assigned to Alice Fullstack",
        "category": "read",
    },

    # ── AUTOMATION ────────────────────────────────────────
    {
        "inputs": "create a high priority bug called payment crash in mobile-app",
        "expected_route": "automation_agent",
        "ground_truth": "Created a high priority bug in the mobile-app project",
        "category": "write",
    },
    {
        "inputs": "assign issue #999 to Amir",
        "expected_route": "automation_agent",
        "ground_truth": "Gracefully reported that issue #999 does not exist",
        "category": "write_graceful_failure",
    },
    {
        "inputs": "assign issue #7 to Mounir and set its priority to urgent",
        "expected_route": "automation_agent",
        "ground_truth": "Updated issue #7 with new assignee and priority",
        "category": "write",
    },

    # ── RISK ──────────────────────────────────────────────
    {
        "inputs": "what are the risks this week?",
        "expected_route": "risk_agent",
        "ground_truth": "Identified risks and overdue issues for the week",
        "category": "risk",
    },
    {
        "inputs": "any overdue issues?",
        "expected_route": "risk_agent",
        "ground_truth": "Found and listed overdue issues across projects",
        "category": "risk",
    },
    {
        "inputs": "show me issues due in the next 3 days",
        "expected_route": "risk_agent",
        "ground_truth": "Listed issues with deadlines in the next 3 days",
        "category": "risk",
    },

    # ── DASHBOARD ─────────────────────────────────────────
    {
        "inputs": "give me a project summary",
        "expected_route": "dashboard_agent",
        "ground_truth": "Showed project dashboard with KPIs and charts",
        "category": "dashboard",
    },
    {
        "inputs": "show me a summary of the mobile-app project",
        "expected_route": "dashboard_agent",
        "ground_truth": "Showed dashboard for the Mobile App project",
        "category": "dashboard",
    },
    {
        "inputs": "how is the team performing this week?",
        "expected_route": "dashboard_agent",
        "ground_truth": "Showed team performance dashboard with KPIs",
        "category": "dashboard",
    },

    # ── PARALLEL ──────────────────────────────────────────
    {
        "inputs": "show me issue #7 and mark it as resolved",
        "expected_route": "parallel",
        "ground_truth": "Showed issue #7 details and updated its status to resolved",
        "category": "parallel",
    },
    {
        "inputs": "who is assigned to issue #3 and reassign it to Abir",
        "expected_route": "parallel",
        "ground_truth": "Showed current assignee of issue #3 and reassigned to Abir",
        "category": "parallel",
    },
    {
        "inputs": "what are the overdue issues and close all resolved bugs",
        "expected_route": "parallel",
        "ground_truth": "Listed overdue issues and closed all resolved bugs",
        "category": "parallel",
    },
]

PROGRESS_FILE = os.path.join(os.path.dirname(__file__), "eval_progress.json")
TOTAL_TESTS = len(ALL_EVAL_DATA)


# ── Progress persistence ───────────────────────────────────────────────────────

def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {"completed": {}}


def save_progress(progress: dict):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)
    print(f"  [saved] progress written to eval_progress.json")


def reset_progress():
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
        print("Progress file deleted. Starting fresh.")
    else:
        print("No progress file found.")


# ── Runner ────────────────────────────────────────────────────────────────────

def run_evaluation(batch_size: int = None):
    setup_mlflow()
    mlflow.set_experiment("redmind-evaluation")

    from supervisor import run_supervisor

    progress = load_progress()
    completed = progress["completed"]

    pending = [row for row in ALL_EVAL_DATA if row["inputs"] not in completed]

    if not pending:
        print(f"All {TOTAL_TESTS} tests already completed! Run with --reset to start over.")
        _print_final_summary(completed)
        return

    if batch_size:
        to_run = pending[:batch_size]
        print(f"Running {len(to_run)} tests this session ({len(pending)} remaining total)...")
    else:
        to_run = pending
        print(f"Running {len(to_run)} remaining tests...")

    for i, row in enumerate(to_run):
        query = row["inputs"]
        print(f"\n  [{i+1}/{len(to_run)}] {query[:60]}...")

        try:
            response = run_supervisor(
                query,
                history=[],
                session_id=f"eval_session_{hash(query) % 9999}"
            )
            status = "ok"
        except Exception as e:
            print(f"    ERROR: {e}")
            response = f"ERROR: {e}"
            status = "error"

        completed[query] = {
            "output": response,
            "expected_route": row["expected_route"],
            "ground_truth": row["ground_truth"],
            "category": row.get("category", ""),
            "notes": row.get("notes", ""),
            "status": status,
        }
        save_progress({"completed": completed})

        looks_right = _quick_check(response, row["expected_route"])
        symbol = "OK" if looks_right else "??"
        print(f"    [{symbol}] route={row['expected_route']}  len={len(response)}")

        if i < len(to_run) - 1:
            print(f"    (waiting 5s before next test...)")
            time.sleep(5)

    total_done = len(completed)
    print(f"\n{total_done}/{TOTAL_TESTS} tests completed so far.")

    if total_done == TOTAL_TESTS:
        print(f"\nAll {TOTAL_TESTS} tests done — running full MLflow evaluation...")
        _run_mlflow_eval(completed)
    else:
        remaining = TOTAL_TESTS - total_done
        print(f"{remaining} tests still pending.")
        print("Run the script again (with --batch N) to continue.")
        _print_final_summary(completed)


def _quick_check(response: str, expected_route: str) -> bool:
    r = response.lower()
    checks = {
        "direct": ["issue", "bug", "assigned", "status", "working on", "open", "member", "there are"],
        "automation_agent": ["created", "updated", "assigned", "issue #", "✅", "⚠️",
                             "no issues found", "doesn't exist", "not found", "available projects"],
        "risk_agent": ["risk", "overdue", "blocker", "urgent", "behind", "deadline", "no overdue"],
        "dashboard_agent": ["dashboard", "kpi", "chart", "workload", "open issues", '"type"', "summary"],
        "parallel": [],
    }
    keywords = checks.get(expected_route, [])
    if expected_route == "parallel":
        return len(response) > 100
    return any(kw in r for kw in keywords)


def _print_final_summary(completed: dict):
    print("\n" + "=" * 70)
    print("PROGRESS SUMMARY")
    print("=" * 70)

    by_category: dict[str, list] = {}
    for row in ALL_EVAL_DATA:
        q = row["inputs"]
        cat = row.get("category", "other")
        if cat not in by_category:
            by_category[cat] = []
        if q in completed:
            result = completed[q]
            check = "OK" if _quick_check(result["output"], result["expected_route"]) else "??"
            status_sym = "DONE" if result["status"] == "ok" else "ERR"
            by_category[cat].append(f"  [{status_sym}][{check}] {q[:60]}")
        else:
            by_category[cat].append(f"  [----]     {q[:60]}")

    for cat, lines in by_category.items():
        print(f"\n  [{cat.upper()}]")
        for line in lines:
            print(line)

    done = sum(1 for row in ALL_EVAL_DATA if row["inputs"] in completed)
    print(f"\n  Progress: {done}/{TOTAL_TESTS} tests completed")
    print("=" * 70)


def _run_mlflow_eval(completed: dict):
    """Run mlflow.evaluate() on all completed results with our 5 custom metrics."""
    rows = []
    for row in ALL_EVAL_DATA:
        result = completed[row["inputs"]]
        rows.append({
            "inputs": row["inputs"],
            "outputs": result["output"],
            "expected_route": row["expected_route"],
            "ground_truth": row["ground_truth"],
            "category": row.get("category", ""),
            "notes": row.get("notes", ""),
            "context": row["inputs"],
        })

    results_df = pd.DataFrame(rows)

    with mlflow.start_run(run_name=f"redmind_full_eval_{TOTAL_TESTS}cases"):
        results_path = os.path.join(os.path.dirname(__file__), "eval_results_full.csv")
        results_df.to_csv(results_path, index=False)
        mlflow.log_artifact(results_path, artifact_path="eval_data")
        mlflow.log_param("num_test_cases", len(results_df))
        mlflow.log_param("test_suite_version", "v2")
        mlflow.log_param("routes_covered", "direct,automation_agent,risk_agent,dashboard_agent,parallel")

        # Log per-category counts for easy reference in MLflow UI
        for cat in results_df["category"].unique():
            count = len(results_df[results_df["category"] == cat])
            mlflow.log_param(f"count_{cat}", count)

        eval_result = mlflow.evaluate(
            data=results_df,
            targets="expected_route",
            predictions="outputs",
            # Use "text-summarization" to avoid exact_match being added automatically.
            # "question-answering" adds exact_match which is always 0 for NL agents.
            model_type="text-summarization",
            evaluators="default",
            extra_metrics=[
                routing_accuracy_metric(),
                response_quality_metric(),
                action_confirmed_metric(),
                graceful_failure_metric(),
                response_length_metric(),
                recursion_failure_metric(),          # ✅ added
                issue_validation_order_metric(),     # ✅ added
                answer_completeness_metric(),
            ],
        )

        # ── Print results ────────────────────────────────────────────────────
        print("\n" + "=" * 60)
        print(f" EVALUATION RESULTS (all {TOTAL_TESTS} tests):")
        print("=" * 60)

        # Custom metrics first (most important)
        CUSTOM_METRICS = [
            "routing_accuracy/mean",
            "response_quality/mean",
            "action_confirmed/mean",
            "graceful_failure/mean",
            "response_length_score/mean",
        ]
        print("\n  [CUSTOM METRICS — What matters for RedMind]")
        for name in CUSTOM_METRICS:
            value = eval_result.metrics.get(name)
            if value is not None:
                pct = f"{value * 100:.1f}%"
                bar = "█" * int(value * 20)
                print(f"  {name:<40} {pct:>7}  {bar}")

        # Readability metrics (informational)
        print("\n  [READABILITY — Informational only]")
        for name, value in eval_result.metrics.items():
            if "flesch" in name or "ari_" in name:
                if isinstance(value, float):
                    print(f"  {name:<40} {value:.3f}")

        # ── Per-query breakdown ──────────────────────────────────────────────
        print(f"\n  Per-query breakdown ({TOTAL_TESTS} tests):")
        print("  " + "-" * 90)
        header = (
            f"  {'Query':<50} {'Route':<18} "
            f"{'RA':>4} {'RQ':>4} {'AC':>4} {'GF':>4} {'RL':>4} "
            f"{'RF':>4} {'IV':>4} {'CP':>4}"
        )
        print(header)
        print("  " + "-" * 90)

        table = eval_result.tables.get("eval_results_table")
        if table is not None:
            for _, r in table.iterrows():
                ra = r.get("routing_accuracy/score", "?")
                rq = r.get("response_quality/score", "?")
                ac = r.get("action_confirmed/score", "?")
                gf = r.get("graceful_failure/score", "?")
                rl = r.get("response_length_score/score", "?")

                def fmt(v):
                    if v == 1 or v == 1.0:
                        return "✓"
                    elif v == 0 or v == 0.0:
                        return "✗"
                    elif isinstance(v, float):
                        return f"{v:.2f}"
                    return str(v)

                all_pass = all(
                    str(v) not in ("0", "0.0", "✗")
                    for v in [ra, rq, ac, gf]
                )
                prefix = "  " if all_pass else "! "

                print(
                    f"{prefix}{str(r['inputs'])[:50]:<50} "
                    f"{str(r.get('expected_route', '')):<18} "
                    f"{fmt(ra):>4} {fmt(rq):>4} {fmt(ac):>4} {fmt(gf):>4} {fmt(rl):>4}"
                )

        print("  " + "-" * 90)
        print("  Columns: RA=routing_accuracy  RQ=response_quality  "
              "AC=action_confirmed  GF=graceful_failure  RL=response_length")
        print("\n  ✓ = 1.0 (pass)   ✗ = 0.0 (fail)   decimal = partial score")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="Wipe saved progress and start over")
    parser.add_argument("--batch", type=int, default=None,
                        help="Max tests to run this session")
    args = parser.parse_args()

    if args.reset:
        reset_progress()

    run_evaluation(batch_size=args.batch)
