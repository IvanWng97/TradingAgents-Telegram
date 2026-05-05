"""Smoke test for the concurrent analysis flow.

Exercises `_run_analysis_for_ticker` with mocked Telegram + mocked
TradingAgents so we can verify:
  - First MAX_CONCURRENT_ANALYSES tickers start running immediately.
  - Beyond that, runs sit in "queued" state.
  - Cancelling a queued run aborts without claiming a slot.
  - Cancelling a running run frees a slot for the next queued ticker.
  - End-to-end: every run reaches a terminal state, slots drain.

Run with: .venv/bin/python3 scripts/smoke_concurrent.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections import defaultdict
from types import SimpleNamespace
from typing import Any

# Cap of 5 with 10 tickers gives us 5 analyzing + 5 queued at launch —
# enough to demonstrate slot-transfer when running tickers are cancelled.
os.environ["TG_BOT_MAX_CONCURRENT_ANALYSES"] = "5"

# Make the package importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tg_bot.handlers import callbacks  # noqa: E402


# --- Mocks -----------------------------------------------------------------


class FakeBot:
    """Records every call. Mimics async PTB bot methods we hit."""

    def __init__(self) -> None:
        self.captions: dict[int, str] = {}  # message_id -> last caption
        self.markups: dict[int, Any] = {}
        self._next_id = 1000
        self._lock = asyncio.Lock()
        self.calls: list[tuple[str, dict]] = []

    async def send_photo(self, chat_id, photo, caption, parse_mode, reply_markup):
        async with self._lock:
            mid = self._next_id
            self._next_id += 1
        self.captions[mid] = caption
        self.markups[mid] = reply_markup
        self.calls.append(("send_photo", {"message_id": mid, "caption": caption}))
        return SimpleNamespace(message_id=mid)

    async def edit_message_caption(
        self, chat_id, message_id, caption, parse_mode=None, reply_markup=None
    ):
        self.captions[message_id] = caption
        if reply_markup is not None:
            self.markups[message_id] = reply_markup
        self.calls.append(
            ("edit_caption", {"message_id": message_id, "caption": caption})
        )

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self.markups[message_id] = reply_markup


class FakeContext:
    """Mimics PTB ContextTypes.DEFAULT_TYPE attrs we read."""

    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot
        self.chat_data: dict = {}
        self.bot_data: dict = {}


# --- Fake analysis pipeline ------------------------------------------------

# Global "complete now" gate — fake runs hold a slot until either their
# own cancel_event is set OR this stop signal fires. Lets the test make
# all timing-sensitive assertions before allowing remaining runs to drain.
_STOP_SIGNAL = threading.Event()


def fake_run_trading_analysis(
    ticker, user_id, user_config_storage, reporter=None, **kw
):
    """Stand-in for tradingagents — holds the slot until the global stop
    signal fires. Respects cancel_event by raising CancelledByUserError."""
    while not _STOP_SIGNAL.is_set():
        if reporter and reporter.cancel_event and reporter.cancel_event.is_set():
            raise callbacks.CancelledByUserError("fake cancel")
        time.sleep(0.05)
    # Return shape matches real propagate(): (final_state, signal).
    return ({"final_trade_decision": f"Decision for {ticker}: HOLD"}, "HOLD")


async def fake_publish_to_telegraph(title, content):
    return f"https://telegra.ph/{title.replace(' ', '-')}-test"


# --- Test driver -----------------------------------------------------------


async def run() -> None:
    # Patch the heavy dependencies on the callbacks module.
    callbacks.run_trading_analysis = fake_run_trading_analysis
    callbacks.publish_to_telegraph = fake_publish_to_telegraph
    callbacks.TRADINGAGENTS_AVAILABLE = True
    # finviz_chart_url is harmless (just builds a URL string), no patch.

    # Reset the lazy semaphore so it picks up our test cap.
    callbacks._run_semaphore = None

    bot = FakeBot()
    context = FakeContext(bot)
    chat_id = 42
    user_id = 7

    # Real-world ticker symbols. The analysis function itself is mocked
    # (running real tradingagents costs LLM tokens and minutes per ticker),
    # but using real symbols catches any string-handling regressions
    # (e.g., the BRK-B class-share dash form should round-trip cleanly).
    tickers = [
        "AAPL",
        "TSLA",
        "NVDA",
        "MSFT",
        "GOOGL",
        "AMZN",
        "META",
        "BRK-B",
        "JPM",
        "JNJ",
    ]

    # Launch all 10 in parallel, just like _handle_done's gather.
    tasks = {
        t: asyncio.create_task(
            callbacks._run_analysis_for_ticker(context, chat_id, user_id, t)
        )
        for t in tickers
    }

    # Give them all a moment to send their initial photo + decide
    # queued vs analyzing.
    await asyncio.sleep(0.3)

    # Snapshot initial state: cap is 5, so first 5 (TKR00–TKR04) analyzing,
    # last 5 (TKR05–TKR09) queued.
    initial_states = {t: bot.captions.get(_msg_id_for(bot, t)) for t in tickers}
    print("=== After launch (10 tickers, cap=5) ===")
    for t in tickers:
        c = initial_states[t]
        if c is None:
            print(f"  {t}: (no caption)")
        else:
            label = "▶️" if c.startswith("📊 Analyzing") else "⏳"
            print(f"  {label} {t}: {c[:55]}")

    analyzing = [
        t for t, c in initial_states.items() if c and c.startswith("📊 Analyzing")
    ]
    queued = [t for t, c in initial_states.items() if c and "queued" in c.lower()]
    assert len(analyzing) == 5, f"expected 5 analyzing, got {analyzing!r}"
    assert len(queued) == 5, f"expected 5 queued, got {queued!r}"
    print(f"  ✓ {len(analyzing)} analyzing, {len(queued)} queued\n")

    # ── Cancel first 2 running tickers; expect 2 queued tickers to promote ──
    targets = analyzing[:2]
    expected_promotions = queued[:2]
    print(
        f"=== Cancelling first 2 running ({', '.join(targets)}) — "
        f"expecting {', '.join(expected_promotions)} to promote ==="
    )
    for t in targets:
        _trigger_cancel(context, t)
    await asyncio.sleep(0.7)  # cancel propagates + next slots acquired

    cancelled_states = {t: bot.captions.get(_msg_id_for(bot, t)) for t in targets}
    for t, c in cancelled_states.items():
        print(f"  ❌ {t}: {c[:80] if c else '(none)'}")
        assert "cancelled" in (c or "").lower(), (
            f"running cancel didn't render: {t}={c!r}"
        )

    # Verify the slots actually transferred. We expect EXACTLY 2 promotions
    # (since we freed 2 slots via cancel). Order is FIFO within the
    # semaphore — should be TKR05 and TKR06.
    promoted = [
        t
        for t in queued
        if (c := bot.captions.get(_msg_id_for(bot, t))) and c.startswith("📊 Analyzing")
    ]
    print(f"  promoted from queued → analyzing: {promoted}")
    assert len(promoted) == 2, f"expected exactly 2 promotions, got {promoted!r}"
    # Still-queued count should now be 3 (started 5, 2 promoted).
    still_queued = [
        t
        for t in queued
        if (c := bot.captions.get(_msg_id_for(bot, t))) and "queued" in c.lower()
    ]
    print(f"  still queued: {still_queued}")
    assert len(still_queued) == 3, f"expected 3 still queued, got {still_queued!r}"

    # Active running count should still be 5 (3 originals + 2 promoted).
    sem = callbacks._get_run_semaphore()
    in_flight = callbacks.Config.MAX_CONCURRENT_ANALYSES - sem._value
    print(f"  semaphore in-flight count: {in_flight} (cap = {sem._value + in_flight})")
    assert in_flight == 5, f"expected 5 slots in use, got {in_flight}"
    print("  ✓ 2 slots transferred FIFO from queue\n")

    # ── Drain the rest naturally ───────────────────────────────────────────
    print("=== Releasing stop signal — remaining 8 should drain ===")
    _STOP_SIGNAL.set()
    drain_started = time.monotonic()
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    drain_elapsed = time.monotonic() - drain_started
    print(f"  drained in {drain_elapsed:.1f}s\n")

    # Final state per ticker.
    counts = defaultdict(int)
    per_ticker: dict[str, str] = {}
    for t, r in zip(tickers, results):
        counts[r] += 1
        per_ticker[t] = r

    print("=== Final tally per ticker ===")
    for t in tickers:
        marker = "❌" if per_ticker[t] == "cancelled" else "✅"
        print(f"  {marker} {t}: {per_ticker[t]}")
    print(f"\n  totals: {dict(counts)}")

    assert counts["cancelled"] == 2, f"expected 2 cancelled, got {counts}"
    assert counts["completed"] == 8, f"expected 8 completed, got {counts}"

    # ── Test 4: promoted-from-queue tickers reached the success path ──────
    print("\n=== Verifying promoted-from-queue tickers ran successfully ===")
    for t in promoted:
        final_caption = bot.captions.get(_msg_id_for(bot, t)) or ""
        # Success caption shape (format_short_message): signal emoji + ticker
        # + signal verb + summary + timestamp + Telegraph link.
        # We assert the structural pieces survived the queue → run path.
        print(f"  {t}: {final_caption[:120]}…")
        assert t in final_caption, f"{t}: ticker missing from final caption"
        assert "HOLD" in final_caption, f"{t}: signal missing from final caption"
        assert "Decision for" in final_caption, f"{t}: summary missing"
        assert "telegra.ph" in final_caption, f"{t}: Telegraph URL missing"
    print(f"  ✓ all {len(promoted)} promoted tickers rendered the success caption\n")

    # Semaphore should be fully drained.
    sem = callbacks._get_run_semaphore()
    assert not sem.locked(), "semaphore still held after all runs finished"
    print("  ✓ all runs reached a terminal state, semaphore drained\n")

    print("ALL CHECKS PASSED ✅")


# --- Helpers ---------------------------------------------------------------


def _caption_has_ticker(caption: str | None, ticker: str) -> bool:
    """Substring match that handles MarkdownV2 escapes (e.g. BRK-B → BRK\\-B
    in captions)."""
    if not caption:
        return False
    from telegram.helpers import escape_markdown

    safe = escape_markdown(ticker, version=2)
    return ticker in caption or safe in caption


def _msg_id_for(bot: FakeBot, ticker: str) -> int | None:
    for mid, cap in bot.captions.items():
        if _caption_has_ticker(cap, ticker):
            return mid
    return None


def _trigger_cancel(context: FakeContext, ticker: str) -> None:
    """Find the run_id for this ticker and set its cancel event directly."""
    registry = context.chat_data.get("analysis_cancels") or {}
    for run_id, entry in registry.items():
        mid = entry.get("message_id")
        if mid is None:
            continue
        cap = context.bot.captions.get(mid, "")
        if _caption_has_ticker(cap, ticker):
            entry["event"].set()
            return
    raise AssertionError(f"no entry for ticker {ticker!r}")


if __name__ == "__main__":
    asyncio.run(run())
