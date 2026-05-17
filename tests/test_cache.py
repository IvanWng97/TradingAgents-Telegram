"""Smoke tests for the same-day result cache (`tg_bot.pipeline.cache`).

The cache API now takes `AnalysisConfigKey` directly (no positional
provider/deep/quick/rounds/effort explosion), and the cache-hygiene
gate is enforced inside `cache.store` itself (no `_should_cache_publish`
caller predicate). Both shifts converge the public surface to:

    lookup(key, ticker, date_iso) -> dict | None
    store(key, ticker, date_iso, final_state, signal, telegraph_url)
    today_iso() -> str  (local-date convenience)

Each scenario uses a fresh temporary `TG_BOT_DATA_DIR` so disk state is
isolated. Covers the storage layer in isolation; integration with the
full /watch and digest fan-out is exercised by the existing
`smoke_digest.py` fan-out tests (which see the cache via
`_analyze_one_for_digest`).

Run with: pytest tests/test_cache.py
"""

from __future__ import annotations

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

from tg_bot.pipeline.config_key import AnalysisConfigKey  # noqa: E402


def _fresh_data_dir() -> Path:
    """New tempdir + repoint TG_BOT_DATA_DIR. The cache module reads the
    env var on every call (no module-level caching), so repeated swaps
    work without reimporting."""
    d = Path(tempfile.mkdtemp(prefix="tg_bot_cache_smoke_"))
    os.environ["TG_BOT_DATA_DIR"] = str(d)
    return d


# Default key used by most scenarios — the openai/gpt-4o/o4-mini default
# triple. Tests that need to exercise key-identity behavior (different
# providers, custom rounds, effort) construct their own keys inline.
DEFAULT_KEY = AnalysisConfigKey(provider="openai", deep="gpt-4o", quick="o4-mini")
DEFAULT_SLUG = "openai__gpt-4o__o4-mini"


# Sample (final_state, signal, telegraph_url) — minimal structure that
# matches what `_run_analysis_for_ticker` writes via `result_cache.store`.
SAMPLE_STATE = {
    "final_trade_decision": "Final Trading Decision: BUY. Reasoning ...",
    "trader_investment_plan": "Buy 100 shares ...",
}

PLACEHOLDER_URL = "https://telegra.ph/test-placeholder"


async def test_lookup_miss_returns_none() -> None:
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    assert cache.lookup(DEFAULT_KEY, "NVDA", "2026-05-09") is None


async def test_store_then_lookup_roundtrip() -> None:
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    cache.store(
        DEFAULT_KEY,
        "NVDA",
        "2026-05-09",
        SAMPLE_STATE,
        "BUY",
        "https://telegra.ph/NVDA-05-09",
    )
    got = cache.lookup(DEFAULT_KEY, "NVDA", "2026-05-09")
    assert got is not None
    assert got["signal"] == "BUY"
    assert got["telegraph_url"] == "https://telegra.ph/NVDA-05-09"
    assert got["final_state"]["final_trade_decision"].startswith("Final Trading")


async def test_different_config_isolates() -> None:
    """Different (provider, deep, quick) combos must NOT collide — the
    LLM output for two providers on the same ticker is genuinely different."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    key_openai = DEFAULT_KEY
    key_deepseek = AnalysisConfigKey(
        provider="deepseek", deep="deepseek-v4", quick="deepseek-chat"
    )
    cache.store(
        key_openai, "NVDA", "2026-05-09", SAMPLE_STATE, "BUY", "https://t.ly/openai"
    )
    cache.store(
        key_deepseek,
        "NVDA",
        "2026-05-09",
        SAMPLE_STATE,
        "SELL",
        "https://t.ly/deepseek",
    )
    a = cache.lookup(key_openai, "NVDA", "2026-05-09")
    b = cache.lookup(key_deepseek, "NVDA", "2026-05-09")
    assert a["signal"] == "BUY" and a["telegraph_url"].endswith("openai")
    assert b["signal"] == "SELL" and b["telegraph_url"].endswith("deepseek")


async def test_same_config_shares_across_users() -> None:
    """The cache key has no user_id — two users on the same config get
    a shared hit on the second tap. This is the core correctness guarantee."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    key = AnalysisConfigKey(
        provider="deepseek", deep="deepseek-v4", quick="deepseek-chat"
    )
    # Simulate user A's run.
    cache.store(key, "NVDA", "2026-05-09", SAMPLE_STATE, "BUY", "https://t.ly/x")
    # User B (different user_id, same /config) — no second store call.
    # Their lookup should hit user A's cache file.
    got = cache.lookup(key, "NVDA", "2026-05-09")
    assert got is not None and got["signal"] == "BUY"


async def test_lazy_eviction_drops_old_dates() -> None:
    """When a fresh date lands for the same (config, ticker), older
    date files in that directory are swept. Bounds disk growth."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    # Yesterday's run.
    cache.store(DEFAULT_KEY, "NVDA", "2026-05-08", SAMPLE_STATE, "BUY", PLACEHOLDER_URL)
    yesterday_path = (
        Path(os.environ["TG_BOT_DATA_DIR"])
        / "result_cache"
        / DEFAULT_SLUG
        / "NVDA"
        / "2026-05-08.json"
    )
    assert yesterday_path.is_file()

    # Today's run lands → yesterday's gets swept.
    cache.store(
        DEFAULT_KEY, "NVDA", "2026-05-09", SAMPLE_STATE, "HOLD", PLACEHOLDER_URL
    )
    assert not yesterday_path.is_file(), "yesterday's entry should be evicted"
    today_path = yesterday_path.with_name("2026-05-09.json")
    assert today_path.is_file()


async def test_corrupt_file_returns_none() -> None:
    """A truncated / hand-edited cache file should never crash a run."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    cache.store(DEFAULT_KEY, "NVDA", "2026-05-09", SAMPLE_STATE, "BUY", PLACEHOLDER_URL)
    # Stomp the file with garbage.
    path = (
        Path(os.environ["TG_BOT_DATA_DIR"])
        / "result_cache"
        / DEFAULT_SLUG
        / "NVDA"
        / "2026-05-09.json"
    )
    path.write_text("not valid json {{{")
    got = cache.lookup(DEFAULT_KEY, "NVDA", "2026-05-09")
    assert got is None


async def test_slug_handles_special_chars() -> None:
    """Provider/model strings with `/`, `:`, etc. don't blow up the path —
    `AnalysisConfigKey.slug()` sanitizes these to filesystem-safe form."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    key = AnalysisConfigKey(
        provider="openrouter/anthropic",
        deep="claude-sonnet-4:thinking",
        quick="haiku-4.5",
    )
    cache.store(key, "BRK-B", "2026-05-09", SAMPLE_STATE, "BUY", PLACEHOLDER_URL)
    got = cache.lookup(key, "BRK-B", "2026-05-09")
    assert got is not None and got["signal"] == "BUY"


async def test_non_serializable_objects_in_state() -> None:
    """`final_state` from tradingagents carries LangChain message objects
    (e.g. HumanMessage) that aren't JSON-native. The cache must coerce
    these via `default=` instead of failing the whole write — otherwise
    every run leaves an orphan .tmp and the next lookup misses."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    class FakeMessage:
        """Pydantic-v1 style — has .dict() returning a serializable form."""

        def __init__(self, content: str) -> None:
            self.content = content

        def dict(self) -> dict:
            return {"type": "human", "content": self.content}

    class OpaqueMessage:
        """No .dict(), no .model_dump() — only .content. Falls through to
        the typed-content branch of _json_default."""

        def __init__(self, content: str) -> None:
            self.content = content

    state = {
        "final_trade_decision": "BUY",
        "messages": [FakeMessage("hi"), OpaqueMessage("opaque")],
    }
    cache.store(DEFAULT_KEY, "NVDA", "2026-05-09", state, "BUY", PLACEHOLDER_URL)
    got = cache.lookup(DEFAULT_KEY, "NVDA", "2026-05-09")
    assert got is not None, "store must not fail on LangChain-style messages"
    msgs = got["final_state"]["messages"]
    assert msgs[0]["content"] == "hi"
    assert msgs[1]["content"] == "opaque" and msgs[1]["__type__"] == "OpaqueMessage"
    # And no orphan tempfiles linger.
    cache_dir = (
        Path(os.environ["TG_BOT_DATA_DIR"]) / "result_cache" / DEFAULT_SLUG / "NVDA"
    )
    assert list(cache_dir.glob("*.tmp")) == []


async def test_default_rounds_effort_keeps_slug() -> None:
    """Default users (rounds=1, effort=None) must keep the original slug
    shape (`provider__deep__quick`) so existing on-disk cache entries
    don't get orphaned when the rounds/effort feature ships."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    cache.store(DEFAULT_KEY, "NVDA", "2026-05-09", SAMPLE_STATE, "BUY", PLACEHOLDER_URL)
    expected = (
        Path(os.environ["TG_BOT_DATA_DIR"])
        / "result_cache"
        / DEFAULT_SLUG
        / "NVDA"
        / "2026-05-09.json"
    )
    assert expected.is_file(), f"default slug shape changed: missing {expected}"


async def test_custom_rounds_isolate_slot() -> None:
    """rounds=1 vs rounds=2 must NOT share a cache file — they produce
    genuinely different LLM output."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    key_r1 = AnalysisConfigKey(
        provider="openai", deep="gpt-4o", quick="o4-mini", rounds=1
    )
    key_r2 = AnalysisConfigKey(
        provider="openai", deep="gpt-4o", quick="o4-mini", rounds=2
    )
    cache.store(key_r1, "NVDA", "2026-05-09", SAMPLE_STATE, "BUY", PLACEHOLDER_URL)
    cache.store(key_r2, "NVDA", "2026-05-09", SAMPLE_STATE, "SELL", PLACEHOLDER_URL)
    a = cache.lookup(key_r1, "NVDA", "2026-05-09")
    b = cache.lookup(key_r2, "NVDA", "2026-05-09")
    assert a is not None and a["signal"] == "BUY"
    assert b is not None and b["signal"] == "SELL"


async def test_custom_effort_isolates_slot() -> None:
    """Different effort levels (or None vs set) must isolate cache slots."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    key_high = AnalysisConfigKey(
        provider="openai", deep="gpt-4o", quick="o4-mini", effort="high"
    )
    cache.store(key_high, "NVDA", "2026-05-09", SAMPLE_STATE, "HOLD", PLACEHOLDER_URL)
    # Default effort=None lookup should miss high-effort entry.
    miss = cache.lookup(DEFAULT_KEY, "NVDA", "2026-05-09")
    assert miss is None
    hit = cache.lookup(key_high, "NVDA", "2026-05-09")
    assert hit is not None and hit["signal"] == "HOLD"


async def test_generated_at_persisted() -> None:
    """Every store() stamps `generated_at` (ISO UTC) so cache-hit renders
    can show the original analysis time instead of the moment-of-tap."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    cache.store(DEFAULT_KEY, "NVDA", "2026-05-09", SAMPLE_STATE, "BUY", PLACEHOLDER_URL)
    got = cache.lookup(DEFAULT_KEY, "NVDA", "2026-05-09")
    ts = got.get("generated_at")
    assert isinstance(ts, str) and "T" in ts and ts.endswith("+00:00"), (
        f"expected ISO UTC string, got {ts!r}"
    )


async def test_atomic_write_is_complete_json() -> None:
    """Reading right after a store must always yield valid JSON — no
    partial writes (the rename pattern guarantees this)."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    cache.store(
        DEFAULT_KEY, "NVDA", "2026-05-09", SAMPLE_STATE, "BUY", "https://example.com"
    )
    path = (
        Path(os.environ["TG_BOT_DATA_DIR"])
        / "result_cache"
        / DEFAULT_SLUG
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


async def test_store_gate_skips_falsy_telegraph_url() -> None:
    """Cache-hygiene gate is now built into `cache.store`. Passing a None
    or empty `telegraph_url` is a no-op — no file is written. Caching a
    failed publish would trap the user: every subsequent `/watch` faithfully
    replays "Instant View unavailable" until midnight rollover. The store
    gate prevents that at the write site, so callers can pass the URL
    directly without remembering to gate."""
    _fresh_data_dir()
    from tg_bot.pipeline import cache

    # None URL → no-op
    cache.store(DEFAULT_KEY, "INTU", "2026-05-11", SAMPLE_STATE, "HOLD", None)
    assert cache.lookup(DEFAULT_KEY, "INTU", "2026-05-11") is None

    # Empty string URL → no-op (some SDK paths could plausibly return "").
    cache.store(DEFAULT_KEY, "INTU", "2026-05-11", SAMPLE_STATE, "HOLD", "")
    assert cache.lookup(DEFAULT_KEY, "INTU", "2026-05-11") is None

    # Real URL → stored normally.
    cache.store(
        DEFAULT_KEY, "INTU", "2026-05-11", SAMPLE_STATE, "BUY", "https://telegra.ph/x"
    )
    got = cache.lookup(DEFAULT_KEY, "INTU", "2026-05-11")
    assert got is not None and got["telegraph_url"] == "https://telegra.ph/x"


async def test_today_iso_returns_local_iso_date() -> None:
    """`today_iso()` returns the LOCAL-date ISO string — matches both the
    cache key AND tradingagents' on-disk log filename convention. Using a
    UTC stamp would 404 for late-night runs from negative-UTC zones."""
    from tg_bot.pipeline import cache
    from datetime import date

    out = cache.today_iso()
    assert out == date.today().isoformat()
