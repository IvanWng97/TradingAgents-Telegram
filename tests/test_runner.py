"""Comprehensive smoke tests for the concurrent analysis flow.

Each test resets shared state, runs a scenario against mocked Telegram +
mocked TradingAgents, and asserts the orchestration behaves correctly:

  - Basic queue: cap=5, 10 tickers → 5 analyzing + 5 queued.
  - Cancel running → next queued promotes (slot transfer).
  - Cancel queued → never claims a slot.
  - Multi-cancel burst → all render Cancelled, no deadlock.
  - Pool reuse: 2nd batch reuses 1st's instances (no rebuild).
  - send_photo retry: TimedOut on first attempt, success on second.
  - send_photo failure: both attempts fail, slot released, registry cleaned.
  - propagate raises: non-cancel exception, graceful error caption.
  - Cancel post-completion race: cancel fires after to_thread returns.
  - Single ticker (cap=5): no queueing, just runs.
  - Graceful shutdown: post_stop signals every in-flight analysis.

Run with: pytest tests/test_runner.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from collections import defaultdict
from types import SimpleNamespace
from collections.abc import Callable

# `TG_BOT_MAX_CONCURRENT_ANALYSES=5` is set in `tests/conftest.py` so it
# applies before any test module loads `Config` regardless of collection
# order — see the conftest comment. Test fixtures here assume cap=5.

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Module-level: arm the env-var BEFORE importing the bot — `cache.py` reads
# `TG_BOT_DATA_DIR` on every call but other modules cache it at import.
from _smoke_helpers import (  # noqa: E402
    FakeBot,
    FakeContext,
    set_smoke_data_dir,
)

set_smoke_data_dir("smoke_runner_")

from tg_bot.handlers import analysis_runner as runner  # noqa: E402
from tg_bot.pipeline import analysis as analysis_mod  # noqa: E402


# --- Mock analysis function ------------------------------------------------

# Global stop signal — fake propagate holds slot until either its own
# cancel_event fires OR this signal is set.
_STOP_SIGNAL = threading.Event()
_BUILD_COUNT = 0  # tracks how many fresh graphs the pool built
_BUILD_LOCK = threading.Lock()


def fake_busy_analysis(ticker, reporter=None, **kw):
    """Holds slot until stop signal or cancel.

    Routes through the real GraphPool so pool-reuse tests can observe
    builder invocations. Uses a fixed config-key so all fake runs share
    one pool (pool reuse across runs is what we're testing). Signature
    matches `run_trading_analysis(ticker, reporter)` — `.env` is the
    single config source so neither user_id nor storage are needed.
    """
    config = {
        "llm_provider": "test",
        "deep_think_llm": "deep",
        "quick_think_llm": "quick",
    }
    pool = analysis_mod._get_or_create_pool(config)
    with pool.acquire() as _ta:
        while not _STOP_SIGNAL.is_set():
            if reporter and reporter.cancel_event and reporter.cancel_event.is_set():
                raise runner.CancelledByUserError("fake cancel")
            time.sleep(0.02)
    return ({"final_trade_decision": f"Decision for {ticker}: HOLD"}, "HOLD")


def fake_raising_analysis(ticker, *a, **kw):
    """Always raises a generic exception (not CancelledByUserError)."""
    raise RuntimeError(f"propagate failed for {ticker}")


async def fake_publish(title, content, edit_path=None):
    # `edit_path` is accepted (kwarg-only in callsite) so the publish path
    # actually exercises here instead of TypeError-ing into the catch-all.
    return f"https://telegra.ph/{title.replace(' ', '-')}-test"


# --- Test framework --------------------------------------------------------


def reset_state() -> None:
    """Reset every piece of shared state so tests are isolated.

    Includes a fresh `TG_BOT_DATA_DIR` per test — multiple scenarios
    here use overlapping ticker names (`TKR00..`), and the same-day
    result cache would otherwise let test N's stored entries short-
    circuit test M's `_run_analysis_for_ticker` call before any slot
    is acquired or caption rendered. The cache module reads the env
    var on every call, so swapping it here propagates without re-import.
    """
    global _STOP_SIGNAL, _BUILD_COUNT
    _STOP_SIGNAL = threading.Event()
    runner._STOP_SIGNAL = _STOP_SIGNAL  # not used, but keep symmetry
    runner._run_semaphore = None
    # Cancel-ack edit pacing is also module-level state — now per-chat,
    # so reset both dicts. Without this, a second scenario's first cancel
    # would inherit the prior scenario's timestamps and possibly stall.
    runner._cancel_edit_locks.clear()
    runner._last_cancel_edit_at.clear()
    analysis_mod._graph_pool = None
    with _BUILD_LOCK:
        _BUILD_COUNT = 0
    os.environ["TG_BOT_DATA_DIR"] = tempfile.mkdtemp(prefix="smoke_runner_")


def install_mocks(
    *,
    bot_send_photo_failures: int = 0,
    analysis_func: Callable = fake_busy_analysis,
) -> tuple[FakeBot, FakeContext]:
    runner.run_trading_analysis = analysis_func
    runner.publish_to_telegraph = fake_publish
    runner.TRADINGAGENTS_AVAILABLE = True

    # Track build count by wrapping the real builder via the pool.
    original_get_or_create = analysis_mod._get_or_create_pool

    def counting_pool(config):
        pool = original_get_or_create(config)

        def counted_builder():
            global _BUILD_COUNT
            with _BUILD_LOCK:
                _BUILD_COUNT += 1
            # Return a sentinel; fake_busy_analysis doesn't use the graph anyway.
            return SimpleNamespace(
                propagate=lambda **kwargs: (
                    SimpleNamespace(),
                    "HOLD",
                )
            )

        pool._builder = counted_builder
        return pool

    analysis_mod._get_or_create_pool = counting_pool

    bot = FakeBot(send_photo_failures=bot_send_photo_failures)
    return bot, FakeContext(bot)


def _msg_id_for(bot: FakeBot, ticker: str) -> int | None:
    from telegram.helpers import escape_markdown

    safe = escape_markdown(ticker, version=2)
    for mid, cap in bot.captions.items():
        if cap and (ticker in cap or safe in cap):
            return mid
    return None


def _trigger_cancel(context: FakeContext, ticker: str) -> None:
    registry = context.chat_data.get("analysis_cancels") or {}
    for entry in registry.values():
        mid = entry.get("message_id")
        if mid is None:
            continue
        cap = context.bot.captions.get(mid, "")
        from telegram.helpers import escape_markdown

        safe = escape_markdown(ticker, version=2)
        if ticker in cap or safe in cap:
            entry["cancel_event"].set()
            ae = entry.get("async_event")
            if ae is not None:
                ae.set()
            return
    raise AssertionError(f"no entry for ticker {ticker!r}")


# --- Test scenarios --------------------------------------------------------


async def test_cache_hit_skips_llm_and_renders_directly() -> None:
    """Pin the cache short-circuit: a pre-seeded same-day cache entry
    for the same (config_key, ticker, today) must skip the LLM run
    entirely — `run_trading_analysis` is never invoked, no semaphore
    slot is taken, no Telegraph round-trip, no cancel registry — and
    the final caption renders directly from the cached `final_state`
    under HTML mode. Cache miss already covered indirectly by every
    other scenario; this one pins the hit path."""
    reset_state()
    # Sentinel flag flipped if the fake analysis is invoked — must
    # remain False on a cache hit.
    invoked: list[str] = []

    def trip_wire_analysis(ticker, *a, **kw):
        invoked.append(ticker)
        return ({"final_trade_decision": f"{ticker}: HOLD"}, "HOLD")

    bot, ctx = install_mocks(analysis_func=trip_wire_analysis)

    # Pre-seed the cache with the exact key `_run_analysis_for_ticker`
    # will look up — derived via the same build_config() the runner
    # uses, so this test stays robust to DEFAULT_CONFIG drift.
    from tg_bot.pipeline import cache as result_cache
    from tg_bot.pipeline.config_key import AnalysisConfigKey
    from tg_bot.pipeline.analysis import build_config

    config = build_config()
    key = AnalysisConfigKey.from_config(config)
    today = result_cache.today_iso()
    cached_url = "https://telegra.ph/AAPL-Cached-Test"
    result_cache.store(
        key,
        "AAPL",
        today,
        {"final_trade_decision": "Final Trading Decision: BUY.\n\nReasoning."},
        "BUY",
        cached_url,
    )

    result = await runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL")
    assert result == "completed", result
    # Trip-wire stayed unfired — the LLM run was skipped.
    assert invoked == [], f"cache hit must skip LLM run; saw {invoked}"

    # The cache-hit branch calls send_photo with parse_mode=HTML and
    # the signal in the caption (no transient "Analyzing…" edits).
    sends = [(cap, mode) for cap, mode in bot.caption_history]
    assert len(sends) == 1, f"cache hit should emit exactly one send_photo; got {sends}"
    cap, mode = sends[0]
    assert mode == "HTML", f"cache-hit caption must use HTML; got {mode!r}"
    assert "BUY" in cap, f"signal must appear in caption; got {cap!r}"
    assert "<b>AAPL</b>" in cap, f"HTML ticker badge expected; got {cap!r}"
    # /status counter bumped on cache-hit successful delivery — same
    # post-`send_photo`-success rule as the fresh-run path. Without this
    # assertion, the cache-hit branch's `analysis_count` increment was
    # untested (the dedicated `test_analysis_count_*` scenario only
    # exercises the fresh-run path).
    assert ctx.bot_data.get("analysis_count") == 1, (
        f"cache-hit success must bump /status counter; "
        f"got {ctx.bot_data.get('analysis_count')!r}"
    )


async def test_force_refresh_bypasses_cache_hit_and_reuses_telegraph_url() -> None:
    """Pin the `/refresh` invariant: force_refresh=True must (a) bypass
    the cache-hit short-circuit so a fresh LLM run happens, AND (b) pass
    the prior cached `telegraph_url`'s path as `edit_path=` to
    `publish_to_telegraph` so the Telegraph page updates in place (same
    URL). Without this, refresh would create a fresh page on every tap
    — URL drifts, old shares go stale, orphan pages accumulate (the
    exact regression the edit-in-place plumbing was added to prevent)."""
    reset_state()
    invoked: list[str] = []

    def trip_wire_analysis(ticker, *a, **kw):
        invoked.append(ticker)
        return ({"final_trade_decision": f"{ticker}: SELL"}, "SELL")

    bot, ctx = install_mocks(analysis_func=trip_wire_analysis)

    # Record edit_path from each publish_to_telegraph call.
    publish_calls: list[dict] = []

    async def recording_publish(title, content, edit_path=None):
        publish_calls.append({"title": title, "edit_path": edit_path})
        return f"https://telegra.ph/{title.replace(' ', '-')}-fresh"

    runner.publish_to_telegraph = recording_publish

    # Pre-seed a cache entry with a known Telegraph URL so the runner
    # has something to pull `prev_telegraph_url` from.
    from tg_bot.pipeline import cache as result_cache
    from tg_bot.pipeline.config_key import AnalysisConfigKey
    from tg_bot.pipeline.analysis import build_config

    config = build_config()
    key = AnalysisConfigKey.from_config(config)
    today = result_cache.today_iso()
    cached_url = "https://telegra.ph/AAPL-Original-Page"
    result_cache.store(
        key,
        "AAPL",
        today,
        {"final_trade_decision": "Final Trading Decision: BUY."},
        "BUY",
        cached_url,
    )

    result = await runner._run_analysis_for_ticker(
        ctx, 1, 1, "AAPL", force_refresh=True
    )
    assert result == "completed", result
    # Cache short-circuit bypassed — the fake analysis fired.
    assert invoked == ["AAPL"], f"force_refresh should run analysis; got {invoked}"

    # publish_to_telegraph received the prior URL's path so the page
    # edits in place. `_path_from_telegraph_url` strips through the
    # last `/`, leaving `AAPL-Original-Page`.
    assert len(publish_calls) == 1, f"expected 1 publish call; got {publish_calls}"
    assert publish_calls[0]["edit_path"] == "AAPL-Original-Page", publish_calls[0]


async def test_basic_queue() -> None:
    """10 tickers, cap=5 → 5 analyzing + 5 queued at launch."""
    reset_state()
    bot, ctx = install_mocks()
    tickers = [f"TKR{i:02d}" for i in range(10)]
    tasks = [
        asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, t))
        for t in tickers
    ]
    await asyncio.sleep(0.3)

    analyzing = sum(
        1
        for t in tickers
        if (c := bot.captions.get(_msg_id_for(bot, t))) and c.startswith("📊 Analyzing")
    )
    queued = sum(
        1
        for t in tickers
        if (c := bot.captions.get(_msg_id_for(bot, t))) and "queued" in c.lower()
    )
    assert analyzing == 5, f"expected 5 analyzing, got {analyzing}"
    assert queued == 5, f"expected 5 queued, got {queued}"

    _STOP_SIGNAL.set()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_cancel_running_promotes_queued() -> None:
    """Cancel 2 running, verify exactly 2 queued promote (FIFO)."""
    reset_state()
    bot, ctx = install_mocks()
    tickers = ["AAPL", "TSLA", "NVDA", "MSFT", "GOOGL", "AMZN", "META"]
    tasks = [
        asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, t))
        for t in tickers
    ]
    await asyncio.sleep(0.3)

    _trigger_cancel(ctx, "AAPL")
    _trigger_cancel(ctx, "TSLA")
    await asyncio.sleep(0.5)

    promoted = [
        t
        for t in tickers[5:]  # the queued ones (AMZN, META)
        if (c := bot.captions.get(_msg_id_for(bot, t))) and c.startswith("📊 Analyzing")
    ]
    assert len(promoted) == 2, f"expected 2 promotions, got {promoted!r}"

    _STOP_SIGNAL.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    counts = defaultdict(int)
    for r in results:
        counts[r] += 1
    assert counts["cancelled"] == 2, counts
    assert counts["completed"] == 5, counts


async def test_cancel_queued_never_takes_slot() -> None:
    """Cancel a queued ticker; sem in-flight count must not increase."""
    reset_state()
    bot, ctx = install_mocks()
    tickers = [f"TKR{i:02d}" for i in range(7)]
    tasks = [
        asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, t))
        for t in tickers
    ]
    await asyncio.sleep(0.3)

    sem = runner._get_run_semaphore()
    in_flight_before = runner.Config.MAX_CONCURRENT_ANALYSES - sem._value
    assert in_flight_before == 5

    _trigger_cancel(ctx, "TKR06")  # last queued
    await asyncio.sleep(0.2)

    in_flight_after = runner.Config.MAX_CONCURRENT_ANALYSES - sem._value
    assert in_flight_after == 5, f"queued cancel changed slot count: {in_flight_after}"

    final = bot.captions.get(_msg_id_for(bot, "TKR06"))
    assert "cancelled" in (final or "").lower(), (
        f"queued cancel didn't render: {final!r}"
    )

    _STOP_SIGNAL.set()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_multi_cancel_burst() -> None:
    """Cancel 3 running tickers in rapid succession; all render Cancelled."""
    reset_state()
    bot, ctx = install_mocks()
    tickers = ["AAPL", "TSLA", "NVDA", "MSFT", "GOOGL"]
    tasks = [
        asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, t))
        for t in tickers
    ]
    await asyncio.sleep(0.3)

    for t in ["AAPL", "TSLA", "NVDA"]:
        _trigger_cancel(ctx, t)
    await asyncio.sleep(0.5)

    for t in ["AAPL", "TSLA", "NVDA"]:
        cap = bot.captions.get(_msg_id_for(bot, t))
        assert "cancelled" in (cap or "").lower(), f"{t} caption: {cap!r}"

    _STOP_SIGNAL.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    counts = defaultdict(int)
    for r in results:
        counts[r] += 1
    assert counts["cancelled"] == 3, counts


async def test_pool_reuse_no_rebuild() -> None:
    """First batch builds; second batch reuses same instances, no new builds."""
    reset_state()
    bot, ctx = install_mocks()

    # Batch 1: 3 tickers (under cap=5, all start)
    batch1 = [
        asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, t))
        for t in ["AAA", "BBB", "CCC"]
    ]
    await asyncio.sleep(0.3)
    _STOP_SIGNAL.set()
    await asyncio.gather(*batch1, return_exceptions=True)
    builds_after_batch1 = _BUILD_COUNT
    assert builds_after_batch1 == 3, f"expected 3 builds, got {builds_after_batch1}"

    # Batch 2: 3 different tickers (same LLM config — same pool key)
    _STOP_SIGNAL.clear()
    batch2 = [
        asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, t))
        for t in ["DDD", "EEE", "FFF"]
    ]
    await asyncio.sleep(0.3)
    _STOP_SIGNAL.set()
    await asyncio.gather(*batch2, return_exceptions=True)

    builds_after_batch2 = _BUILD_COUNT
    assert builds_after_batch2 == 3, (
        f"pool reuse failed — extra builds: {builds_after_batch2 - 3}"
    )


async def test_send_photo_retry_succeeds() -> None:
    """First send_photo attempt TimedOut; retry succeeds; analysis proceeds."""
    reset_state()
    bot, ctx = install_mocks(bot_send_photo_failures=1)

    task = asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))
    await asyncio.sleep(1.5)  # give time for retry (1s backoff + jitter)
    _STOP_SIGNAL.set()
    result = await task

    assert result == "completed", f"expected completed after retry, got {result!r}"
    cap = bot.captions.get(_msg_id_for(bot, "AAPL"))
    assert "AAPL" in (cap or ""), f"no AAPL caption after retry: {cap!r}"


async def test_send_photo_failure_cleanup() -> None:
    """Both attempts fail; slot released, registry cleaned, no leak."""
    reset_state()
    bot, ctx = install_mocks(bot_send_photo_failures=10)  # always fails

    sem = runner._get_run_semaphore()
    starting_value = sem._value
    task = asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))
    result = await task

    assert result == "completed", result  # "failed" path returns "completed"
    assert sem._value == starting_value, (
        f"slot leaked: {sem._value} vs {starting_value}"
    )
    assert not ctx.chat_data.get("analysis_cancels"), (
        f"registry leaked: {ctx.chat_data.get('analysis_cancels')}"
    )


async def test_cancel_edit_lock_is_per_chat() -> None:
    """`_get_cancel_edit_lock` returns the SAME lock for the same chat_id
    but DIFFERENT locks for distinct chat_ids. A previous version used
    one module-wide lock + timestamp pair, so a cancel-ack edit in chat A
    silently delayed cancel-acks in chat B by up to `_MIN_CANCEL_INTERVAL`
    even though Telegram's rate limit is per-chat. Per-chat locks remove
    that cross-chat coupling."""
    reset_state()
    lock_a1 = runner._get_cancel_edit_lock(101)
    lock_a2 = runner._get_cancel_edit_lock(101)
    lock_b = runner._get_cancel_edit_lock(202)
    assert lock_a1 is lock_a2, "same chat_id must reuse its lock"
    assert lock_a1 is not lock_b, "distinct chat_ids must get distinct locks"


async def test_analysis_count_only_after_send_photo_success() -> None:
    """`bot_data["analysis_count"]` must NOT be inflated when send_photo
    fails (network blip + retries exhausted). The runner returns
    "completed" on send_photo failure for queue-tally reasons, but the
    /status counter should reflect actually-delivered analyses — a
    previous version bumped at function entry and over-reported."""
    reset_state()
    bot, ctx = install_mocks(bot_send_photo_failures=10)  # always fails

    # Pre-condition: counter starts at 0.
    assert ctx.bot_data.get("analysis_count", 0) == 0

    task = asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))
    result = await task

    assert result == "completed"
    assert ctx.bot_data.get("analysis_count", 0) == 0, (
        f"counter inflated on send_photo failure: {ctx.bot_data.get('analysis_count')}"
    )

    # Sanity: a successful run DOES bump the counter (so the assertion
    # above isn't trivially passing because the increment is broken).
    reset_state()
    bot2, ctx2 = install_mocks()
    task = asyncio.create_task(runner._run_analysis_for_ticker(ctx2, 1, 1, "AAPL"))
    await asyncio.sleep(0.2)
    _STOP_SIGNAL.set()
    await task
    assert ctx2.bot_data.get("analysis_count") == 1, (
        f"counter didn't increment on successful delivery: "
        f"{ctx2.bot_data.get('analysis_count')}"
    )


async def test_propagate_raises_non_cancel() -> None:
    """propagate() raises generic exception; rendered as error, slot released."""
    reset_state()
    bot, ctx = install_mocks(analysis_func=fake_raising_analysis)
    sem = runner._get_run_semaphore()
    starting_value = sem._value

    task = asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))
    result = await task

    assert result == "completed", result  # error path returns "completed"
    assert sem._value == starting_value, f"slot leaked: {sem._value}"
    cap = bot.captions.get(_msg_id_for(bot, "AAPL")) or ""
    assert "Error" in cap or "AAPL" in cap, f"unexpected error caption: {cap!r}"


async def test_single_ticker_no_queueing() -> None:
    """1 ticker (cap=5) — runs immediately, no queue overhead."""
    reset_state()
    bot, ctx = install_mocks()
    task = asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))
    await asyncio.sleep(0.2)

    cap = bot.captions.get(_msg_id_for(bot, "AAPL"))
    assert cap and cap.startswith("📊 Analyzing"), f"unexpected: {cap!r}"

    _STOP_SIGNAL.set()
    result = await task
    assert result == "completed", result


async def test_graceful_shutdown_signals_all() -> None:
    """post_stop iterates registries and sets all events."""
    reset_state()
    bot, ctx = install_mocks()
    tickers = [f"TKR{i:02d}" for i in range(7)]
    tasks = [
        asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, t))
        for t in tickers
    ]
    await asyncio.sleep(0.3)

    # Simulate the post_stop hook by walking the registry directly.
    registry = ctx.chat_data.get("analysis_cancels") or {}
    assert len(registry) == 7, f"expected 7 in-flight, got {len(registry)}"
    for entry in registry.values():
        entry["cancel_event"].set()
        if entry.get("async_event"):
            entry["async_event"].set()

    await asyncio.sleep(0.5)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    counts = defaultdict(int)
    for r in results:
        counts[r] += 1
    assert counts["cancelled"] == 7, f"expected all 7 cancelled, got {counts}"


async def test_cancel_post_completion_race() -> None:
    """Cancel fires after to_thread returns but before result rendered."""
    reset_state()
    # Use an analysis function that completes immediately and sets a flag
    # so we can race the cancel against the post-completion render.
    completion_event = threading.Event()

    def quick_analysis(ticker, *a, **kw):
        completion_event.set()
        return ({"final_trade_decision": f"{ticker}: HOLD"}, "HOLD")

    bot, ctx = install_mocks(analysis_func=quick_analysis)
    task = asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))

    # Wait for analysis to complete, then race cancel.
    while not completion_event.is_set():
        await asyncio.sleep(0.01)
    # At this point, to_thread has just returned. Set the cancel event.
    registry = ctx.chat_data.get("analysis_cancels") or {}
    for entry in registry.values():
        entry["cancel_event"].set()
        if entry.get("async_event"):
            entry["async_event"].set()

    result = await task
    # Either path is acceptable; what we're verifying is no slot leak.
    assert result in ("cancelled", "completed"), result
    sem = runner._get_run_semaphore()
    assert sem._value == runner.Config.MAX_CONCURRENT_ANALYSES, (
        f"slot leaked after race: {sem._value}"
    )


# --- Race-close coverage (CLAUDE.md Invariant #5) --------------------------
# Three checks discard a late-arriving Cancel rather than overwriting the
# "Cancelling…" caption with a success: (1) flag-check post-to_thread,
# (2) flag-check post-Telegraph publish, (3) CancelledByUserError from the
# progress callback during propagate. The deterministic scenarios below
# arm each path directly so a regression flips the test from "cancelled"
# to "completed" — the post-completion-race test above only asserts no
# slot leak so it cannot catch a missing flag check.


async def test_race_close_post_to_thread_discards_result() -> None:
    """Flag-check #1: cancel_event set inside the analysis (before return)
    makes the post-to_thread check on the asyncio side flip to 'cancelled'."""
    reset_state()

    def self_cancelling_analysis(ticker, reporter=None, **kw):
        # Simulate "user tapped Cancel during the last LLM call" — the
        # propagate() returns successfully, but the cancel event was set
        # while it was on the wire. The post-to_thread check (line ~525)
        # must honour the flag rather than render the success caption.
        if reporter is not None and reporter.cancel_event is not None:
            reporter.cancel_event.set()
        return ({"final_trade_decision": f"{ticker}: HOLD"}, "HOLD")

    bot, ctx = install_mocks(analysis_func=self_cancelling_analysis)
    result = await runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL")
    assert result == "cancelled", f"expected cancelled, got {result!r}"
    cap = bot.captions.get(_msg_id_for(bot, "AAPL")) or ""
    assert "cancelled" in cap.lower(), f"expected Cancelled caption, got {cap!r}"


async def test_race_close_post_telegraph_publish_discards_result() -> None:
    """Flag-check #2: cancel_event set during Telegraph publish (between
    LLM finish and final caption edit) makes the post-publish check flip
    to 'cancelled' rather than committing the success caption."""
    reset_state()

    def quick_analysis(ticker, *a, **kw):
        return ({"final_trade_decision": f"{ticker}: HOLD"}, "HOLD")

    bot, ctx = install_mocks(analysis_func=quick_analysis)

    async def cancelling_publish(title, content, edit_path=None):
        # Simulate "user tapped Cancel while Telegraph publish was on the
        # wire". Flip the cancel event before returning the URL so the
        # post-publish flag check (line ~566) discards instead of editing.
        registry = ctx.chat_data.get("analysis_cancels") or {}
        for entry in registry.values():
            entry["cancel_event"].set()
            if entry.get("async_event"):
                entry["async_event"].set()
        return f"https://telegra.ph/{title.replace(' ', '-')}-tg"

    runner.publish_to_telegraph = cancelling_publish

    result = await runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL")
    assert result == "cancelled", f"expected cancelled, got {result!r}"
    cap = bot.captions.get(_msg_id_for(bot, "AAPL")) or ""
    assert "cancelled" in cap.lower(), f"expected Cancelled caption, got {cap!r}"


async def test_race_close_cancelled_by_user_error_branch() -> None:
    """Exception boundary #3: CancelledByUserError raised mid-propagate
    (the path ProgressReporter._dispatch takes when cancel_event fires
    before a step) lands in the `except CancelledByUserError` arm and
    renders Cancelled — not the generic exception handler that would
    surface as 'Error analyzing AAPL'."""
    reset_state()

    def raising_analysis(ticker, *a, **kw):
        raise runner.CancelledByUserError(f"in-pipeline cancel for {ticker}")

    bot, ctx = install_mocks(analysis_func=raising_analysis)
    result = await runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL")
    assert result == "cancelled", f"expected cancelled, got {result!r}"
    cap = bot.captions.get(_msg_id_for(bot, "AAPL")) or ""
    assert "cancelled" in cap.lower(), f"expected Cancelled caption, got {cap!r}"
    # Crucially: no error caption — the generic except branch would render
    # "Error analyzing AAPL." which would be a regression.
    assert "Error analyzing" not in cap, f"hit generic exception arm: {cap!r}"


# --- parse_mode contract (CLAUDE.md Invariant #4) --------------------------


async def test_progress_edit_reattaches_cancel_keyboard() -> None:
    """Pin Invariant #3 (root CLAUDE.md): Telegram's editMessageCaption
    drops `reply_markup` unless re-sent on every edit, so the
    `ProgressReporter` MUST re-attach the ❌ Cancel button on every
    per-step caption edit. Without this, the cancel button vanishes
    after the first per-step update and the user can't abort. Use a
    fake analysis that fires one `reporter.report` call before
    returning so a per-step edit definitely fires."""
    reset_state()

    def reporting_analysis(ticker, reporter=None, **kw):
        if reporter is not None:
            # Trigger one per-step caption edit via the reporter. This
            # path uses `bot.edit_message_caption` which goes through
            # the FakeBot — we then introspect edit_markup_history to
            # confirm reply_markup is non-None and carries the cancel
            # callback.
            future = asyncio.run_coroutine_threadsafe(
                reporter.report("Bullish Analyst"), reporter.loop
            )
            future.result(timeout=5)
        return ({"final_trade_decision": f"{ticker}: HOLD"}, "HOLD")

    bot, ctx = install_mocks(analysis_func=reporting_analysis)
    result = await runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL")
    assert result == "completed", result

    # At least one edit_message_caption call between the initial
    # send_photo and the final completion must include a reply_markup
    # carrying a `cancel_analysis:<run_id>` callback_data — proves the
    # ProgressReporter re-sent the button on its per-step edit.
    progress_edits = [
        (cap, mk)
        for cap, mk in bot.edit_markup_history
        if cap and "running" in cap.lower() and "Bullish Analyst" in cap
    ]
    assert progress_edits, (
        f"no progress edit found in history: {bot.edit_markup_history!r}"
    )
    cap, mk = progress_edits[0]
    assert mk is not None, (
        f"reply_markup dropped on progress edit: {cap!r} -> mk={mk!r}"
    )
    # Walk the inline keyboard to find the cancel callback.
    callbacks = [
        btn.callback_data
        for row in mk.inline_keyboard
        for btn in row
        if btn.callback_data
    ]
    assert any(cb.startswith("cancel_analysis:") for cb in callbacks), (
        f"cancel_analysis:<run_id> callback missing from re-attached keyboard: {callbacks}"
    )


async def test_queued_caption_shown_before_semaphore_wait() -> None:
    """Pin the UX guarantee that a queued ticker's caption reads
    "⏳ Queued" BEFORE the holding ticker releases its slot. With cap=1
    and two parallel tickers, the second must show its queued caption
    while the first is still in-flight — otherwise the user stares at a
    blank "Analyzing…" placeholder until the first finishes."""
    reset_state()
    # Override the global cap to 1 for this scenario. Restore via
    # finally so other tests aren't affected.
    sem_backup = runner._run_semaphore
    cap_backup = runner.Config.MAX_CONCURRENT_ANALYSES
    runner.Config.MAX_CONCURRENT_ANALYSES = 1
    runner._run_semaphore = asyncio.Semaphore(1)

    try:
        bot, ctx = install_mocks()  # uses fake_busy_analysis (holds on _STOP_SIGNAL)
        tickers = ["AAA", "BBB"]
        tasks = [
            asyncio.create_task(runner._run_analysis_for_ticker(ctx, 1, 1, t))
            for t in tickers
        ]
        # Let both send_photo calls land. AAA gets the slot ("Analyzing"),
        # BBB shows "⏳ ... queued" since cap=1.
        await asyncio.sleep(0.3)

        caps = {t: bot.captions.get(_msg_id_for(bot, t)) or "" for t in tickers}
        assert caps["AAA"].startswith("📊 Analyzing"), (
            f"AAA should be Analyzing (held the slot); got {caps['AAA']!r}"
        )
        assert "queued" in caps["BBB"].lower(), (
            f"BBB should show queued before AAA releases; got {caps['BBB']!r}"
        )

        _STOP_SIGNAL.set()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        runner.Config.MAX_CONCURRENT_ANALYSES = cap_backup
        runner._run_semaphore = sem_backup


async def test_getmd_callback_sends_document_on_hit_and_fallback_on_miss() -> None:
    """Pin `_handle_get_full_md` (the `📥 Download Full Report` callback).
    Two paths: (a) state found → builds the .md and posts it via
    `query.message.chat.send_document` with filename `<TICKER>_<DATE>.md`;
    (b) state missing → falls back to `context.bot.send_message` with
    an "unavailable" notice. Without this test, a refactor of either
    branch silently breaks the .md download UX."""
    reset_state()
    bot, ctx = install_mocks()

    # Build a fake `query` that mirrors the PTB Update.callback_query
    # shape `_handle_get_full_md` reads: query.message.chat_id,
    # query.message.message_id, query.message.chat.send_document.
    photo_msg = await bot.send_photo(
        chat_id=99,
        photo="http://example/x",
        caption="placeholder",
        parse_mode="HTML",
        reply_markup=None,
    )
    query = SimpleNamespace(message=photo_msg)

    # ----- (a) HIT path: monkey-patch load_historical_state to return state.
    from tg_bot.handlers import analysis_runner as runner_mod

    orig_load = runner_mod.load_historical_state
    runner_mod.load_historical_state = lambda t, d: {
        "final_trade_decision": f"Final Trading Decision: BUY for {t} on {d}."
    }
    try:
        await runner_mod._handle_get_full_md(query, ctx, "AAPL", "2026-05-16")
    finally:
        runner_mod.load_historical_state = orig_load

    assert len(bot.documents) == 1, (
        f"hit path must send_document exactly once; got {bot.documents!r}"
    )
    doc_call = bot.documents[0]
    assert doc_call["document"].filename == "AAPL_2026-05-16.md", doc_call["document"]
    # No fallback message on the happy path.
    assert bot.messages == [], (
        f"send_message must not fire on hit path; got {bot.messages!r}"
    )

    # ----- (b) MISS path: load_historical_state returns None → fallback.
    runner_mod.load_historical_state = lambda t, d: None
    try:
        await runner_mod._handle_get_full_md(query, ctx, "NVDA", "2026-05-15")
    finally:
        runner_mod.load_historical_state = orig_load

    assert len(bot.messages) == 1, (
        f"miss path must send_message once; got {bot.messages!r}"
    )
    assert "unavailable" in bot.messages[0]["text"].lower(), bot.messages[0]
    # No second send_document on the miss path.
    assert len(bot.documents) == 1, (
        f"miss path must not send_document; got {bot.documents!r}"
    )


async def test_parse_mode_contract_across_lifecycle() -> None:
    """Pin the per-call parse_mode used by `_run_analysis_for_ticker`:
    initial `send_photo` + transient `Analyzing…` edits use MarkdownV2;
    the final completion `editMessageCaption` uses HTML. A mismatch
    surfaces as `Bad Request: can't parse entities` at runtime, which
    no unit test on the formatter functions alone can catch — this
    scenario watches the actual bot-API call sequence."""
    reset_state()

    def quick_analysis(ticker, *a, **kw):
        return ({"final_trade_decision": f"{ticker}: HOLD"}, "HOLD")

    bot, ctx = install_mocks(analysis_func=quick_analysis)
    result = await runner._run_analysis_for_ticker(ctx, 1, 1, "AAPL")
    assert result == "completed", result

    # Initial send_photo carries the "📊 Analyzing…" caption under MarkdownV2.
    analyzing = [
        (cap, mode)
        for cap, mode in bot.caption_history
        if cap and cap.startswith("📊 Analyzing")
    ]
    assert analyzing, f"no 📊 Analyzing caption seen: {bot.caption_history!r}"
    for cap, mode in analyzing:
        assert mode == "MarkdownV2", (
            f"transient caption must use MarkdownV2, got {mode!r}: {cap!r}"
        )

    # The final caption (after Telegraph publish) flips to HTML — it
    # contains `<b>TICKER</b>` and the markdown-rendered summary block.
    html_finals = [
        (cap, mode) for cap, mode in bot.caption_history if cap and "<b>AAPL</b>" in cap
    ]
    assert html_finals, f"no HTML final caption seen: {bot.caption_history!r}"
    for cap, mode in html_finals:
        assert mode == "HTML", (
            f"final caption must use HTML, got {mode!r}: {cap[:120]!r}"
        )


# --- Runner ----------------------------------------------------------------
