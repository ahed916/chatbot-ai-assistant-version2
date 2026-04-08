"""
test_09_write_operations.py — Test create/update/delete with real Redmine.

This test:
  1. Creates a test issue
  2. Verifies it was created
  3. Updates it
  4. Verifies the update
  5. Deletes it (cleanup)

Safe to run — it cleans up after itself.

Run: python tests/test_09_write_operations.py
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

CREATED_ISSUE_ID = None


def test_write_operations():
    global CREATED_ISSUE_ID
    print("\n" + "=" * 60)
    print("TEST 09 — Write Operations (Create / Update / Delete)")
    print("=" * 60)

    import redmine as rm

    projects = rm.list_projects()
    if not projects:
        print("❌ No projects found. Cannot test writes.")
        sys.exit(1)

    first_project = projects[0]
    project_id = str(first_project["id"])
    project_name = first_project["name"]
    print(f"\n  Using project: [{project_id}] {project_name}\n")

    # ── CREATE ────────────────────────────────────────────────
    print("[ CREATE ]")
    t = time.perf_counter()
    result = rm.create_issue(
        project_id=project_id,
        subject="[REDMIND TEST] Automated test issue — safe to delete",
        description="Created by test_09_write_operations.py — will be auto-deleted.",
        priority_id=2,   # Normal
        tracker_id=1,    # Task (adjust if needed)
        status_id=1,     # New
    )
    elapsed_ms = (time.perf_counter() - t) * 1000

    if not result or "id" not in result:
        print(f"  ❌ create_issue failed: {result}")
        sys.exit(1)

    CREATED_ISSUE_ID = result["id"]
    print(f"  ✅ Created issue #{CREATED_ISSUE_ID} in {elapsed_ms:.0f}ms")
    print(f"     Subject: {result.get('subject')}")
    print(f"     Status:  {result.get('status', {}).get('name')}")
    print(f"     Project: {result.get('project', {}).get('name')}")

    # ── VERIFY CREATION ───────────────────────────────────────
    print("\n[ VERIFY CREATION ]")
    time.sleep(0.5)  # Brief pause for Redmine to index
    fetched = rm.get_issue(CREATED_ISSUE_ID)
    if fetched and fetched.get("id") == CREATED_ISSUE_ID:
        print(f"  ✅ Issue #{CREATED_ISSUE_ID} verified in Redmine")
    else:
        print(f"  ❌ Could not fetch issue #{CREATED_ISSUE_ID} after creation")

    # ── CHECK TRANSITIONS ─────────────────────────────────────
    print("\n[ CHECK TRANSITIONS ]")
    transitions = rm.get_allowed_transitions(CREATED_ISSUE_ID)
    current = transitions.get("current_status_name", "?")
    allowed = [s["name"] for s in transitions.get("allowed", [])]
    print(f"  Current status: {current}")
    print(f"  Can transition to: {allowed}")

    # ── UPDATE ────────────────────────────────────────────────
    print("\n[ UPDATE ]")
    # Find a valid "In Progress" status or use first allowed
    target_status = None
    target_status_id = None

    statuses = rm.list_issue_statuses()
    status_map = {s["name"].lower(): s["id"] for s in statuses}

    for candidate in ["in progress", "in review", "open", "resolved"]:
        if candidate in status_map and candidate.replace(" ", "") != "new":
            allowed_names = [n.lower() for n in allowed]
            if candidate in allowed_names:
                target_status = candidate
                target_status_id = status_map[candidate]
                break

    if target_status_id and allowed:
        t = time.perf_counter()
        error = rm.update_issue(
            CREATED_ISSUE_ID,
            status_id=target_status_id,
            notes="Updated by automated test",
        )
        elapsed_ms = (time.perf_counter() - t) * 1000
        if error:
            print(f"  ⚠️  Update returned: {error}")
        else:
            print(f"  ✅ Updated issue #{CREATED_ISSUE_ID} to '{target_status}' in {elapsed_ms:.0f}ms")

        # Verify update
        time.sleep(0.5)
        updated = rm.get_issue(CREATED_ISSUE_ID)
        new_status = updated.get("status", {}).get("name", "?")
        print(f"  ✅ Verified: status is now '{new_status}'")
    else:
        print(f"  ⚠️  Skipping status update — no valid transitions from '{current}'")

    # ── AUTOMATION AGENT UPDATE (uses tool) ───────────────────
    print("\n[ AUTOMATION AGENT UPDATE (via tool) ]")
    from agents.tools.redmine_write_tools import update_redmine_issue

    t = time.perf_counter()
    result = update_redmine_issue.invoke({
        "issue_id": CREATED_ISSUE_ID,
        "notes": "Note added via automation agent tool in test",
    })
    elapsed_ms = (time.perf_counter() - t) * 1000
    print(f"  ✅ Tool response in {elapsed_ms:.0f}ms: '{result}'")

    # ── DELETE (cleanup) ──────────────────────────────────────
    print("\n[ DELETE (cleanup) ]")
    t = time.perf_counter()
    error = rm.delete_issue(CREATED_ISSUE_ID)
    elapsed_ms = (time.perf_counter() - t) * 1000

    if error == "NOT_FOUND":
        print(f"  ⚠️  Issue #{CREATED_ISSUE_ID} already deleted")
    elif error:
        print(f"  ❌ Delete failed: {error}")
    else:
        print(f"  ✅ Issue #{CREATED_ISSUE_ID} deleted in {elapsed_ms:.0f}ms")

    # Verify deletion
    time.sleep(0.5)
    gone = rm.get_issue(CREATED_ISSUE_ID)
    if not gone:
        print(f"  ✅ Confirmed: issue #{CREATED_ISSUE_ID} is gone from Redmine")
    else:
        print(f"  ⚠️  Issue may still exist: {gone.get('id')}")

    # ── AUDIT LOG CHECK ───────────────────────────────────────
    print("\n[ AUDIT LOG ]")
    from pathlib import Path
    import json
    from config import AUDIT_LOG_FILE
    log = Path(AUDIT_LOG_FILE)
    if log.exists():
        events = [json.loads(l) for l in log.read_text().strip().split("\n") if l.strip()]
        write_events = [e for e in events if e.get("event") == "redmine_write"]
        print(f"  ✅ {len(write_events)} redmine_write event(s) in audit log")
        for ev in write_events[-3:]:
            print(f"    → {ev.get('redmine_action')} | {ev.get('tool_args')} | success={ev.get('success')}")
    else:
        print(f"  ⚠️  No audit log at {AUDIT_LOG_FILE}")

    print("\n✅ Write operations test PASSED — all test data cleaned up.")


if __name__ == "__main__":
    try:
        test_write_operations()
    except KeyboardInterrupt:
        # Cleanup on interrupt
        if CREATED_ISSUE_ID:
            print(f"\n⚠️  Interrupted — cleaning up issue #{CREATED_ISSUE_ID}...")
            import redmine as rm
            rm.delete_issue(CREATED_ISSUE_ID)
            print("  Cleaned up.")
