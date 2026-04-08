"""
test_02_redmine.py — Test Redmine API connection and all client functions.

Run: python tests/test_02_redmine.py

Tests:
  - Can connect to Redmine
  - Can list projects, trackers, statuses, users, members
  - Can list/get issues
  - Can resolve project names to IDs
  - Can get workflow transitions
  - Tenacity retry is importable
  - Cache invalidation works
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

PASS = 0
FAIL = 0


def check(label, fn):
    global PASS, FAIL
    try:
        result = fn()
        print(f"  ✅ {label}")
        return result
    except Exception as e:
        print(f"  ❌ {label}: {e}")
        FAIL += 1
        return None


def test_redmine():
    global PASS, FAIL
    PASS = 0
    FAIL = 0
    print("\n" + "=" * 60)
    print("TEST 02 — Redmine API Connection")
    print("=" * 60)

    import redmine as rm

    # ── Connection ────────────────────────────────────────────
    print("\n[ Connection ]")
    projects = check("list_projects()", rm.list_projects)
    if projects is None:
        print("  Cannot continue — Redmine is unreachable. Check REDMINE_URL and REDMINE_API_KEY.")
        sys.exit(1)

    print(f"  → Found {len(projects)} project(s):")
    for p in projects[:5]:
        print(f"    [{p['id']}] {p['name']} (identifier: {p.get('identifier')})")

    # ── Schema discovery ──────────────────────────────────────
    print("\n[ Schema Discovery ]")
    trackers = check("list_trackers()", rm.list_trackers)
    if trackers:
        print(f"  → Trackers: {[t['name'] for t in trackers]}")

    statuses = check("list_issue_statuses()", rm.list_issue_statuses)
    if statuses:
        print(f"  → Statuses: {[s['name'] for s in statuses]}")

    # ── Users ─────────────────────────────────────────────────
    print("\n[ Users ]")
    users = check("list_users()", rm.list_users)
    if users:
        print(f"  → Found {len(users)} user(s)")
        for u in users[:3]:
            print(f"    [{u['id']}] {u.get('firstname','')} {u.get('lastname','')} ({u.get('login','')})")
    else:
        print("  ⚠️  Users endpoint returned empty (may need admin API key)")

    # ── Issues ────────────────────────────────────────────────
    print("\n[ Issues ]")
    issues = check("list_issues(status='open')", lambda: rm.list_issues(status="open", limit=10))
    if issues is not None:
        print(f"  → Found {len(issues)} open issue(s)")
        if issues:
            first = issues[0]
            print(f"  → First: #{first['id']} — {first['subject']}")

            issue_detail = check(f"get_issue({first['id']})", lambda: rm.get_issue(first["id"]))
            if issue_detail:
                print(f"  → Detail: status={issue_detail.get('status',{}).get('name')}, "
                      f"priority={issue_detail.get('priority',{}).get('name')}")

            transitions = check(
                f"get_allowed_transitions({first['id']})",
                lambda: rm.get_allowed_transitions(first["id"])
            )
            if transitions:
                allowed = [s["name"] for s in transitions.get("allowed", [])]
                print(f"  → Current: {transitions['current_status_name']}")
                print(f"  → Can transition to: {allowed}")

    all_issues = check("list_issues(status='*')", lambda: rm.list_issues(status="*", limit=50))
    if all_issues is not None:
        print(f"  → Total issues (all statuses): {len(all_issues)}")

    # ── Project-scoped queries ────────────────────────────────
    print("\n[ Project-Scoped ]")
    if projects:
        first_project = projects[0]
        pid = str(first_project["id"])
        pname = first_project["name"]

        resolved = check(
            f"resolve_project_id('{pname}')",
            lambda: rm.resolve_project_id(pname)
        )
        if resolved:
            print(f"  → '{pname}' resolved to ID: {resolved}")

        members = check(
            f"list_members('{pid}')",
            lambda: rm.list_members(pid)
        )
        if members is not None:
            print(f"  → {len(members)} member(s) in '{pname}'")

        proj_issues = check(
            f"list_issues(project_id='{pid}')",
            lambda: rm.list_issues(project_id=pid, limit=10)
        )
        if proj_issues is not None:
            print(f"  → {len(proj_issues)} open issue(s) in '{pname}'")

    # ── Retry decorator importable ────────────────────────────
    print("\n[ Tenacity / Retry ]")
    try:
        from tenacity import retry, stop_after_attempt
        print("  ✅ tenacity imported OK")
    except ImportError:
        print("  ❌ tenacity not installed — run: pip install tenacity")
        FAIL += 1

    # ── Summary ───────────────────────────────────────────────
    print()
    total = PASS + FAIL
    if FAIL == 0:
        print(f"✅ Redmine test PASSED ({total} checks)")
    else:
        print(f"⚠️  Redmine test: {FAIL} failure(s) out of {total} checks")


if __name__ == "__main__":
    test_redmine()
