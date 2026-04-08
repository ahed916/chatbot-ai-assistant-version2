"""
run_all_tests.py — Master test runner.

Runs all tests in the correct order and produces a final report.

Usage:
  python tests/run_all_tests.py              # run all tests
  python tests/run_all_tests.py --fast       # skip slow LLM tests (01-05 only)
  python tests/run_all_tests.py --no-write   # skip write operations test

Run from your project root directory.
"""
import sys
import os
import time
import subprocess
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

TESTS = [
    ("test_01_config.py", "Config & Environment", False),  # (file, label, slow)
    ("test_02_redmine.py", "Redmine Connection", False),
    ("test_03_redis_cache.py", "Redis Cache", False),
    ("test_04_llm.py", "LLM Connection", True),   # True = slow (LLM call)
    ("test_05_tools.py", "LangChain Tools", False),
    ("test_06_agents.py", "Individual Agents", True),
    ("test_07_supervisor.py", "Supervisor & Routing", True),
    ("test_08_edge_cases.py", "Edge Cases (Jury Questions)", True),
    ("test_09_write_operations.py", "Write Operations (Create/Delete)", False),
]


def run_test(test_file: str) -> tuple[bool, float, str]:
    """Run a single test file and return (success, elapsed_sec, output)."""
    test_path = os.path.join(os.path.dirname(__file__), test_file)
    t = time.perf_counter()
    try:
        result = subprocess.run(
            [sys.executable, test_path],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute max per test
        )
        elapsed = time.perf_counter() - t
        output = result.stdout + result.stderr
        success = result.returncode == 0
        return success, elapsed, output
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - t
        return False, elapsed, "TIMEOUT after 10 minutes"
    except Exception as e:
        elapsed = time.perf_counter() - t
        return False, elapsed, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true", help="Skip slow LLM tests")
    parser.add_argument("--no-write", action="store_true", help="Skip write operations")
    args = parser.parse_args()

    print("\n" + "█" * 60)
    print("  REDMIND — Full Test Suite")
    print("█" * 60)

    if args.fast:
        print("  Mode: FAST (skipping LLM-heavy tests)")
    if args.no_write:
        print("  Mode: NO WRITE (skipping write operations)")
    print()

    suite_start = time.perf_counter()
    results = []

    for test_file, label, is_slow in TESTS:
        # Skip conditions
        if args.fast and is_slow:
            print(f"  ⏭  SKIPPED [{label}] (--fast mode)")
            results.append((label, "skipped", 0, ""))
            continue
        if args.no_write and "Write" in label:
            print(f"  ⏭  SKIPPED [{label}] (--no-write mode)")
            results.append((label, "skipped", 0, ""))
            continue

        print(f"\n{'─'*60}")
        print(f"  ▶ Running: {label}")
        print(f"{'─'*60}")

        success, elapsed, output = run_test(test_file)

        # Print output
        print(output)

        status = "✅ PASSED" if success else "❌ FAILED"
        print(f"\n  {status} — {label} ({elapsed:.1f}s)")
        results.append((label, "pass" if success else "fail", elapsed, output))

        # Stop on critical failures (config/redis/redmine)
        if not success and label in ("Config & Environment", "Redmine Connection", "Redis Cache"):
            print(f"\n  🛑 Critical test failed — stopping suite.")
            print(f"     Fix '{label}' before running other tests.")
            break

    # ── Final Report ──────────────────────────────────────────
    total_elapsed = time.perf_counter() - suite_start
    print("\n" + "█" * 60)
    print("  FINAL REPORT")
    print("█" * 60)

    passed = sum(1 for _, s, _, _ in results if s == "pass")
    failed = sum(1 for _, s, _, _ in results if s == "fail")
    skipped = sum(1 for _, s, _, _ in results if s == "skipped")
    total = len([r for r in results if r[1] != "skipped"])

    for label, status, elapsed, _ in results:
        icon = {"pass": "✅", "fail": "❌", "skipped": "⏭ "}.get(status, "?")
        time_str = f"({elapsed:.1f}s)" if elapsed > 0 else ""
        print(f"  {icon} {label} {time_str}")

    print()
    print(f"  Passed:  {passed}/{total}")
    print(f"  Failed:  {failed}/{total}")
    print(f"  Skipped: {skipped}")
    print(f"  Total time: {total_elapsed:.1f}s")

    if failed == 0:
        print("\n  🎉 ALL TESTS PASSED — RedMind is ready!")
    else:
        print(f"\n  ⚠️  {failed} test(s) failed — check output above.")
        print("  Tip: run individual test files for detailed output.")

    print("█" * 60 + "\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
