"""Real-parallelism smoke test for the multi-ticker analysis flow.

Launches 5 tickers concurrently with cap=5 and verifies they truly run
in parallel (not serialized through a thread pool, lock, or the graph
pool's queue). Each fake propagate() does 1 second of (sleep) work and
records its actual start/end wall-clock window. After gather, we check:

  1. Total elapsed wall time ≈ one run's duration (not N×).
  2. All 5 (start, end) windows overlap — a single instant exists when
     every run was in flight.

Run with: .venv/bin/python3 scripts/smoke_runner_parallel.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

# Match cap to ticker count so no runs are queued — we want all 5 to
# enter the work phase simultaneously.
os.environ["TG_BOT_MAX_CONCURRENT_ANALYSES"] = "5"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from _smoke_helpers import FakeBot, FakeContext, set_smoke_data_dir  # noqa: E402

# NOTE: no module-level `set_smoke_data_dir` here. The only test in this
# file (`test_real_parallelism`) overrides TG_BOT_DATA_DIR inside its body
# anyway — see the comment there for why. A module-level call would just
# create an orphan tempdir that neither runner ever reads from.

from tg_bot.handlers import analysis_runner as runner  # noqa: E402


# How long each fake run takes (seconds). Big enough to make GIL/serialization
# obvious in the elapsed total, small enough to keep the test snappy.
RUN_DURATION = 1.0

# Per-ticker (start, end) windows in monotonic time. Filled by the fake
# analysis function from worker threads — guarded by a lock.
WINDOWS: dict[str, tuple[float, float]] = {}
_WINDOWS_LOCK = threading.Lock()


def fake_busy_analysis(ticker, user_id, user_config_storage, reporter=None, **kw):
    """Sleeps for RUN_DURATION (releases GIL), records window."""
    start = time.monotonic()
    time.sleep(RUN_DURATION)
    end = time.monotonic()
    with _WINDOWS_LOCK:
        WINDOWS[ticker] = (start, end)
    return ({"final_trade_decision": f"Decision for {ticker}: HOLD"}, "HOLD")


async def fake_publish(title, content, edit_path=None):
    # `edit_path` is the /refresh code path's "update in place" hook; the
    # real `publish_to_telegraph` accepts it (None on fresh runs), so the
    # fake must too — otherwise the call site raises TypeError and the
    # analysis branches into the catch-all error path. Surfaced when
    # pytest captured stderr cleanly; bash was hiding it.
    return f"https://telegra.ph/{title.replace(' ', '-')}-test"


# --- Test driver -----------------------------------------------------------


async def test_real_parallelism() -> None:
    # Set the data dir AT TEST TIME, not at module-import time. Under
    # pytest, multiple smoke files set TG_BOT_DATA_DIR at import; the
    # env var ends up pointing at whichever set it LAST — so this test
    # would otherwise read a cache populated by smoke_runner for the
    # same AAPL/TSLA/NVDA/MSFT/GOOGL tickers, the cache short-circuits
    # before fake_busy_analysis runs, no per-ticker window gets recorded,
    # and the WINDOWS iteration raises KeyError. Repointing here gives
    # a guaranteed-empty cache under both pytest and standalone bash.
    set_smoke_data_dir("smoke_runner_parallel_test_")
    WINDOWS.clear()

    runner.run_trading_analysis = fake_busy_analysis
    runner.publish_to_telegraph = fake_publish
    runner.TRADINGAGENTS_AVAILABLE = True
    # Force cap=5 explicitly. Setting `runner._run_semaphore = None` and
    # letting the lazy-init read `Config.MAX_CONCURRENT_ANALYSES` works
    # when this file is run as a standalone script (the module-level
    # `os.environ["TG_BOT_MAX_CONCURRENT_ANALYSES"] = "5"` at line 25
    # fires before Config initializes). But under pytest, earlier test
    # files import tg_bot.config first — Config's class-body reads the
    # env var ONCE at that point, so the later os.environ change is a
    # no-op and the lazy-init builds a Semaphore(3) instead of (5), so
    # 2 tickers queue and never run before the assertions fire.
    runner._run_semaphore = asyncio.Semaphore(5)

    bot = FakeBot()
    context = FakeContext(bot)
    chat_id = 42
    user_id = 7

    tickers = ["AAPL", "TSLA", "NVDA", "MSFT", "GOOGL"]

    print(f"=== Launching {len(tickers)} tickers in parallel (cap=5) ===")
    launched_at = time.monotonic()
    tasks = {
        t: asyncio.create_task(
            runner._run_analysis_for_ticker(context, chat_id, user_id, t)
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
        f"unexpected results: {dict(zip(tickers, results, strict=True))}"
    )
    print(f"  ✓ all {len(tickers)} reached 'completed'")

    print("\nALL CHECKS PASSED ✅")


if __name__ == "__main__":
    # Backwards-compat entry point so `bash scripts/run_smoke.sh` still
    # works alongside `pytest scripts/`. Pytest discovers
    # `test_real_parallelism` directly.
    asyncio.run(test_real_parallelism())
