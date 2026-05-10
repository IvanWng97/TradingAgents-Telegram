"""Real-parallelism smoke test for the multi-ticker analysis flow.

Launches 5 tickers concurrently with cap=5 and verifies they truly run
in parallel (not serialized through a thread pool, lock, or the graph
pool's queue). Each fake propagate() does 1 second of (sleep) work and
records its actual start/end wall-clock window. After gather, we check:

  1. Total elapsed wall time ≈ one run's duration (not N×).
  2. All 5 (start, end) windows overlap — a single instant exists when
     every run was in flight.

Run with: .venv/bin/python3 scripts/smoke_parallel.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

# Match cap to ticker count so no runs are queued — we want all 5 to
# enter the work phase simultaneously.
os.environ["TG_BOT_MAX_CONCURRENT_ANALYSES"] = "5"
# Isolate from any real on-disk cache: the same-day result cache would
# short-circuit our fake_busy_analysis (no LLM call, no WINDOWS entry,
# `KeyError` later). A fresh tempdir guarantees every lookup misses.
os.environ["TG_BOT_DATA_DIR"] = tempfile.mkdtemp(prefix="smoke_parallel_")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tg_bot.handlers import callbacks  # noqa: E402


# How long each fake run takes (seconds). Big enough to make GIL/serialization
# obvious in the elapsed total, small enough to keep the test snappy.
RUN_DURATION = 1.0

# Per-ticker (start, end) windows in monotonic time. Filled by the fake
# analysis function from worker threads — guarded by a lock.
WINDOWS: dict[str, tuple[float, float]] = {}
_WINDOWS_LOCK = threading.Lock()


# --- Mocks (same shape as smoke_concurrent.py) -----------------------------


class FakeBot:
    def __init__(self) -> None:
        self.captions: dict[int, str] = {}
        self._next_id = 1000
        self._lock = asyncio.Lock()

    async def send_photo(self, chat_id, photo, caption, parse_mode, reply_markup):
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


class FakeContext:
    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot
        self.chat_data: dict = {}
        self.bot_data: dict = {}


def fake_busy_analysis(ticker, user_id, user_config_storage, reporter=None, **kw):
    """Sleeps for RUN_DURATION (releases GIL), records window."""
    start = time.monotonic()
    time.sleep(RUN_DURATION)
    end = time.monotonic()
    with _WINDOWS_LOCK:
        WINDOWS[ticker] = (start, end)
    return ({"final_trade_decision": f"Decision for {ticker}: HOLD"}, "HOLD")


async def fake_publish(title, content):
    return f"https://telegra.ph/{title.replace(' ', '-')}-test"


# --- Test driver -----------------------------------------------------------


async def run() -> None:
    callbacks.run_trading_analysis = fake_busy_analysis
    callbacks.publish_to_telegraph = fake_publish
    callbacks.TRADINGAGENTS_AVAILABLE = True
    callbacks._run_semaphore = None  # reset lazy-init

    bot = FakeBot()
    context = FakeContext(bot)
    chat_id = 42
    user_id = 7

    tickers = ["AAPL", "TSLA", "NVDA", "MSFT", "GOOGL"]

    print(f"=== Launching {len(tickers)} tickers in parallel (cap=5) ===")
    launched_at = time.monotonic()
    tasks = {
        t: asyncio.create_task(
            callbacks._run_analysis_for_ticker(context, chat_id, user_id, t)
        )
        for t in tickers
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    total_elapsed = time.monotonic() - launched_at

    print(f"\nTotal wall time: {total_elapsed:.2f}s")
    print(f"Per-run duration: {RUN_DURATION}s")
    print(f"Serial would take: {len(tickers) * RUN_DURATION}s")
    print()

    print("=== Per-ticker (start → end), relative to launch ===")
    for t in tickers:
        start, end = WINDOWS[t]
        rel_s = start - launched_at
        rel_e = end - launched_at
        print(f"  {t}: {rel_s:.3f}s → {rel_e:.3f}s  (duration {end - start:.3f}s)")

    # ── Assertion 1: total elapsed close to one run, not N ────────────────
    # Allow ~50% overhead to be safe (GIL contention during builds, asyncio
    # scheduling jitter). Definitely should not be near N × RUN_DURATION.
    overhead_budget = RUN_DURATION * 1.5
    print("\n=== Checks ===")
    print(f"  total elapsed {total_elapsed:.2f}s vs budget {overhead_budget:.2f}s")
    assert total_elapsed < overhead_budget, (
        f"elapsed {total_elapsed:.2f}s suggests serial execution "
        f"(would be ~{len(tickers) * RUN_DURATION}s); expected ≤ {overhead_budget:.2f}s"
    )
    print("  ✓ wall time consistent with parallel execution")

    # ── Assertion 2: all 5 windows overlap ────────────────────────────────
    # A moment of true 5-way overlap exists iff max(start) < min(end).
    max_start = max(s for s, _ in WINDOWS.values())
    min_end = min(e for _, e in WINDOWS.values())
    overlap_window = min_end - max_start
    print(
        f"  overlap window: max_start={max_start - launched_at:.3f}s, "
        f"min_end={min_end - launched_at:.3f}s, overlap={overlap_window:.3f}s"
    )
    assert overlap_window > 0, (
        f"no instant of 5-way overlap — runs serialized "
        f"(max_start={max_start:.3f}, min_end={min_end:.3f})"
    )
    print(f"  ✓ all 5 runs were simultaneously in-flight for {overlap_window:.3f}s")

    # ── Assertion 3: every run completed successfully ────────────────────
    assert all(r == "completed" for r in results), (
        f"unexpected results: {dict(zip(tickers, results))}"
    )
    print(f"  ✓ all {len(tickers)} reached 'completed'")

    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    asyncio.run(run())
