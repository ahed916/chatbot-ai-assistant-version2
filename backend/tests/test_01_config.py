"""
test_01_config.py — Verify .env loads correctly and all required vars are present.

Run: python tests/test_01_config.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_config():
    print("\n" + "=" * 60)
    print("TEST 01 — Config & Environment Variables")
    print("=" * 60)

    errors = []
    warnings = []

    try:
        import config as cfg
        print("✅ config.py imported successfully")
    except Exception as e:
        print(f"❌ config.py import failed: {e}")
        sys.exit(1)

    # Required vars
    checks = [
        ("REDMINE_URL", cfg.REDMINE_URL, "https://"),
        ("REDMINE_API_KEY", cfg.REDMINE_API_KEY, None),
        ("LLM_API_KEY", cfg.LLM_API_KEY, None),
        ("LLM_MODEL", cfg.LLM_MODEL, None),
        ("REDIS_HOST", cfg.REDIS_HOST, None),
    ]

    for name, value, must_start_with in checks:
        if not value or value.startswith("your_") or value.startswith("https://your"):
            errors.append(f"  ❌ {name} is not set or still has placeholder value: '{value}'")
        elif must_start_with and not value.startswith(must_start_with):
            warnings.append(f"  ⚠️  {name} = '{value}' — expected to start with '{must_start_with}'")
        else:
            print(f"  ✅ {name} = '{value[:30]}...' " if len(value) > 30 else f"  ✅ {name} = '{value}'")

    # Optional but warn if missing
    optional = [
        ("SLACK_BOT_TOKEN", cfg.SLACK_BOT_TOKEN),
        ("SLACK_CHANNEL_ID", cfg.SLACK_CHANNEL_ID),
    ]
    for name, value in optional:
        if not value:
            warnings.append(f"  ⚠️  {name} not set — Slack notifications will be skipped")
        else:
            print(f"  ✅ {name} = '{value[:20]}...'")

    # Check prompt files exist
    from pathlib import Path
    for prompt_name in ["supervisor", "dashboard_agent", "risk_agent", "automation_agent"]:
        path = cfg.PROMPTS_DIR / f"{prompt_name}.md"
        if path.exists():
            content = path.read_text()
            print(f"  ✅ prompts/v1/{prompt_name}.md ({len(content)} chars)")
        else:
            errors.append(f"  ❌ prompts/v1/{prompt_name}.md NOT FOUND at {path}")

    # Check logs directory can be created
    from pathlib import Path
    log_dir = Path(cfg.AUDIT_LOG_FILE).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    print(f"  ✅ Logs directory: {log_dir.resolve()}")

    # Summary
    print()
    for w in warnings:
        print(w)
    if errors:
        print()
        for e in errors:
            print(e)
        print("\n❌ Config test FAILED — fix the errors above before proceeding.")
        sys.exit(1)
    else:
        print("\n✅ Config test PASSED")


if __name__ == "__main__":
    test_config()
