"""
test_04_llm.py — Test LLM connection and basic inference.

Tests:
  - LLM can be instantiated from config
  - Simple invoke works and returns a response
  - JSON-structured output works (needed for routing)
  - Response time is acceptable

Run: python tests/test_04_llm.py
"""
import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_llm():
    print("\n" + "=" * 60)
    print("TEST 04 — LLM Connection & Inference")
    print("=" * 60)

    from config import LLM_MODEL, LLM_BASE_URL
    print(f"\n  Model:    {LLM_MODEL}")
    print(f"  Endpoint: {LLM_BASE_URL}")

    # ── Basic import ──────────────────────────────────────────
    print("\n[ Import ]")
    try:
        from llm import get_llm
        llm = get_llm()
        print("  ✅ LLM instantiated from config")
    except Exception as e:
        print(f"  ❌ LLM import/instantiation failed: {e}")
        sys.exit(1)

    # ── Simple invoke ─────────────────────────────────────────
    print("\n[ Simple Invoke ]")
    from langchain_core.messages import HumanMessage

    t = time.perf_counter()
    try:
        response = llm.invoke([HumanMessage(content="Reply with exactly: OK")])
        elapsed_ms = (time.perf_counter() - t) * 1000
        content = response.content.strip()
        print(f"  ✅ Got response in {elapsed_ms:.0f}ms")
        print(f"  → Response: '{content}'")
        if elapsed_ms > 30000:
            print("  ⚠️  Response took >30s — free model may be rate-limited or slow")
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t) * 1000
        print(f"  ❌ LLM invoke failed after {elapsed_ms:.0f}ms: {e}")
        print("  Check: LLM_API_KEY is valid, LLM_MODEL name is correct on OpenRouter")
        sys.exit(1)

    # ── JSON structured output ────────────────────────────────
    print("\n[ JSON Structured Output (needed for routing) ]")
    import json
    from langchain_core.messages import SystemMessage

    routing_test_prompt = """You are a routing engine. Respond ONLY with valid JSON, no markdown:
{"route": "direct", "agents": [], "reason": "test"}"""

    t = time.perf_counter()
    try:
        resp = llm.invoke([
            SystemMessage(content=routing_test_prompt),
            HumanMessage(content="What projects do we have?"),
        ])
        elapsed_ms = (time.perf_counter() - t) * 1000
        raw = resp.content.strip()

        # Strip markdown fences if model adds them
        if "```" in raw:
            raw = raw.split("```")[1].split("```")[0]
            if raw.startswith("json"):
                raw = raw[4:]

        parsed = json.loads(raw.strip())
        assert "route" in parsed, f"'route' key missing from: {parsed}"
        print(f"  ✅ JSON output in {elapsed_ms:.0f}ms")
        print(f"  → Parsed: {parsed}")
    except json.JSONDecodeError as e:
        print(f"  ❌ Model didn't return valid JSON: {e}")
        print(f"  → Raw response: '{resp.content[:200]}'")
        print("  ⚠️  This may cause routing issues — the supervisor has a fallback")
    except Exception as e:
        print(f"  ❌ JSON structured output test failed: {e}")

    # ── Reasoning test ────────────────────────────────────────
    print("\n[ Reasoning Quality (spot check) ]")
    t = time.perf_counter()
    try:
        resp = llm.invoke([HumanMessage(content=(
            "A project has 5 overdue issues, 3 unassigned, and 2 members with 10+ tasks each. "
            "In one sentence, what is the most urgent risk?"
        ))])
        elapsed_ms = (time.perf_counter() - t) * 1000
        print(f"  ✅ Reasoning response in {elapsed_ms:.0f}ms")
        print(f"  → '{resp.content.strip()[:200]}'")
    except Exception as e:
        print(f"  ⚠️  Reasoning test failed: {e}")

    print("\n✅ LLM test PASSED")


if __name__ == "__main__":
    test_llm()
