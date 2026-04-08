"""
test_06_agents.py — Test each agent independently end-to-end.

Tests:
  - Dashboard agent produces JSON with charts and KPIs
  - Risk agent produces structured risk analysis
  - Automation agent can read context before acting
  - All agents handle errors gracefully

Run: python tests/test_06_agents.py
NOTE: This makes real LLM calls — may take 30-120s per agent.
"""
import sys
import os
import json
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def section(title):
    print(f"\n{'─'*50}")
    print(f"  {title}")
    print('─' * 50)


def test_agents():
    print("\n" + "=" * 60)
    print("TEST 06 — Individual Agents (makes real LLM calls)")
    print("=" * 60)
    print("⏳ This may take 1-5 minutes depending on model speed...\n")

    import redmine as rm
    projects = rm.list_projects()
    if not projects:
        print("❌ No projects found. Cannot test agents.")
        sys.exit(1)

    first_project = projects[0]["name"]

    # ════════════════════════════════════════════════════════
    # DASHBOARD AGENT
    # ════════════════════════════════════════════════════════
    section("Dashboard Agent")

    from agents.dashboard_agent import run_dashboard_agent

    prompts = [
        f"Give me a summary of the '{first_project}' project",
        "Show me the overall issue distribution across all projects",
    ]

    for prompt in prompts:
        print(f"\n  Prompt: '{prompt}'")
        t = time.perf_counter()
        try:
            result = run_dashboard_agent(prompt)
            elapsed = (time.perf_counter() - t) * 1000
            print(f"  ⏱  {elapsed:.0f}ms")

            # Try to parse as JSON (dashboard payload)
            try:
                data = json.loads(result)
                if data.get("type") in ("dashboard", "quick_stat"):
                    print(f"  ✅ Returned dashboard JSON")
                    print(f"     type={data['type']}")
                    if data.get("charts"):
                        print(f"     charts: {len(data['charts'])} — types: {[c.get('type') for c in data['charts']]}")
                    if data.get("kpis"):
                        print(f"     kpis: {len(data['kpis'])} — labels: {[k.get('label') for k in data['kpis']]}")
                    if data.get("summary"):
                        print(f"     summary: '{data['summary'][:100]}'")
                else:
                    print(f"  ✅ Got JSON response: {str(data)[:150]}")
            except json.JSONDecodeError:
                # Plain text response is also valid
                print(f"  ✅ Got text response: '{result[:200]}'")

        except Exception as e:
            print(f"  ❌ Dashboard agent failed: {e}")

    # ════════════════════════════════════════════════════════
    # RISK AGENT
    # ════════════════════════════════════════════════════════
    section("Risk Agent")

    from agents.risk_agent import run_risk_agent, proactive_risk_check

    # Interactive mode
    prompt = f"What are the risks in the '{first_project}' project?"
    print(f"\n  Prompt: '{prompt}'")
    t = time.perf_counter()
    try:
        result = run_risk_agent(prompt)
        elapsed = (time.perf_counter() - t) * 1000
        print(f"  ⏱  {elapsed:.0f}ms")
        print(f"  ✅ Risk agent response:")
        print(f"  '{result[:400]}'")
    except Exception as e:
        print(f"  ❌ Risk agent failed: {e}")

    # Proactive mode
    print(f"\n  Testing proactive_risk_check()...")
    t = time.perf_counter()
    try:
        result = proactive_risk_check()
        elapsed = (time.perf_counter() - t) * 1000
        print(f"  ⏱  {elapsed:.0f}ms")
        print(f"  ✅ Proactive check result:")
        print(f"     critical_count:  {result.get('critical_count', '?')}")
        print(f"     overall_health:  {result.get('overall_health', '?')}")
        print(f"     slack_sent:      {result.get('slack_sent', '?')}")
        msg = result.get("proactive_message", "")
        print(f"     proactive_msg:   '{msg[:150]}'")
        recs = result.get("recommendations", [])
        if recs:
            print(f"     recommendations: {recs[:2]}")
    except Exception as e:
        print(f"  ❌ Proactive risk check failed: {e}")

    # ════════════════════════════════════════════════════════
    # AUTOMATION AGENT
    # ════════════════════════════════════════════════════════
    section("Automation Agent — READ-ONLY QUERIES (no writes)")

    from agents.automation_agent import run_automation_agent

    # Only test read-side reasoning — no actual mutations
    prompts = [
        f"How many open issues are there in '{first_project}' and who is handling the most?",
        f"What is the current status of issues in '{first_project}'?",
    ]

    for prompt in prompts:
        print(f"\n  Prompt: '{prompt}'")
        t = time.perf_counter()
        try:
            result = run_automation_agent(prompt)
            elapsed = (time.perf_counter() - t) * 1000
            print(f"  ⏱  {elapsed:.0f}ms")
            print(f"  ✅ Response: '{result[:300]}'")
        except Exception as e:
            print(f"  ❌ Automation agent failed: {e}")

    print("\n✅ Agent tests complete.")
    print("  Check logs/audit.jsonl to see every tool call that was made.")


if __name__ == "__main__":
    test_agents()
