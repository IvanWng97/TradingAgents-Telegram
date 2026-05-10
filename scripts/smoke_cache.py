"""Smoke tests for the same-day result cache (`tg_bot.cache`).

Each scenario uses a fresh temporary `TG_BOT_DATA_DIR` so disk state is
isolated. Covers the storage layer in isolation; integration with the
full /watch and digest fan-out is exercised by the manual test in
docs/MANUAL_INSTALL.md (no-LLM stub) and the existing `smoke_digest.py`
fan-out tests (which see the cache via `_analyze_one_for_digest`).

Run with: .venv/bin/python3 scripts/smoke_cache.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


# Make src/ importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _fresh_data_dir() -> Path:
    """New tempdir + repoint TG_BOT_DATA_DIR. The cache module reads the
    env var on every call (no module-level caching), so repeated swaps
    work without reimporting."""
    d = Path(tempfile.mkdtemp(prefix="tg_bot_cache_smoke_"))
    os.environ["TG_BOT_DATA_DIR"] = str(d)
    return d


# Sample (final_state, signal, telegraph_url) — minimal structure that
# matches what `_run_analysis_for_ticker` writes via `result_cache.store`.
SAMPLE_STATE = {
    "final_trade_decision": "Final Trading Decision: BUY. Reasoning ...",
    "trader_investment_plan": "Buy 100 shares ...",
}


async def test_lookup_miss_returns_none() -> None:
    _fresh_data_dir()
    from tg_bot import cache

    assert cache.lookup("openai", "gpt-4o", "o4-mini", "NVDA", "2026-05-09") is None


async def test_store_then_lookup_roundtrip() -> None:
    _fresh_data_dir()
    from tg_bot import cache

    cache.store(
        "openai",
        "gpt-4o",
        "o4-mini",
        "NVDA",
        "2026-05-09",
        SAMPLE_STATE,
        "BUY",
        "https://telegra.ph/NVDA-05-09",
    )
    got = cache.lookup("openai", "gpt-4o", "o4-mini", "NVDA", "2026-05-09")
    assert got is not None
    assert got["signal"] == "BUY"
    assert got["telegraph_url"] == "https://telegra.ph/NVDA-05-09"
    assert got["final_state"]["final_trade_decision"].startswith("Final Trading")


async def test_different_config_isolates() -> None:
    """Different (provider, deep, quick) combos must NOT collide — the
    LLM output for two providers on the same ticker is genuinely different."""
    _fresh_data_dir()
    from tg_bot import cache

    cache.store(
        "openai",
        "gpt-4o",
        "o4-mini",
        "NVDA",
        "2026-05-09",
        SAMPLE_STATE,
        "BUY",
        "https://t.ly/openai",
    )
    cache.store(
        "deepseek",
        "deepseek-v4",
        "deepseek-chat",
        "NVDA",
        "2026-05-09",
        SAMPLE_STATE,
        "SELL",
        "https://t.ly/deepseek",
    )
    a = cache.lookup("openai", "gpt-4o", "o4-mini", "NVDA", "2026-05-09")
    b = cache.lookup("deepseek", "deepseek-v4", "deepseek-chat", "NVDA", "2026-05-09")
    assert a["signal"] == "BUY" and a["telegraph_url"].endswith("openai")
    assert b["signal"] == "SELL" and b["telegraph_url"].endswith("deepseek")


async def test_same_config_shares_across_users() -> None:
    """The cache key has no user_id — two users on the same config get
    a shared hit on the second tap. This is the core correctness guarantee."""
    _fresh_data_dir()
    from tg_bot import cache

    # Simulate user A's run.
    cache.store(
        "deepseek",
        "deepseek-v4",
        "deepseek-chat",
        "NVDA",
        "2026-05-09",
        SAMPLE_STATE,
        "BUY",
        "https://t.ly/x",
    )
    # User B (different user_id, same /config) — no second store call.
    # Their lookup should hit user A's cache file.
    got = cache.lookup("deepseek", "deepseek-v4", "deepseek-chat", "NVDA", "2026-05-09")
    assert got is not None and got["signal"] == "BUY"


async def test_invalidate_removes_entry() -> None:
    _fresh_data_dir()
    from tg_bot import cache

    cache.store(
        "openai",
        "gpt-4o",
        "o4-mini",
        "NVDA",
        "2026-05-09",
        SAMPLE_STATE,
        "BUY",
        None,
    )
    assert cache.lookup("openai", "gpt-4o", "o4-mini", "NVDA", "2026-05-09") is not None
    assert cache.invalidate("openai", "gpt-4o", "o4-mini", "NVDA", "2026-05-09") is True
    assert cache.lookup("openai", "gpt-4o", "o4-mini", "NVDA", "2026-05-09") is None
    # Second invalidate on already-gone entry: returns False, no error.
    assert (
        cache.invalidate("openai", "gpt-4o", "o4-mini", "NVDA", "2026-05-09") is False
    )


async def test_lazy_eviction_drops_old_dates() -> None:
    """When a fresh date lands for the same (config, ticker), older
    date files in that directory are swept. Bounds disk growth."""
    _fresh_data_dir()
    from tg_bot import cache

    # Yesterday's run.
    cache.store(
        "openai",
        "gpt-4o",
        "o4-mini",
        "NVDA",
        "2026-05-08",
        SAMPLE_STATE,
        "BUY",
        None,
    )
    yesterday_path = (
        Path(os.environ["TG_BOT_DATA_DIR"])
        / "result_cache"
        / "openai__gpt-4o__o4-mini"
        / "NVDA"
        / "2026-05-08.json"
    )
    assert yesterday_path.is_file()

    # Today's run lands → yesterday's gets swept.
    cache.store(
        "openai",
        "gpt-4o",
        "o4-mini",
        "NVDA",
        "2026-05-09",
        SAMPLE_STATE,
        "HOLD",
        None,
    )
    assert not yesterday_path.is_file(), "yesterday's entry should be evicted"
    today_path = yesterday_path.with_name("2026-05-09.json")
    assert today_path.is_file()


async def test_corrupt_file_returns_none() -> None:
    """A truncated / hand-edited cache file should never crash a run."""
    _fresh_data_dir()
    from tg_bot import cache

    cache.store(
        "openai",
        "gpt-4o",
        "o4-mini",
        "NVDA",
        "2026-05-09",
        SAMPLE_STATE,
        "BUY",
        None,
    )
    # Stomp the file with garbage.
    path = (
        Path(os.environ["TG_BOT_DATA_DIR"])
        / "result_cache"
        / "openai__gpt-4o__o4-mini"
        / "NVDA"
        / "2026-05-09.json"
    )
    path.write_text("not valid json {{{")
    got = cache.lookup("openai", "gpt-4o", "o4-mini", "NVDA", "2026-05-09")
    assert got is None


async def test_slug_handles_special_chars() -> None:
    """Provider/model strings with `/`, `:`, etc. don't blow up the path."""
    _fresh_data_dir()
    from tg_bot import cache

    cache.store(
        "openrouter/anthropic",
        "claude-sonnet-4:thinking",
        "haiku-4.5",
        "BRK-B",
        "2026-05-09",
        SAMPLE_STATE,
        "BUY",
        None,
    )
    got = cache.lookup(
        "openrouter/anthropic",
        "claude-sonnet-4:thinking",
        "haiku-4.5",
        "BRK-B",
        "2026-05-09",
    )
    assert got is not None and got["signal"] == "BUY"


async def test_atomic_write_is_complete_json() -> None:
    """Reading right after a store must always yield valid JSON — no
    partial writes (the rename pattern guarantees this)."""
    _fresh_data_dir()
    from tg_bot import cache

    cache.store(
        "openai",
        "gpt-4o",
        "o4-mini",
        "NVDA",
        "2026-05-09",
        SAMPLE_STATE,
        "BUY",
        "https://example.com",
    )
    path = (
        Path(os.environ["TG_BOT_DATA_DIR"])
        / "result_cache"
        / "openai__gpt-4o__o4-mini"
        / "NVDA"
        / "2026-05-09.json"
    )
    raw = path.read_text()
    parsed = json.loads(raw)
    assert parsed["signal"] == "BUY"
    assert "final_state" in parsed
    # No leftover .tmp files in the dir.
    leftover = list(path.parent.glob("*.tmp"))
    assert leftover == [], f"unexpected leftover tempfiles: {leftover}"


SCENARIOS = [
    ("lookup miss returns None", test_lookup_miss_returns_none),
    ("store + lookup round-trip", test_store_then_lookup_roundtrip),
    ("different config keys isolate", test_different_config_isolates),
    ("same config shares across users", test_same_config_shares_across_users),
    ("invalidate removes entry", test_invalidate_removes_entry),
    ("lazy eviction drops old dates", test_lazy_eviction_drops_old_dates),
    ("corrupt file returns None", test_corrupt_file_returns_none),
    ("slug handles special chars", test_slug_handles_special_chars),
    ("atomic write is complete JSON", test_atomic_write_is_complete_json),
]


async def main() -> int:
    failures = 0
    for label, fn in SCENARIOS:
        try:
            await fn()
        except AssertionError as e:
            failures += 1
            print(f"  {FAIL} {label}: {e}")
        except Exception as e:
            failures += 1
            print(f"  {FAIL} {label}: {type(e).__name__}: {e}")
        else:
            print(f"  {PASS} {label}")
    print()
    if failures:
        print(f"{FAIL} {failures} of {len(SCENARIOS)} failed")
        return 1
    print(f"{PASS} all {len(SCENARIOS)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
