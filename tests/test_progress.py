"""Tests for `pipeline/progress.py` — the LangChain → Telegram dispatch.

Pins the diagnostic logging behavior added after a VPS deploy showed
progress captions wedging on the first step. The silent-failure surfaces
that masked the wedge (no-reporter drop, no-langgraph_node drop, swallowed
edit failure, unknown-node fallback) all log loudly now; these tests
ensure a future "tidy the logs" refactor doesn't silence them again.

Also pins `resolve_step`'s upstream-rename canary: an unknown node name
emits a WARNING listing the expected node-name vocabulary, so a future
tradingagents upgrade that renames `social_analyst` → `sentiment_analyst`
surfaces in the bot logs instead of silently dropping the ordinal badge.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_bot.pipeline.progress import (  # noqa: E402
    TOTAL_STEPS,
    CancelledByUserError,
    ProgressReporter,
    _DelegatingProgressCallback,
    _STEP_MAP,
    resolve_step,
    set_reporter,
)


# ─── _STEP_MAP upstream alignment ───────────────────────────────────────


# Snapshot of every node `tradingagents/graph/setup.py` registers via
# `workflow.add_node(...)` that fires an LLM call (and therefore reaches
# our callback). Verified against v0.2.5 by reading the source — the four
# analyst names are built dynamically (`f"{analyst_type.capitalize()} Analyst"`
# with `analyst_type` in {"market", "social", "news", "fundamentals"}), the
# rest are hardcoded.
#
# Excluded: `Msg Clear *` (state-clearing nodes, no LLM call) and `tools_*`
# (tool-execution nodes, no LLM call). These never reach our progress
# callback so we don't need entries for them.
#
# Bump this set in lockstep with `_STEP_MAP` when upgrading tradingagents —
# the alignment test below catches drift in either direction.
_UPSTREAM_LLM_NODE_NAMES_V025 = {
    "Market Analyst",
    "Social Analyst",
    "News Analyst",
    "Fundamentals Analyst",
    "Bull Researcher",
    "Bear Researcher",
    "Research Manager",
    "Trader",
    "Aggressive Analyst",
    "Neutral Analyst",
    "Conservative Analyst",
    "Portfolio Manager",
}


def test_step_map_covers_all_upstream_v025_nodes() -> None:
    """Every node name tradingagents v0.2.5 emits via `add_node()` must
    resolve in our `_STEP_MAP` with a valid ordinal — otherwise the
    `(n/12)` badge silently disappears for that step (user-visible
    regression, exactly the VPS issue this test was added to prevent).

    When tradingagents is upgraded, walk `setup.py`'s `add_node()` calls
    and update both this set AND `_STEP_MAP` together. The new WARN log
    in `resolve_step` is the runtime canary; this test is the build-time
    canary."""
    missing: dict[str, str] = {}
    for node_name in _UPSTREAM_LLM_NODE_NAMES_V025:
        friendly, ordinal = resolve_step(node_name)
        if ordinal is None:
            missing[node_name] = friendly
    assert not missing, (
        f"Upstream tradingagents v0.2.5 emits these node names that are "
        f"NOT in `_STEP_MAP` (badge will disappear for these steps): "
        f"{missing}. Add them to `_STEP_MAP` in progress.py."
    )


def test_total_steps_derived_from_step_map_max_ordinal() -> None:
    """`TOTAL_STEPS` is the badge denominator (`n/12`). Pinned as the max
    ordinal in `_STEP_MAP` so adding a future node at ordinal 13 auto-
    updates the badge to `(n/13)` everywhere — no separate constant to
    forget to bump."""
    expected = max(ordinal for _name, ordinal in _STEP_MAP.values())
    assert TOTAL_STEPS == expected, (
        f"TOTAL_STEPS={TOTAL_STEPS} but max(_STEP_MAP ordinals)={expected}. "
        "Restore the `max(...)` derivation in progress.py — hardcoding "
        "TOTAL_STEPS lets the badge drift when a future node is added."
    )


def test_step_map_sentiment_analyst_alias_resolves_to_social() -> None:
    """Upstream issue #557 renamed `create_social_analyst` →
    `create_sentiment_analyst`. v0.2.5 keeps the node name `"Social Analyst"`
    for back-compat, but newer pip-from-git installs (observed on user's
    VPS) emit `"Sentiment Analyst"` directly. Both must resolve to step 2
    so the badge survives the upstream switch."""
    social_friendly, social_ord = resolve_step("social_analyst")
    sentiment_friendly, sentiment_ord = resolve_step("sentiment_analyst")
    assert social_ord == sentiment_ord == 2
    # Display name stays "Social Analyst" for both (canonical UI string —
    # we don't want the caption flipping between "Social" and "Sentiment"
    # depending on which tradingagents version the VPS happens to install).
    assert social_friendly == sentiment_friendly == "Social Analyst"


# ─── resolve_step ────────────────────────────────────────────────────────


def test_resolve_step_known_node_returns_friendly_and_ordinal() -> None:
    """Known node → (friendly, ordinal). Pinned so a future map shuffle
    doesn't silently drift the (n/12) badge."""
    friendly, ordinal = resolve_step("market_analyst")
    assert friendly == "Market Analyst"
    assert ordinal == 1


def test_resolve_step_tools_suffix_resolves_to_base_node() -> None:
    """`news_analyst tools` → `news analyst` ordinal. Lets langgraph tool
    sub-nodes share the parent's badge."""
    friendly, ordinal = resolve_step("news_analyst tools")
    assert friendly == "News Analyst"
    assert ordinal == 3


def test_resolve_step_unknown_node_logs_warning_with_vocabulary(
    caplog,
) -> None:
    """An unknown node name (upstream rename or new node) must log WARN
    listing the expected vocabulary — surfaces tradingagents version drift
    without requiring DEBUG logging in production. Symptom of the missing
    warning: ordinal badge silently disappears, friendly name is title-cased
    instead of canonical, no log to explain why.

    Uses a fictitious node `macro_economist` as a stand-in — `sentiment_analyst`
    would be tempting but it's already a known alias for step 2 (upstream
    issue #557 rename — see `test_step_map_sentiment_analyst_alias_resolves_to_social`)."""
    with caplog.at_level(logging.WARNING, logger="tg_bot.pipeline.progress"):
        friendly, ordinal = resolve_step("macro_economist")
    assert ordinal is None  # fallback path
    assert friendly == "Macro Economist"
    # The WARN must include both the unknown node name AND the expected
    # vocabulary — without the vocabulary the operator can't tell if it's
    # a rename or a brand-new node.
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, [r.getMessage() for r in caplog.records]
    msg = warns[0].getMessage()
    assert "macro_economist" in msg
    assert "market analyst" in msg, "vocabulary list missing from warning"


# ─── _DelegatingProgressCallback._dispatch ──────────────────────────────


def test_dispatch_no_reporter_logs_warning(caplog) -> None:
    """The callback singleton lives on every cached graph; when a sub-thread
    spawned by a langgraph node fires an LLM event, there's no reporter on
    the thread's threadlocal. Was previously a silent DEBUG drop — bumped
    to WARN so the failure mode is visible at production log levels."""
    set_reporter(None)  # ensure clean threadlocal
    cb = _DelegatingProgressCallback()
    with caplog.at_level(logging.WARNING, logger="tg_bot.pipeline.progress"):
        cb._dispatch({"metadata": {"langgraph_node": "market_analyst"}})
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, [r.getMessage() for r in caplog.records]
    assert "no reporter" in warns[0].getMessage()


def test_dispatch_missing_langgraph_node_logs_info(caplog) -> None:
    """An LLM event without `metadata['langgraph_node']` (LLM client init,
    auxiliary call outside any langgraph node) was previously silent. Bumped
    to INFO so we can confirm callbacks are firing at all — the VPS wedge
    looked identical to "no callbacks fired" before this surfaced."""
    bot = MagicMock()
    loop = asyncio.new_event_loop()
    try:
        reporter = ProgressReporter(
            bot=bot, chat_id=1, message_id=2, ticker="TEST", loop=loop
        )
        set_reporter(reporter)
        cb = _DelegatingProgressCallback()
        with caplog.at_level(logging.INFO, logger="tg_bot.pipeline.progress"):
            cb._dispatch({"metadata": {"something_else": "x"}})
        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and "langgraph_node" in r.getMessage()
        ]
        assert len(infos) == 1, [r.getMessage() for r in caplog.records]
        msg = infos[0].getMessage()
        assert "TEST" in msg
        assert "something_else" in msg, "metadata keys missing from log"
    finally:
        set_reporter(None)
        loop.close()


def test_dispatch_with_node_name_logs_info_and_schedules_report() -> None:
    """Happy path: reporter set, metadata['langgraph_node'] present —
    `_dispatch` should log INFO and schedule `reporter.report()` on the
    reporter's loop. The schedule call is what eventually edits the
    Telegram caption."""
    bot = MagicMock()
    loop = asyncio.new_event_loop()
    scheduled: list[str] = []

    async def _capture(node_name: str) -> None:
        scheduled.append(node_name)

    try:
        reporter = ProgressReporter(
            bot=bot, chat_id=1, message_id=2, ticker="TEST", loop=loop
        )
        # Swap report for a capture; what we're testing is _dispatch's
        # decision to call it, not report's own logic.
        reporter.report = _capture  # type: ignore[assignment]
        set_reporter(reporter)
        cb = _DelegatingProgressCallback()
        cb._dispatch({"metadata": {"langgraph_node": "market_analyst"}})
        # Run the loop once so the scheduled coroutine fires.
        loop.run_until_complete(asyncio.sleep(0))
        assert scheduled == ["market_analyst"]
    finally:
        set_reporter(None)
        loop.close()


def test_dispatch_raises_cancelled_when_event_set() -> None:
    """Invariant #2 (root): `raise_error=True` on the callback class is
    load-bearing — the exception must propagate up through langgraph to
    abort the in-flight LLM call. Pinned by checking the raise here."""
    bot = MagicMock()
    loop = asyncio.new_event_loop()
    cancel_event = threading.Event()
    cancel_event.set()
    try:
        reporter = ProgressReporter(
            bot=bot,
            chat_id=1,
            message_id=2,
            ticker="TEST",
            loop=loop,
            cancel_event=cancel_event,
        )
        set_reporter(reporter)
        cb = _DelegatingProgressCallback()
        try:
            cb._dispatch({"metadata": {"langgraph_node": "market_analyst"}})
        except CancelledByUserError:
            pass
        else:
            raise AssertionError("expected CancelledByUserError")
    finally:
        set_reporter(None)
        loop.close()


def test_dispatch_logs_warning_when_loop_is_closed(caplog) -> None:
    """Loop closed before event fires (analysis outlived the chat) — must
    log WARNING, not raise and not silently swallow. Was previously a
    bare `pass` (silent swallow); this PR bumped it to WARN so an
    outlived-chat race surfaces in production logs instead of producing
    invisible event loss."""
    bot = MagicMock()
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    try:
        reporter = ProgressReporter(
            bot=bot, chat_id=1, message_id=2, ticker="TEST", loop=closed_loop
        )
        set_reporter(reporter)
        cb = _DelegatingProgressCallback()
        with caplog.at_level(logging.WARNING, logger="tg_bot.pipeline.progress"):
            cb._dispatch({"metadata": {"langgraph_node": "market_analyst"}})
        warns = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "run_coroutine_threadsafe failed" in r.getMessage()
        ]
        assert len(warns) == 1, [r.getMessage() for r in caplog.records]
        msg = warns[0].getMessage()
        assert "TEST" in msg
        assert "market_analyst" in msg
    finally:
        set_reporter(None)


# ─── ProgressReporter.report ────────────────────────────────────────────


async def test_report_logs_info_on_successful_caption_edit(caplog) -> None:
    """Successful caption edit must log INFO with ticker + node + ordinal.
    This is the operator's primary "progress is alive" signal during a
    wedged-analysis investigation."""
    bot = AsyncMock()
    loop = asyncio.get_event_loop()
    reporter = ProgressReporter(
        bot=bot, chat_id=1, message_id=2, ticker="NVDA", loop=loop
    )
    with caplog.at_level(logging.INFO, logger="tg_bot.pipeline.progress"):
        await reporter.report("market_analyst")
    bot.edit_message_caption.assert_called_once()
    infos = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "caption updated" in r.getMessage()
    ]
    assert len(infos) == 1, [r.getMessage() for r in caplog.records]
    msg = infos[0].getMessage()
    assert "NVDA" in msg
    assert "market_analyst" in msg
    assert "1/12" in msg, "ordinal badge missing from success log"


async def test_report_logs_warning_on_caption_edit_failure(caplog) -> None:
    """Edit failure was previously DEBUG (invisible at production levels).
    Now WARN — surfaces Telegram BadRequest / rate-limit / network errors
    that previously masked a wedged progress surface."""
    bot = AsyncMock()
    bot.edit_message_caption.side_effect = RuntimeError("Bad Request: ...")
    loop = asyncio.get_event_loop()
    reporter = ProgressReporter(
        bot=bot, chat_id=1, message_id=2, ticker="NVDA", loop=loop
    )
    with caplog.at_level(logging.WARNING, logger="tg_bot.pipeline.progress"):
        await reporter.report("market_analyst")
    warns = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "FAILED" in r.getMessage()
    ]
    assert len(warns) == 1, [r.getMessage() for r in caplog.records]
    msg = warns[0].getMessage()
    assert "NVDA" in msg
    assert "RuntimeError" in msg, "exception type missing from WARN log"
    assert "Bad Request" in msg, "exception message missing from WARN log"


async def test_report_coalesces_duplicate_node_names() -> None:
    """A node firing multiple LLM calls only updates the caption on the
    first one (`_last_step` short-circuit). Pinned so a future eager-edit
    refactor doesn't blow Telegram's per-chat edit rate limit."""
    bot = AsyncMock()
    loop = asyncio.get_event_loop()
    reporter = ProgressReporter(
        bot=bot, chat_id=1, message_id=2, ticker="NVDA", loop=loop
    )
    await reporter.report("market_analyst")
    await reporter.report("market_analyst")
    await reporter.report("market_analyst")
    assert bot.edit_message_caption.call_count == 1


# ─── Token usage accumulation ─────────────────────────────────────────────


async def test_token_extraction_handles_openai_shape() -> None:
    """OpenAI's LLMResult.llm_output['token_usage'] uses prompt_tokens /
    completion_tokens. Pin the extraction across provider naming."""
    from types import SimpleNamespace

    from tg_bot.pipeline.progress import _extract_token_usage

    response = SimpleNamespace(
        llm_output={"token_usage": {"prompt_tokens": 1234, "completion_tokens": 567}},
        generations=[],
    )
    usage = _extract_token_usage(response)
    assert usage == (1234, 567), usage


async def test_token_extraction_handles_anthropic_shape() -> None:
    """Anthropic uses input_tokens / output_tokens — the other naming.
    Both must round-trip through `_extract_token_usage` so the bot-wide
    counter accumulates correctly regardless of which provider runs."""
    from types import SimpleNamespace

    from tg_bot.pipeline.progress import _extract_token_usage

    response = SimpleNamespace(
        llm_output={"token_usage": {"input_tokens": 999, "output_tokens": 333}},
        generations=[],
    )
    assert _extract_token_usage(response) == (999, 333)


async def test_token_extraction_walks_generation_info_fallback() -> None:
    """Some providers/integrations stash usage under generation_info per
    Generation, not at the top-level llm_output. Pin the fallback path."""
    from types import SimpleNamespace

    from tg_bot.pipeline.progress import _extract_token_usage

    gen = SimpleNamespace(
        generation_info={"token_usage": {"prompt_tokens": 50, "completion_tokens": 25}},
    )
    response = SimpleNamespace(
        llm_output=None,
        generations=[[gen]],
    )
    assert _extract_token_usage(response) == (50, 25)


async def test_token_extraction_returns_none_on_missing_usage() -> None:
    """No usage data → None, not (0, 0). Lets the accumulator skip the
    update rather than artificially log a zero call (which would look
    like a leak in /status)."""
    from types import SimpleNamespace

    from tg_bot.pipeline.progress import _extract_token_usage

    response = SimpleNamespace(llm_output={}, generations=[])
    assert _extract_token_usage(response) is None
    response = SimpleNamespace(llm_output=None, generations=None)
    assert _extract_token_usage(response) is None


async def test_token_accumulator_is_thread_safe() -> None:
    """`_add_token_usage` runs on analysis worker threads; `get_token_totals`
    reads on the asyncio loop. Pin that concurrent writes don't drop
    updates — a torn read would surface as silently-wrong /status output."""
    import threading

    from tg_bot.pipeline import progress as p

    # Snapshot starting state so other tests' writes don't pollute.
    with p._token_counter_lock:
        p._total_input_tokens = 0
        p._total_output_tokens = 0

    def worker():
        for _ in range(1000):
            p._add_token_usage(1, 2)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    in_total, out_total = p.get_token_totals()
    # 4 threads × 1000 iters × (1, 2) = (4000, 8000).
    assert in_total == 4000, in_total
    assert out_total == 8000, out_total


async def test_on_llm_end_accumulates_into_bot_wide_totals() -> None:
    """End-to-end pin: `_DelegatingProgressCallback.on_llm_end` extracts
    usage from a response and adds it to the bot-wide counter."""
    from types import SimpleNamespace

    from tg_bot.pipeline import progress as p

    with p._token_counter_lock:
        p._total_input_tokens = 0
        p._total_output_tokens = 0

    cb = p._DelegatingProgressCallback()
    response = SimpleNamespace(
        llm_output={"token_usage": {"prompt_tokens": 700, "completion_tokens": 300}},
        generations=[],
    )
    cb.on_llm_end(response)

    in_total, out_total = p.get_token_totals()
    assert in_total == 700
    assert out_total == 300


# ─── estimate_token_cost_usd ──────────────────────────────────────────────


async def test_estimate_cost_returns_dollar_amount_for_known_model() -> None:
    """For a (provider, model) in the price table, the function must
    return a float dollar amount. 1M input tokens of gpt-4o = $2.50."""
    from tg_bot.pipeline.analysis import estimate_token_cost_usd

    cost = estimate_token_cost_usd("openai", "gpt-4o", "gpt-4o-mini", 1_000_000, 0)
    # 50/50 split between gpt-4o (input $2.50/M) and gpt-4o-mini (input
    # $0.15/M) → average $1.325/M. 1M tokens × $1.325/M = $1.325.
    assert 1.30 < cost < 1.35, cost


async def test_estimate_cost_returns_none_for_unknown_provider() -> None:
    """No price entry for either model → None. The /status renderer
    falls back to tokens-only rendering instead of inventing a number."""
    from tg_bot.pipeline.analysis import estimate_token_cost_usd

    cost = estimate_token_cost_usd("unknown-llm", "ghost-1", "ghost-2", 1000, 500)
    assert cost is None
