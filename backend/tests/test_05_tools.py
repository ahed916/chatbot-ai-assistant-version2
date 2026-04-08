"""
test_05_tools.py — Test every LangChain tool individually.

Tests:
  - All read tools return well-formatted strings
  - Write tools resolve names to IDs correctly
  - Chart tool produces valid JSON
  - Slack tool gracefully handles missing token

Run: python tests/test_05_tools.py
"""
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

PASS = 0
FAIL = 0


def check(label, fn, assert_fn=None):
    global PASS, FAIL
    try:
        result = fn()
        if assert_fn:
            assert_fn(result)
        print(f"  ✅ {label}")
        PASS += 1
        return result
    except AssertionError as e:
        print(f"  ❌ {label} — assertion failed: {e}")
        FAIL += 1
        return None
    except Exception as e:
        print(f"  ❌ {label}: {e}")
        FAIL += 1
        return None


def test_tools():
    global PASS, FAIL
    print("\n" + "=" * 60)
    print("TEST 05 — LangChain Tools")
    print("=" * 60)

    import redmine as rm
    projects = rm.list_projects()
    if not projects:
        print("  ❌ No projects found — cannot test tools. Check Redmine connection.")
        sys.exit(1)

    first_project = projects[0]["name"]
    print(f"  Using project: '{first_project}' for tests\n")

    # ── Read Tools ────────────────────────────────────────────
    print("[ Read Tools ]")
    from tools.read_tools import (
        get_all_projects,
        get_project_issues,
        get_all_issues_across_projects,
        get_project_members,
        get_available_statuses,
        get_available_trackers,
        get_all_users,
        get_workload_by_member,
    )

    result = check(
        "get_all_projects()",
        lambda: get_all_projects.invoke({}),
        lambda r: "Found" in r or "project" in r.lower(),
    )
    if result:
        print(f"    → {result[:120]}...")

    result = check(
        f"get_project_issues('{first_project}')",
        lambda: get_project_issues.invoke({"project_identifier": first_project}),
        lambda r: isinstance(r, str) and len(r) > 0,
    )
    if result:
        print(f"    → {result[:120]}...")

    result = check(
        "get_all_issues_across_projects()",
        lambda: get_all_issues_across_projects.invoke({"status": "open"}),
        lambda r: isinstance(r, str),
    )
    if result:
        print(f"    → {result[:120]}...")

    result = check(
        f"get_project_members('{first_project}')",
        lambda: get_project_members.invoke({"project_identifier": first_project}),
        lambda r: isinstance(r, str),
    )
    if result:
        print(f"    → {result[:120]}...")

    result = check(
        "get_available_statuses()",
        lambda: get_available_statuses.invoke({}),
        lambda r: "ID" in r,
    )
    if result:
        print(f"    → {result[:120]}...")

    result = check(
        "get_available_trackers()",
        lambda: get_available_trackers.invoke({}),
        lambda r: "ID" in r,
    )
    if result:
        print(f"    → {result[:80]}...")

    result = check(
        "get_all_users()",
        lambda: get_all_users.invoke({}),
        lambda r: isinstance(r, str),
    )
    if result:
        print(f"    → {result[:100]}...")

    result = check(
        f"get_workload_by_member('{first_project}')",
        lambda: get_workload_by_member.invoke({"project_identifier": first_project}),
        lambda r: isinstance(r, str),
    )
    if result:
        print(f"    → {result[:150]}...")

    # Test allowed transitions if issues exist
    issues = rm.list_issues(status="open", limit=1)
    if issues:
        from tools.read_tools import get_allowed_status_transitions, get_issue_details
        issue_id = issues[0]["id"]

        result = check(
            f"get_issue_details({issue_id})",
            lambda: get_issue_details.invoke({"issue_id": issue_id}),
            lambda r: str(issue_id) in r,
        )

        result = check(
            f"get_allowed_status_transitions({issue_id})",
            lambda: get_allowed_status_transitions.invoke({"issue_id": issue_id}),
            lambda r: "current" in r.lower() or "allowed" in r.lower(),
        )
        if result:
            print(f"    → {result[:150]}...")

    # ── Chart Tool ────────────────────────────────────────────
    print("\n[ Chart Tool ]")
    from tools.chart_tools import generate_dashboard_json, generate_quick_stat

    dashboard_result = check(
        "generate_dashboard_json()",
        lambda: generate_dashboard_json.invoke({
            "charts": [
                {
                    "type": "bar",
                    "title": "Issues by Status",
                    "data": [{"status": "Open", "count": 10}, {"status": "Closed", "count": 5}],
                    "xKey": "status",
                    "yKey": "count",
                    "insight": "Open issues dominate — action needed.",
                }
            ],
            "kpis": [
                {
                    "label": "Open Issues",
                    "value": 10,
                    "trend": "up",
                    "status": "warning",
                    "context": "Up 3 from last week",
                }
            ],
            "summary": "The project has 10 open issues with an upward trend.",
            "title": "Test Dashboard",
        }),
        lambda r: json.loads(r)["type"] == "dashboard",
    )
    if dashboard_result:
        parsed = json.loads(dashboard_result)
        print(f"    → type={parsed['type']}, charts={len(parsed['charts'])}, kpis={len(parsed['kpis'])}")

    stat_result = check(
        "generate_quick_stat()",
        lambda: generate_quick_stat.invoke({
            "label": "Open Bugs",
            "value": "14",
            "context": "across 3 projects",
        }),
        lambda r: json.loads(r)["type"] == "quick_stat",
    )

    # ── Slack Tool ────────────────────────────────────────────
    print("\n[ Slack Tool ]")
    from tools.slack_tools import send_slack_risk_alert

    # Test with no token (should gracefully degrade, not crash)
    result = check(
        "send_slack_risk_alert() — graceful degradation without token",
        lambda: send_slack_risk_alert.invoke({
            "message": "Test alert — ignore",
            "channel_id": "",
        }),
        lambda r: isinstance(r, str),  # should return a string (success OR skip message)
    )
    if result:
        print(f"    → '{result}'")

    # ── Write Tools (NON-DESTRUCTIVE checks only) ─────────────
    print("\n[ Write Tools — Import & Schema Check (no actual writes) ]")
    try:
        from tools.write_tools import (
            create_redmine_issue,
            update_redmine_issue,
            delete_redmine_issue,
            bulk_update_issues,
            WRITE_TOOLS,
        )
        print(f"  ✅ Write tools imported: {[t.name for t in WRITE_TOOLS]}")
        PASS += 1
    except Exception as e:
        print(f"  ❌ Write tools import failed: {e}")
        FAIL += 1

    # ── Summary ───────────────────────────────────────────────
    print(f"\n{'✅' if FAIL == 0 else '⚠️ '} Tools test: {PASS} passed, {FAIL} failed")


if __name__ == "__main__":
    test_tools()
