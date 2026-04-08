
"""
test_08_edge_cases.py — The "jury questions" your supervisor warned you about.

These are prompts the agent was NOT explicitly programmed to handle.
If the system is properly intelligent, it will figure them out.
If it's just hardcoded, it will fail or give nonsense.

Run: python tests/test_08_edge_cases.py
NOTE: This is the SLOWEST test — makes many LLM calls.
      Budget ~5-10 minutes.
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── Edge case prompts ─────────────────────────────────────────────────────────
# Each entry: (prompt, what_we_expect_to_see, category)

EDGE_CASES = [

    # ── Vague / ambiguous language ────────────────────────────
    (
        "How are we doing?",
        "some form of project status or health summary",
        "vague_language",
    ),
    (
        "Things look messy. Can you help?",
        "risk or workload analysis, possibly recommendations",
        "vague_language",
    ),
    (
        "I'm worried about the team",
        "workload analysis, member statistics, or risk assessment",
        "vague_language",
    ),

    # ── Multi-intent (needs reasoning about what the PM really wants) ─────────
    (
        "Who should I talk to about the most critical issues?",
        "assignees of high priority/overdue issues",
        "multi_intent",
    ),
    (
        "Is anyone doing too much work?",
        "workload analysis per member, mention of overloaded individuals",
        "multi_intent",
    ),
    (
        "What should I focus on today?",
        "high priority or overdue issues, possibly recommendations",
        "multi_intent",
    ),

    # ── Implicit project reference ─────────────────────────────
    (
        "How's the backend project looking?",
        "project data or graceful 'project not found' message",
        "implicit_reference",
    ),
    (
        "What's the status of the sprint?",
        "open issues summary or sprint-like grouping",
        "implicit_reference",
    ),

    # ── Unexpected combinations ────────────────────────────────
    (
        "Give me a risk report but make it visual",
        "risk analysis + chart JSON or mention of visualization",
        "unexpected_combo",
    ),
    (
        "Show me who's overloaded and create tasks to help them",
        "workload analysis + mention of task creation (or confirmation of creation)",
        "unexpected_combo",
    ),

    # ── Domain knowledge the agent should infer ────────────────
    (
        "Which issues are blocking the release?",
        "urgent/high priority issues or blockers identified",
        "domain_knowledge",
    ),
    (
        "Are we going to miss the deadline?",
        "overdue or at-risk issues, deadline analysis",
        "domain_knowledge",
    ),
    (
        "What's the velocity of the team this week?",
        "closed issues count or activity metric, or honest explanation",
        "domain_knowledge",
    ),

    # ── Completely novel — agent must reason from scratch ──────
    (
        "If I had to cut the team in half, which issues would suffer most?",
        "analysis of issue ownership/priority without clear single assignment",
        "novel_reasoning",
    ),
    (
        "What would happen if we closed all open bugs right now?",
        "some kind of impact analysis or explanation",
        "novel_reasoning",
    ),
    (
        "Tell me something I don't know about my project",
        "any non-obvious insight from the data",
        "novel_reasoning",
    ),

    # ── Error recovery ─────────────────────────────────────────
    (
        "Update issue #999999",
        "graceful 'issue not found' message, no crash",
        "error_recovery",
    ),
    (
        "Show me the project called XYZ_DOES_NOT_EXIST_AT_ALL",
        "graceful 'not found' message",
        "error_recovery",
    ),
    (
        "asdfghjkl",
        "polite clarification or best-effort interpretation",
        "error_recovery",
    ),
]


def test_edge_cases():
    print("\n" + "=" * 60)
    print("TEST 08 — Edge Cases (Jury Questions)")
    print("=" * 60)
    print(f"  {len(EDGE_CASES)} prompts to test")
    print("  ⏳ Budget ~5-10 minutes for this test suite\n")

    from supervisor import run_supervisor

    results = []
    categories = {}

    for i, (prompt, expectation, category) in enumerate(EDGE_CASES, 1):
        categories.setdefault(category, {"pass": 0, "review": 0})
        print(f"  [{i:02d}/{len(EDGE_CASES)}] [{category}]")
        print(f"  Prompt: '{prompt}'")
        print(f"  Expect: {expectation}")

        t = time.perf_counter()
        try:
            response = run_supervisor(prompt, [])
            elapsed_ms = (time.perf_counter() - t) * 1000

            # Basic quality checks
            is_empty = not response or len(response.strip()) < 10
            is_error_crash = "Traceback" in response or "Exception" in response
            is_refusal = response.strip().lower().startswith("i can't") or "cannot" in response.lower()[:50]
            is_reasonable = len(response) > 20 and not is_error_crash

            if is_empty:
                print(f"  ❌ EMPTY response — agent produced nothing")
                categories[category]["review"] += 1
            elif is_error_crash:
                print(f"  ❌ CRASHED — response contains traceback")
                categories[category]["review"] += 1
            elif is_refusal and category != "error_recovery":
                print(f"  ⚠️  REFUSED — agent said it can't help (may be too restrictive)")
                categories[category]["review"] += 1
            elif is_reasonable:
                print(f"  ✅ Got response ({elapsed_ms:.0f}ms, {len(response)} chars)")
                categories[category]["pass"] += 1
            else:
                print(f"  ⚠️  REVIEW NEEDED — short or unusual response")
                categories[category]["review"] += 1

            print(f"  Response: '{response[:200]}'")
            results.append({
                "prompt": prompt,
                "category": category,
                "elapsed_ms": elapsed_ms,
                "response_len": len(response),
                "response_preview": response[:200],
                "quality": "pass" if is_reasonable and not is_error_crash else "review",
            })

        except Exception as e:
            elapsed_ms = (time.perf_counter() - t) * 1000
            print(f"  ❌ EXCEPTION after {elapsed_ms:.0f}ms: {e}")
            categories[category]["review"] += 1
            results.append({
                "prompt": prompt,
                "category": category,
                "elapsed_ms": elapsed_ms,
                "response_len": 0,
                "response_preview": f"ERROR: {e}",
                "quality": "fail",
            })

        print()

    # ── Summary ───────────────────────────────────────────────
    print("=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)

    total_pass = sum(r["quality"] == "pass" for r in results)
    total_review = len(results) - total_pass
    print(f"\n  Overall: {total_pass}/{len(results)} passed ({100*total_pass//len(results)}%)")

    print("\n  By category:")
    for cat, counts in categories.items():
        total_cat = counts["pass"] + counts["review"]
        print(f"  {cat:<25} {counts['pass']}/{total_cat} passed")

    if total_review > 0:
        print(f"\n  ⚠️  {total_review} response(s) need review:")
        for r in results:
            if r["quality"] != "pass":
                print(f"  → '{r['prompt'][:50]}...'")
                print(f"     Response: '{r['response_preview'][:100]}'")

    avg_ms = sum(r["elapsed_ms"] for r in results) / len(results)
    print(f"\n  Average response time: {avg_ms:.0f}ms")
    print(f"  Total test time: {sum(r['elapsed_ms'] for r in results)/1000:.1f}s")

    print("\n✅ Edge case test complete.")
    print("  Use these results to refine your prompts/v1/*.md files.")


if __name__ == "__main__":
    test_edge_cases()
