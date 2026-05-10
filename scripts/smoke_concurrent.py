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

Run with: .venv/bin/python3 scripts/smoke_concurrent.py
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
from typing import Callable

# Cap=5 across all tests. Individual tests vary ticker count.
os.environ["TG_BOT_MAX_CONCURRENT_ANALYSES"] = "5"
# Isolate from any real on-disk cache: a same-day result cache hit would
# short-circuit `_run_analysis_for_ticker` before the cancel registry +
# semaphore plumbing fires, defeating the orchestration assertions.
os.environ["TG_BOT_DATA_DIR"] = tempfile.mkdtemp(prefix="smoke_concurrent_")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tg_bot.handlers import callbacks  # noqa: E402
from tg_bot import analysis as analysis_mod  # noqa: E402


# --- Mocks -----------------------------------------------------------------


class FakeTimedOut(Exception):
    """Stand-in for telegram.error.TimedOut so the retry path catches it."""

    def __init__(self, msg: str = "Timed out"):
        super().__init__(msg)


# Make the type name "TimedOut" so the retry's `type(e).__name__` check fires.
FakeTimedOut.__name__ = "TimedOut"


class FakeBot:
    """Records every call. Optionally fails send_photo on the first N attempts."""

    def __init__(self, send_photo_failures: int = 0) -> None:
        self.captions: dict[int, str] = {}
        self._next_id = 1000
        self._lock = asyncio.Lock()
        self._send_photo_attempts: dict[int, int] = defaultdict(int)
        self._send_photo_failures = send_photo_failures

    async def send_photo(self, chat_id, photo, caption, parse_mode, reply_markup):
        # Identify which logical "send" this is via the caption (contains ticker).
        key = hash(caption)
        self._send_photo_attempts[key] += 1
        if self._send_photo_attempts[key] <= self._send_photo_failures:
            raise FakeTimedOut(
                f"fake timeout (attempt {self._send_photo_attempts[key]})"
            )
        async with self._lock:
            mid = self._next_id
            self._next_id += 1
        self.captions[mid] = caption
        return SimpleNamespace(message_id=mid)

    async def edit_message_caption(
        self, chat_id, message_id, caption, parse_mode=None, reply_markup=None
    ):
        self.captions[message_id] = caption

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        pass

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        pass

    async def send_message(self, chat_id, text, parse_mode=None):
        pass


class FakeContext:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot
        self.chat_data: dict = {}
        self.bot_data: dict = {}


# --- Mock analysis function ------------------------------------------------

# Global stop signal — fake propagate holds slot until either its own
# cancel_event fires OR this signal is set.
_STOP_SIGNAL = threading.Event()
_BUILD_COUNT = 0  # tracks how many fresh graphs the pool built
_BUILD_LOCK = threading.Lock()


def fake_busy_analysis(ticker, user_id, user_config_storage, reporter=None, **kw):
    """Holds slot until stop signal or cancel.

    Routes through the real GraphPool so pool-reuse tests can observe
    builder invocations. Uses a fixed config-key so all fake runs share
    one pool (pool reuse across runs is what we're testing).
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
                raise callbacks.CancelledByUserError("fake cancel")
            time.sleep(0.02)
    return ({"final_trade_decision": f"Decision for {ticker}: HOLD"}, "HOLD")


def fake_raising_analysis(ticker, *a, **kw):
    """Always raises a generic exception (not CancelledByUserError)."""
    raise RuntimeError(f"propagate failed for {ticker}")


async def fake_publish(title, content):
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
    callbacks._STOP_SIGNAL = _STOP_SIGNAL  # not used, but keep symmetry
    callbacks._run_semaphore = None
    analysis_mod._graph_pool.clear()
    with _BUILD_LOCK:
        _BUILD_COUNT = 0
    os.environ["TG_BOT_DATA_DIR"] = tempfile.mkdtemp(prefix="smoke_concurrent_")


def install_mocks(
    *,
    bot_send_photo_failures: int = 0,
    analysis_func: Callable = fake_busy_analysis,
) -> tuple[FakeBot, FakeContext]:
    callbacks.run_trading_analysis = analysis_func
    callbacks.publish_to_telegraph = fake_publish
    callbacks.TRADINGAGENTS_AVAILABLE = True

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
    for run_id, entry in registry.items():
        mid = entry.get("message_id")
        if mid is None:
            continue
        cap = context.bot.captions.get(mid, "")
        from telegram.helpers import escape_markdown

        safe = escape_markdown(ticker, version=2)
        if ticker in cap or safe in cap:
            entry["event"].set()
            ae = entry.get("async_event")
            if ae is not None:
                ae.set()
            return
    raise AssertionError(f"no entry for ticker {ticker!r}")


# --- Test scenarios --------------------------------------------------------


async def test_basic_queue() -> None:
    """10 tickers, cap=5 → 5 analyzing + 5 queued at launch."""
    reset_state()
    bot, ctx = install_mocks()
    tickers = [f"TKR{i:02d}" for i in range(10)]
    tasks = [
        asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, t))
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
        asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, t))
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
        asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, t))
        for t in tickers
    ]
    await asyncio.sleep(0.3)

    sem = callbacks._get_run_semaphore()
    in_flight_before = callbacks.Config.MAX_CONCURRENT_ANALYSES - sem._value
    assert in_flight_before == 5

    _trigger_cancel(ctx, "TKR06")  # last queued
    await asyncio.sleep(0.2)

    in_flight_after = callbacks.Config.MAX_CONCURRENT_ANALYSES - sem._value
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
        asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, t))
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
        asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, t))
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
        asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, t))
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

    task = asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))
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

    sem = callbacks._get_run_semaphore()
    starting_value = sem._value
    task = asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))
    result = await task

    assert result == "completed", result  # "failed" path returns "completed"
    assert sem._value == starting_value, (
        f"slot leaked: {sem._value} vs {starting_value}"
    )
    assert not ctx.chat_data.get("analysis_cancels"), (
        f"registry leaked: {ctx.chat_data.get('analysis_cancels')}"
    )


async def test_propagate_raises_non_cancel() -> None:
    """propagate() raises generic exception; rendered as error, slot released."""
    reset_state()
    bot, ctx = install_mocks(analysis_func=fake_raising_analysis)
    sem = callbacks._get_run_semaphore()
    starting_value = sem._value

    task = asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))
    result = await task

    assert result == "completed", result  # error path returns "completed"
    assert sem._value == starting_value, f"slot leaked: {sem._value}"
    cap = bot.captions.get(_msg_id_for(bot, "AAPL")) or ""
    assert "Error" in cap or "AAPL" in cap, f"unexpected error caption: {cap!r}"


async def test_single_ticker_no_queueing() -> None:
    """1 ticker (cap=5) — runs immediately, no queue overhead."""
    reset_state()
    bot, ctx = install_mocks()
    task = asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))
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
        asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, t))
        for t in tickers
    ]
    await asyncio.sleep(0.3)

    # Simulate the post_stop hook by walking the registry directly.
    registry = ctx.chat_data.get("analysis_cancels") or {}
    assert len(registry) == 7, f"expected 7 in-flight, got {len(registry)}"
    for entry in registry.values():
        entry["event"].set()
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
    task = asyncio.create_task(callbacks._run_analysis_for_ticker(ctx, 1, 1, "AAPL"))

    # Wait for analysis to complete, then race cancel.
    while not completion_event.is_set():
        await asyncio.sleep(0.01)
    # At this point, to_thread has just returned. Set the cancel event.
    registry = ctx.chat_data.get("analysis_cancels") or {}
    for entry in registry.values():
        entry["event"].set()
        if entry.get("async_event"):
            entry["async_event"].set()

    result = await task
    # Either path is acceptable; what we're verifying is no slot leak.
    assert result in ("cancelled", "completed"), result
    sem = callbacks._get_run_semaphore()
    assert sem._value == callbacks.Config.MAX_CONCURRENT_ANALYSES, (
        f"slot leaked after race: {sem._value}"
    )


# --- Runner ----------------------------------------------------------------


TESTS = [
    ("basic queue (10 tickers, cap=5)", test_basic_queue),
    ("cancel running → queued promotes", test_cancel_running_promotes_queued),
    ("cancel queued → no slot taken", test_cancel_queued_never_takes_slot),
    ("multi-cancel burst (3 simultaneous)", test_multi_cancel_burst),
    ("pool reuse across batches (no rebuild)", test_pool_reuse_no_rebuild),
    ("send_photo retry succeeds", test_send_photo_retry_succeeds),
    ("send_photo failure cleanup", test_send_photo_failure_cleanup),
    ("propagate raises non-cancel exception", test_propagate_raises_non_cancel),
    ("single ticker (no queueing)", test_single_ticker_no_queueing),
    ("graceful shutdown signals all", test_graceful_shutdown_signals_all),
    ("cancel post-completion race", test_cancel_post_completion_race),
]


async def main() -> None:
    # Silence the deliberate-exception trace from test_propagate_raises
    # — the tg_bot.handlers.callbacks logger emits a stacktrace via
    # logger.exception() which clutters test output.
    import logging

    logging.getLogger("tg_bot").setLevel(logging.CRITICAL)
    logging.getLogger("tg_bot.handlers.callbacks").setLevel(logging.CRITICAL)

    passed = 0
    failed = 0
    for name, fn in TESTS:
        sys.stdout.write(f"  {name:<48}")
        sys.stdout.flush()
        start = time.monotonic()
        try:
            await fn()
            elapsed = time.monotonic() - start
            print(f"✓ ({elapsed:.2f}s)")
            passed += 1
        except AssertionError as e:
            print(f"✗ FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ ERROR: {type(e).__name__}: {e}")
            failed += 1

    print()
    print(f"{passed}/{len(TESTS)} passed, {failed} failed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    print("=== smoke_concurrent: scenario coverage ===\n")
    asyncio.run(main())
    print("\nALL CHECKS PASSED ✅")
