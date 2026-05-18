"""Analysis-execution pipeline + cancel + digest scheduling.

Split out of `handlers/callbacks.py` to keep that module focused on
inline-button routing (the `button_callback` dispatcher) and picker UI.
Public surface:

- `_run_analysis_for_ticker` — single-ticker analysis (used by `/watch`,
  `/refresh`, and the watchlist picker Done button)
- `run_user_digest`, `register_digest_job`, `cancel_digest_job` — daily
  digest fan-out + scheduling
- `_handle_get_full_md`, `_handle_cancel_analysis`, `_handle_digest_cancel`
  — callback-query handlers dispatched from `button_callback`
- `_full_report_keyboard` — keyboard builder reused by `/history`

Shared module state:
- `_run_semaphore` — global concurrency cap, lazy-bound to the running
  asyncio loop on first acquire.
"""

import asyncio
import logging
import threading
import time
import uuid
from datetime import datetime, time as dt_time, date, UTC
from html import escape as _html_escape
from io import BytesIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import markdown
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.error import Forbidden
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from tg_bot.pipeline import cache as result_cache
from tg_bot.pipeline.analysis import (
    TRADINGAGENTS_AVAILABLE,
    build_config,
    check_llm_configured,
    llm_setup_error_message,
    run_trading_analysis,
)
from tg_bot.config import Config
from tg_bot.pipeline.config_key import AnalysisConfigKey
from tg_bot.rendering.chart import finviz_chart_url
from tg_bot.rendering.formatters import (
    DECISION_EMOJI,
    caption_summary,
    format_analysis_result_markdown,
    format_full_md_report,
    format_short_message,
)
from tg_bot.history import load_historical_state
from tg_bot.market_calendar import is_market_open_for
from tg_bot.pipeline.progress import (
    TOTAL_STEPS,
    CancelledByUserError,
    ProgressReporter,
    resolve_step,
)
from tg_bot.storage import user_config_storage, watchlist_storage
from tg_bot.rendering.email_client import is_email_configured, send_digest_email
from tg_bot.rendering.telegraph_client import (
    _path_from_telegraph_url,
    publish_to_telegraph,
)


logger = logging.getLogger(__name__)


# Bounds total concurrent analyses across the whole bot. Acts as a
# coroutine-level FIFO queue (waiters are served in arrival order).
# Lazy-init on first use so we bind it to the running loop, not the
# module-import loop.
_run_semaphore: "asyncio.Semaphore | None" = None


def _get_run_semaphore() -> asyncio.Semaphore:
    global _run_semaphore
    if _run_semaphore is None:
        _run_semaphore = asyncio.Semaphore(Config.MAX_CONCURRENT_ANALYSES)
    return _run_semaphore


def _full_report_keyboard(
    ticker: str, date_iso: str, telegraph_url: str | None = None
) -> InlineKeyboardMarkup:
    """Two-button inline keyboard for the analysis output.

    Row 1: `📰 Instant View` (URL → Telegraph IV — usually all 7 sections,
           but the 40K HTML budget can drop table-heavy sections like
           Fundamentals/Market on long analyses) + `📥 Download Full Report`
           (callback → `getmd:<TICKER>:<DATE>` → unbounded `.md`, all 7
           sections, no cap — the canonical complete archive).

    Label asymmetry is deliberate: "Instant View" keeps Telegraph's
    in-app branding for the convenient view; "Full Report" signals the
    .md is the guaranteed-complete artifact when Telegraph truncates.

    The IV button is omitted when `telegraph_url` is None (publish
    failure). `getmd:` payload stays well under Telegram's 64-byte
    callback_data cap (max ~25 bytes for sane ticker + ISO date)."""
    buttons: list[InlineKeyboardButton] = []
    if telegraph_url:
        buttons.append(InlineKeyboardButton("📰 Instant View", url=telegraph_url))
    buttons.append(
        InlineKeyboardButton(
            "📥 Download Full Report",
            callback_data=f"getmd:{ticker}:{date_iso}",
        )
    )
    return InlineKeyboardMarkup([buttons])


async def _handle_get_full_md(
    query, context: ContextTypes.DEFAULT_TYPE, ticker: str, date_str: str
) -> None:
    """Build and send the full `.md` report for (ticker, date) on demand.

    Sources `final_state` from tradingagents' on-disk logs (durable, kept
    forever) — not from `result_cache`, which is same-day only and would
    miss for older photo+caption messages. Sends as a reply to the photo
    so the pairing stays visually anchored. Falls back to a brief inline
    text when the log file is missing (e.g. ephemeral Docker filesystem
    after restart without TRADINGAGENTS_RESULTS_DIR persistence)."""
    state = load_historical_state(ticker, date_str)
    if state is None:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"📥 Markdown report unavailable for {ticker} on {date_str}.",
            reply_to_message_id=query.message.message_id,
            allow_sending_without_reply=True,
            disable_notification=True,
        )
        return
    try:
        gen_date = date.fromisoformat(date_str)
    except ValueError:
        gen_date = None
    try:
        md = format_full_md_report(ticker, state, generated_at=gen_date)
        doc = InputFile(BytesIO(md.encode("utf-8")), filename=f"{ticker}_{date_str}.md")
        await query.message.chat.send_document(
            document=doc,
            reply_to_message_id=query.message.message_id,
            allow_sending_without_reply=True,
            disable_notification=True,
        )
    except Exception as e:
        logger.warning("getmd: send_document(.md) failed for %s: %s", ticker, e)


def _try_acquire_nonblocking(sem: asyncio.Semaphore) -> bool:
    """Mimic asyncio.Semaphore.acquire()'s fast-path synchronously — safe
    in asyncio because coroutines are cooperative (no preemption between
    `if` and `_value -= 1`). Avoids the broken `wait_for(..., timeout=0)`
    pattern, which cancels the inner task before the event loop can run
    its fast-path, so it always reports failure.

    Reaches into `sem._value` / `sem._waiters` — CPython-private. Works on
    CPython 3.10-3.14 (verified). If a future CPython release reshapes
    those attributes the `AttributeError` branch conservatively returns
    False — the caller falls back to the slow blocking path (`sem.acquire()`),
    which still works correctly. The user sees a brief `⏳ Queued` caption
    instead of the snappier "Analyzing…", but no analysis is lost.
    """
    try:
        if not sem.locked() and (
            sem._waiters is None or all(w.cancelled() for w in sem._waiters)
        ):
            sem._value -= 1
            return True
    except AttributeError:
        # Private attrs shifted — degrade to blocking acquire path.
        return False
    return False


# Per-chat serialization of cancel-ack caption edits — a multi-cancel
# burst in one chat is paced through that chat's lock + last-edit-at so
# we don't overrun Telegram's per-chat edit limit (~1/sec). Different
# chats get independent locks: a cancel tap in chat A must NOT block a
# cancel tap in chat B, since Telegram's limit is per-chat, not bot-wide.
# Per-chat is the correct shape — a previous version used one global
# lock and silently delayed cross-chat cancel-acks by up to 1.1s.
#
# Lazy-create entries on first use so locks bind to the running loop,
# not the module-import loop. `asyncio.Lock()` at module-level deprecates
# under 3.10+ and raises in 3.12+ when no loop is running. Dicts grow
# unbounded with distinct chat_ids — bounded in practice by allowlist
# size; LRU eviction could be added if multi-tenancy scales.
_cancel_edit_locks: "dict[int, asyncio.Lock]" = {}
_last_cancel_edit_at: "dict[int, float]" = {}
_MIN_CANCEL_INTERVAL = 1.1  # safe margin under Telegram's per-chat limit


def _get_cancel_edit_lock(chat_id: int) -> asyncio.Lock:
    lock = _cancel_edit_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _cancel_edit_locks[chat_id] = lock
    return lock


async def _queued_cancel_edit(bot, chat_id: int, message_id: int) -> None:
    """Per-chat lock-serialized variant of `edit_message_caption` for
    cancel-ack — paces multi-cancel bursts within a single chat while
    keeping different chats independent."""
    async with _get_cancel_edit_lock(chat_id):
        now = time.monotonic()
        last_at = _last_cancel_edit_at.get(chat_id, 0.0)
        wait = _MIN_CANCEL_INTERVAL - (now - last_at)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption="❌ Cancelling… will stop after the current step finishes\\.",
                parse_mode="MarkdownV2",
                reply_markup=None,
            )
        except Exception as e:
            logger.warning("queued cancel-ack edit failed: %s", e)
        _last_cancel_edit_at[chat_id] = time.monotonic()


async def _run_analysis_for_ticker(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    ticker: str,
    force_refresh: bool = False,
) -> str:
    """Run a single analysis end-to-end. Sends its own progress photo +
    caption, registers a cancel event, and renders the final result.

    The graph instance comes from a pool — see analysis._get_or_create_pool.
    Pool serves both single-tap and parallel queue paths uniformly.

    `force_refresh=True` (set by `/refresh` callers) skips the same-day
    cache hit short-circuit so a fresh LLM run happens. We also pull the
    previously-cached `telegraph_url` first and pass its path to
    `publish_to_telegraph(edit_path=...)` so the Telegraph page is
    updated in place — same shareable URL, fresh content. Without this,
    `/refresh` creates a new page each time (URL drifts, old shares go
    stale, orphan pages accumulate).

    Returns one of:
    - "completed" — analysis finished (success or non-cancel error).
    - "cancelled" — user tapped Cancel mid-run.
    - "unavailable" — tradingagents module not loaded.
    """
    # INVARIANT — keep in sync with `_analyze_one_for_digest`. The two
    # analysis paths share three structural surfaces that must drift
    # together: (1) the race-close cancel-event checks after `to_thread`
    # and after Telegraph publish, (2) the `AnalysisConfigKey` cache-key
    # construction via `build_config → from_config`, (3) the Telegraph
    # title via `key.telegraph_title(ticker)`. If you change one path's
    # handling of any of these, change the other to match.
    #
    # /status counter — bumped only AFTER the user actually received
    # something (cache-hit photo OR fresh-run progress photo). A previous
    # version bumped at function entry, which inflated the count when
    # send_photo failed (Telegram network blip → silently undelivered)
    # since send_photo failure returns "completed" without surfacing.

    # Cache short-circuit: if an identical analysis already ran today
    # (same provider + deep + quick + rounds + effort + ticker, any user),
    # render directly from the cached final state and skip the LLM run,
    # the progress flow, the Telegraph round-trip, and the cancel plumbing.
    config = build_config()
    # Construct AnalysisConfigKey once per request — drives the cache
    # lookup/store, the caption "via" line, the Telegraph title, and
    # (transitively) the graph pool. Threading the instance avoids the
    # 3-4 separate `AnalysisConfigKey.from_config(config)` constructions
    # the codebase had before this PR.
    key = AnalysisConfigKey.from_config(config)
    today_iso = result_cache.today_iso()
    cached = result_cache.lookup(key, ticker, today_iso)
    # Capture the prior Telegraph URL for edit-in-place on refresh, then
    # let force_refresh suppress the cache-hit short-circuit below so a
    # fresh LLM run + Telegraph edit_page happens.
    prev_telegraph_url = (
        cached.get("telegraph_url") if (cached and force_refresh) else None
    )
    if cached and force_refresh:
        cached = None
    if cached:
        logger.info("[%s] result_cache HIT — skipping LLM run", ticker)
        chart_url = finviz_chart_url(ticker)
        # final_trade_decision is the source so the expandable prose
        # matches the signal badge by construction (both come from the
        # post-risk-debate synthesis). caption_summary strips the
        # redundant `**Final Trading Decision: <T>**` + `**Rating: <X>**`
        # boilerplate so the 700-char clip lands real content.
        summary = caption_summary(cached["final_state"])
        caption = format_short_message(
            ticker,
            cached["signal"],
            cached.get("telegraph_url"),
            summary=summary,
            config_summary=key.caption(),
            generated_at=result_cache.parse_generated_at(cached.get("generated_at")),
        )
        # `today_iso` is the cache lookup key AND matches tradingagents'
        # on-disk log filename (`full_states_log_<local-date>.json`).
        # Using the cached `generated_at` UTC stamp would drift a day
        # for late-night runs from negative-UTC zones (e.g. 23:00 PDT →
        # 06:00 UTC next day → button looks for tomorrow's log → 404).
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=chart_url,
                caption=caption,
                parse_mode="HTML",
                reply_markup=_full_report_keyboard(
                    ticker, today_iso, cached.get("telegraph_url")
                ),
            )
            # /status counter — bump only on successful delivery.
            context.bot_data["analysis_count"] = (
                context.bot_data.get("analysis_count", 0) + 1
            )
        except Exception as e:
            logger.warning("[%s] cache-hit send_photo failed: %s", ticker, e)
        return "completed"

    cancel_registry = context.chat_data.setdefault("analysis_cancels", {})

    # Per-run UUID is the cancel-callback key. Lets us attach the cancel
    # button via send_photo's reply_markup (which doesn't yet know the
    # message_id Telegram will assign), avoiding a second API call. With
    # N parallel queue runs, that second call commonly hit Telegram's
    # per-bot rate limit (~30/s) and silently dropped — leaving later
    # tickers stuck on "Analyzing… please wait" with no cancel button.
    run_id = uuid.uuid4().hex[:8]
    # Two cancel signals: threading.Event for the LangChain callback (runs
    # in a worker thread), asyncio.Event for the queue-wait (asyncio loop).
    # Both set together by the cancel handler — see _handle_cancel_analysis.
    cancel_event = threading.Event()
    cancel_async = asyncio.Event()
    cancel_registry[run_id] = {
        "cancel_event": cancel_event,
        "async_event": cancel_async,
        "message_id": None,
    }
    cancel_kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"cancel_analysis:{run_id}",
                )
            ]
        ]
    )

    chart_url = finviz_chart_url(ticker)
    logger.info("[%s] chart_url=%s run_id=%s", ticker, chart_url, run_id)

    # Try to acquire a slot synchronously BEFORE the send_photo. This
    # determines whether the initial caption should be "Analyzing" (slot
    # in hand) or "Queued" (will wait). Doing the acquire first is what
    # makes the caption reflect reality — `sem.locked()` checked AFTER
    # all 7 coroutines have entered but BEFORE any have acquired would
    # show every one of them as "not locked", so all would label
    # themselves "Analyzing" even though only N can actually run.
    sem = _get_run_semaphore()
    acquired = _try_acquire_nonblocking(sem)
    logger.debug(
        "[%s] sem state after sync-acquire: acquired=%s, _value=%d, _waiters=%d",
        ticker,
        acquired,
        sem._value,
        len(sem._waiters) if sem._waiters else 0,
    )

    initial_caption = (
        f"📊 Analyzing *{escape_markdown(ticker, version=2)}*… please wait\\."
        if acquired
        else f"⏳ *{escape_markdown(ticker, version=2)}* queued — waiting for slot…"
    )
    logger.debug(
        "[%s] send_photo START caption=%s",
        ticker,
        "Analyzing" if acquired else "Queued",
    )
    progress_msg = None
    last_err: Exception | None = None
    # Retry once on transient TimedOut. Telegram fetches finviz URLs
    # server-side and a burst of parallel send_photos against the
    # per-chat rate limit causes occasional 5–30s tail latencies.
    # Retrying avoids dropping the ticker entirely on a single flap.
    for attempt in range(2):
        try:
            progress_msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=chart_url,
                caption=initial_caption,
                parse_mode="MarkdownV2",
                reply_markup=cancel_kb,
            )
            break
        except Exception as e:
            last_err = e
            is_timeout = type(e).__name__ in ("TimedOut", "TimeoutError")
            if is_timeout and attempt == 0:
                logger.warning(
                    "[%s] send_photo TimedOut (attempt 1) — retrying", ticker
                )
                await asyncio.sleep(1.0)
                continue
            break

    if progress_msg is None:
        # Telegram rate-limit / network error / retries exhausted. Without
        # this guard, the exception propagates out of the coroutine,
        # leaks the acquired semaphore slot AND the cancel_registry entry
        # — and the user sees no message at all for this ticker.
        logger.error(
            "[%s] send_photo FAILED: %s (type=%s)",
            ticker,
            last_err,
            type(last_err).__name__ if last_err else "unknown",
        )
        if acquired:
            sem.release()
            logger.debug("[%s] released slot after send_photo failure", ticker)
        cancel_registry.pop(run_id, None)
        return "completed"  # not "cancelled" — counts toward 'failed' tally
    logger.debug("[%s] send_photo OK message_id=%s", ticker, progress_msg.message_id)
    cancel_registry[run_id]["message_id"] = progress_msg.message_id
    # /status counter — bump only after the progress photo lands so a
    # network blip in send_photo (handled above with retry-then-fail)
    # doesn't inflate the count for an analysis the user never saw.
    context.bot_data["analysis_count"] = context.bot_data.get("analysis_count", 0) + 1

    if not TRADINGAGENTS_AVAILABLE:
        if acquired:
            sem.release()
        # The `try:` block that pops cancel_registry in `finally` starts
        # below — early-returning here would leak the entry written at L358
        # for the chat's lifetime. Pop explicitly.
        cancel_registry.pop(run_id, None)
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption="TradingAgents module not available.",
        )
        return "unavailable"

    reporter = ProgressReporter(
        bot=context.bot,
        chat_id=chat_id,
        message_id=progress_msg.message_id,
        ticker=ticker,
        loop=asyncio.get_running_loop(),
        cancel_event=cancel_event,
        cancel_run_id=run_id,
    )

    async def _render_cancelled() -> None:
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption=f"❌ Analysis cancelled for *{escape_markdown(ticker, version=2)}*\\.",
            parse_mode="MarkdownV2",
            reply_markup=None,
        )

    # If the synchronous acquire didn't succeed, wait for either a slot
    # OR a cancel signal — whichever fires first. asyncio.wait races the
    # two; instant cancel response, no polling churn on the semaphore's
    # waiter list.
    try:
        if not acquired:
            logger.debug("[%s] entering wait-for-slot", ticker)
            acquire_task = asyncio.create_task(sem.acquire())
            cancel_task = asyncio.create_task(cancel_async.wait())
            try:
                done, pending = await asyncio.wait(
                    [acquire_task, cancel_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for t in (acquire_task, cancel_task):
                    if not t.done():
                        t.cancel()

            if acquire_task in done and not acquire_task.cancelled():
                acquired = True
                logger.debug("[%s] queued slot acquired", ticker)
            else:
                # Cancel won the race. If acquire ALSO completed before we
                # got to inspect (rare race), give the slot back.
                if acquire_task.done() and not acquire_task.cancelled():
                    try:
                        acquire_task.result()
                        sem.release()
                        logger.debug(
                            "[%s] released raced-acquire slot during cancel", ticker
                        )
                    except Exception:
                        pass
                logger.info("[%s] cancelled while queued", ticker)
                await _render_cancelled()
                return "cancelled"

        # If we showed "Queued" initially, flip the caption to "Analyzing"
        # now that we have the slot. Best-effort — drop on rate-limit.
        if initial_caption.startswith("⏳"):
            logger.debug("[%s] flipping caption Queued → Analyzing", ticker)
            try:
                await context.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=progress_msg.message_id,
                    caption=f"📊 Analyzing *{escape_markdown(ticker, version=2)}*… please wait\\.",
                    parse_mode="MarkdownV2",
                    reply_markup=cancel_kb,
                )
            except Exception as e:
                logger.warning("[%s] caption flip failed: %s", ticker, e)

        logger.debug("[%s] entering to_thread(run_trading_analysis)", ticker)
        final_state, signal = await asyncio.to_thread(
            run_trading_analysis,
            ticker,
            reporter,
        )
        logger.debug(
            "[%s] to_thread returned signal=%s final_state=%s",
            ticker,
            signal,
            "present" if final_state else "None",
        )
        # Race close: the user can tap Cancel after the final LLM call but
        # before any next step-boundary check. propagate() then returns
        # cleanly even though the user wanted to abort. Honour the cancel
        # flag and discard the result instead of overwriting "Cancelling…".
        if cancel_event.is_set():
            logger.info("Analysis cancelled by user (post-completion) for %s", ticker)
            await _render_cancelled()
            return "cancelled"

        if final_state is None:
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                caption="Analysis failed. TradingAgents module not available.",
                reply_markup=None,
            )
            return "unavailable"

        markdown_content = format_analysis_result_markdown(
            ticker,
            final_state,
            signal,
            config_summary=key.caption(),
            generated_at=datetime.now(UTC),
        )
        # `tables` extension generates <table> for GFM pipe-tables; the
        # telegraph_client then rewrites them into <ul> since Telegraph
        # strips <table>. Without this extension the pipes survive as
        # literal `|` text in the rendered page.
        rendered_md = markdown.markdown(markdown_content, extensions=["tables"])
        html_content = f'<img src="{chart_url}"/>{rendered_md}'
        # `edit_path` is set on refresh runs so the Telegraph page is
        # updated in place (same URL) instead of creating a new one.
        # Defaults to None on fresh runs (no prior URL) → create_page.
        edit_path = (
            _path_from_telegraph_url(prev_telegraph_url) if prev_telegraph_url else None
        )
        telegraph_url = await publish_to_telegraph(
            key.telegraph_title(ticker),
            html_content,
            edit_path=edit_path,
        )

        # Re-check cancel flag — Telegraph publish is also a network round-trip
        # the user might cancel through.
        if cancel_event.is_set():
            logger.info("Analysis cancelled by user (post-publish) for %s", ticker)
            await _render_cancelled()
            return "cancelled"

        # See cache-hit branch above — sourced from final_trade_decision
        # with the redundant header stripped, so the expandable prose
        # matches the signal badge by construction.
        summary = caption_summary(final_state)
        caption = format_short_message(
            ticker,
            signal,
            telegraph_url,
            summary=summary,
            config_summary=key.caption(),
        )
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption=caption,
            parse_mode="HTML",
            reply_markup=_full_report_keyboard(ticker, today_iso, telegraph_url),
        )
        # Persist for the rest of today so the next /watch tap or digest
        # fire on this ticker (any user with the same config) hits the
        # short-circuit at the top of the function. `cache.store` enforces
        # the cache-hygiene gate internally — a falsy `telegraph_url` is
        # a no-op (skips the store + logs). See PR #30 / INTU 2026-05-11.
        result_cache.store(key, ticker, today_iso, final_state, signal, telegraph_url)
        return "completed"
    except CancelledByUserError:
        logger.info("Analysis cancelled by user for %s", ticker)
        await _render_cancelled()
        return "cancelled"
    except Exception as e:
        logger.exception("Error analyzing %s", ticker)
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption=f"Error analyzing {ticker}.\n\nDetails: {str(e)[:200]}",
            reply_markup=None,
        )
        return "completed"
    finally:
        if acquired:
            sem.release()
            logger.debug(
                "[%s] sem released, _value=%d, _waiters=%d",
                ticker,
                sem._value,
                len(sem._waiters) if sem._waiters else 0,
            )
        cancel_registry.pop(run_id, None)
        logger.debug(
            "[%s] run_id=%s exiting; registry size=%d",
            ticker,
            run_id,
            len(cancel_registry),
        )


async def _handle_cancel_analysis(
    context: ContextTypes.DEFAULT_TYPE, query, run_id: str
) -> None:
    """Set the cancel flag for a running analysis identified by run_id.

    The persistent "❌ Cancelling…" caption edit is fired through the
    serializing queue (`_queued_cancel_edit`) so a multi-cancel burst
    doesn't overrun Telegram's per-chat edit limit; later taps land
    their captions in FIFO order at ~1/sec. The dispatcher already
    answered the callback query with an empty ack.
    """
    cancel_registry = context.chat_data.get("analysis_cancels") or {}
    entry = cancel_registry.get(run_id)
    logger.debug(
        "cancel_analysis tapped: run_id=%s registry_keys=%s",
        run_id,
        list(cancel_registry.keys()),
    )
    if entry is None:
        logger.warning(
            "cancel_analysis: no entry for run_id=%s — already finished?", run_id
        )
        return
    entry["cancel_event"].set()
    async_event = entry.get("async_event")
    if async_event is not None:
        async_event.set()
    logger.debug("cancel_analysis: events set for run_id=%s", run_id)

    message_id = entry.get("message_id")
    if message_id is None:
        return
    # Fire-and-forget through the serializing queue. Returns immediately;
    # the actual edit lands in FIFO order respecting Telegram's per-chat
    # rate limit (~1/sec).
    asyncio.create_task(
        _queued_cancel_edit(context.bot, query.message.chat_id, message_id)
    )


async def _run_digest_with_guard(
    chat_data: dict, running_key: str, application, user_id: int, chat_id: int
) -> None:
    """Wrap run_user_digest so the re-entry guard clears even on exception."""
    try:
        await run_user_digest(application, user_id, chat_id)
    finally:
        chat_data.pop(running_key, None)


# ─── digest fan-out + JobQueue plumbing ─────────────────────────────────


class _DigestProgressReporter:
    """Per-ticker reporter for the digest fan-out. Conforms to the
    `ProgressReporter` interface that `_DelegatingProgressCallback`
    consumes (`report` coroutine, `loop`, `cancel_event`) but instead of
    editing its own Telegram message it calls `on_step` so the parent
    `run_user_digest` can repaint the shared digest message.

    Coalesces consecutive duplicate node names so a node that fires
    multiple LLM calls only updates the status once.
    """

    def __init__(
        self,
        ticker: str,
        loop: asyncio.AbstractEventLoop,
        on_step,  # async (ticker, friendly: str, ordinal: int|None) -> None
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.ticker = ticker
        self.loop = loop
        self.on_step = on_step
        # _DelegatingProgressCallback._dispatch reads cancel_event before
        # every LLM-call boundary; a non-None event lets digest_cancel
        # raise CancelledByUserError into in-flight tickers.
        self.cancel_event = cancel_event
        self._last_step: str | None = None

    async def report(self, raw_node_name: str) -> None:
        if raw_node_name == self._last_step:
            return
        self._last_step = raw_node_name
        friendly, ordinal = resolve_step(raw_node_name)
        try:
            await self.on_step(self.ticker, friendly, ordinal)
        except Exception as e:
            logger.debug("digest progress callback failed: %s", e)

    async def report_starting(self) -> None:
        """Flip status to 'Starting…' right after sem.acquire — bridges
        the gap between slot acquisition and the first LLM-callback
        event. Without it, a ticker can sit in ⏳ for 5-30s during the
        graph cold-start phase even though it's already running."""
        try:
            await self.on_step(self.ticker, "Starting…", None)
        except Exception as e:
            logger.debug("digest progress callback failed: %s", e)


async def _analyze_one_for_digest(
    user_id: int, ticker: str, reporter: _DigestProgressReporter | None = None
) -> dict | None:
    """Headless analysis for one ticker. Returns
    {ticker, signal, telegraph_url} or None on failure.

    Holds the global `_run_semaphore` for the duration so the digest
    fan-out interleaves with manual /watch runs through the same FIFO
    queue — a 50-ticker watchlist serializes naturally into batches of
    `MAX_CONCURRENT_ANALYSES` instead of hammering the LLM provider.

    `reporter`, when supplied, drives the per-ticker step display in the
    shared digest message — same LangChain hook as the manual flow.
    """
    # INVARIANT — keep in sync with `_run_analysis_for_ticker`. The two
    # analysis paths share three structural surfaces that must drift
    # together: (1) the race-close cancel-event checks after `to_thread`
    # and after Telegraph publish, (2) the `AnalysisConfigKey` cache-key
    # construction via `build_config → from_config`, (3) the Telegraph
    # title via `key.telegraph_title(ticker)`. If you change one path's
    # handling of any of these, change the other to match.
    #
    # Cache short-circuit before acquiring a semaphore slot — a cached
    # result needs no LLM call, so we shouldn't burn a slot or trigger
    # the "Starting…" reporter event.
    config = build_config()
    key = AnalysisConfigKey.from_config(config)
    today_iso = result_cache.today_iso()
    cached = result_cache.lookup(key, ticker, today_iso)
    if cached:
        logger.info("digest: result_cache HIT for %s — skipping LLM run", ticker)
        return {
            "ticker": ticker,
            "signal": cached["signal"],
            "telegraph_url": cached.get("telegraph_url"),
        }

    sem = _get_run_semaphore()
    # Defensive: track whether acquire actually completed before releasing.
    # CPython 3.11+ asyncio.Semaphore.acquire handles cancel-during-await
    # correctly (re-releases if the slot was given), but the `acquired`
    # flag mirrors the manual flow's pattern and survives any future
    # behavior change in CPython's primitive.
    acquired = False
    try:
        await sem.acquire()
        acquired = True
        if not TRADINGAGENTS_AVAILABLE:
            return None
        # Flip the digest status from ⏳ → "Starting…" the moment we have
        # a slot. Without this, cold-start graph builds (5–30s) make the
        # ticker look queued even though it's running.
        if reporter is not None:
            await reporter.report_starting()
        try:
            final_state, signal = await asyncio.to_thread(
                run_trading_analysis,
                ticker,
                reporter,
            )
        except Exception:
            logger.exception("digest: analysis failed for %s", ticker)
            return None
        if final_state is None:
            return None

        # Race-close check: if the cancel button was tapped while
        # `to_thread(propagate)` was on the wire, skip the Telegraph
        # publish + cache store so we don't pay for a network round-trip
        # the user already abandoned (and don't poison the cache with a
        # post-cancel result). Mirrors the two-check pattern in
        # `_run_analysis_for_ticker`.
        if reporter is not None and reporter.cancel_event.is_set():
            logger.info("digest: cancel detected post-propagate for %s", ticker)
            raise CancelledByUserError(f"digest cancel for {ticker}")

        # Publish the per-ticker Telegraph page so the digest summary can
        # link to a full report per row. Skip silently on failure — the
        # row just renders without a link.
        chart_url = finviz_chart_url(ticker)
        try:
            md = format_analysis_result_markdown(
                ticker,
                final_state,
                signal,
                config_summary=key.caption(),
                generated_at=datetime.now(UTC),
            )
            html = markdown.markdown(md, extensions=["tables"])
            html = f'<img src="{chart_url}"/>{html}'
            telegraph_url = await publish_to_telegraph(
                key.telegraph_title(ticker), html
            )
        except Exception as e:
            logger.warning("digest: telegraph publish failed for %s: %s", ticker, e)
            telegraph_url = None

        # Persist for the rest of today — same key the manual /watch flow
        # writes, so a digest fan-out followed by a manual /watch tap on
        # the same ticker only pays for one actual LLM run. `cache.store`
        # enforces the cache-hygiene gate internally (skips on falsy
        # `telegraph_url`), so a transient publish failure doesn't
        # poison the cache.
        result_cache.store(key, ticker, today_iso, final_state, signal, telegraph_url)

        return {"ticker": ticker, "signal": signal, "telegraph_url": telegraph_url}
    finally:
        if acquired:
            sem.release()


_DIGEST_PROGRESS_INTERVAL = 2.0  # min seconds between progressive edits


def _completed_digest_row(ticker_h: str, result: dict) -> str:
    """One HTML-safe row for a completed ticker. Used by both the
    progress view and the final summary so a row's appearance is stable
    once analysis lands — no jump from generic ✅ to a signal-coloured
    emoji at the final-edit boundary.

    `ticker_h` is pre-escaped (html.escape) by the caller."""
    signal = (result.get("signal") or "—").strip()
    emoji = DECISION_EMOJI.get(signal.upper(), "📊")
    signal_h = _html_escape(signal)
    if result.get("telegraph_url"):
        href = _html_escape(result["telegraph_url"], quote=True)
        return f'{emoji} <b>{ticker_h}</b> — <b>{signal_h}</b> <a href="{href}">📄</a>'
    return f"{emoji} <b>{ticker_h}</b> — <b>{signal_h}</b>"


def _format_digest_progress(
    watchlist: list[str],
    status: dict[str, object],
    safe_date: str,
    skipped_closed: list[str] | None = None,
) -> str:
    """In-progress view (HTML). `status[ticker]` is one of:
      - "pending"           → ⏳ TICKER
      - ("analyzing", friendly, ordinal) → 📊 TICKER — Friendly (n/M)
      - "cancelled"         → ⛔ TICKER — cancelled
      - dict (result)       → ✅ TICKER — SIGNAL
      - None                → ❌ TICKER — error
    Watchlist order is preserved so the user can see exactly where the
    fan-out is at any point.

    `safe_date` is pre-escaped (html.escape) by the caller.

    `skipped_closed` (optional) names tickers dropped by the per-ticker
    market-calendar gate. When non-empty, renders a one-line footnote
    so users see why a ticker isn't in today's run.
    """
    done = sum(1 for s in status.values() if not (s == "pending" or _is_analyzing(s)))
    n = len(watchlist)
    lines = [f"🌙 <b>Daily Digest</b> — {safe_date}  ({done}/{n})\n"]
    for ticker in watchlist:
        s = status[ticker]
        ticker_h = _html_escape(ticker)
        if s == "pending":
            lines.append(f"⏳ {ticker_h}")
        elif _is_analyzing(s):
            _, friendly, ordinal = s  # type: ignore[misc]
            friendly_h = _html_escape(friendly)
            if ordinal is not None:
                lines.append(
                    f"📊 <b>{ticker_h}</b> — {friendly_h} ({ordinal}/{TOTAL_STEPS})"
                )
            else:
                lines.append(f"📊 <b>{ticker_h}</b> — {friendly_h}")
        elif s == "cancelled":
            lines.append(f"⛔ <b>{ticker_h}</b> — cancelled")
        elif s is None:
            lines.append(f"❌ <b>{ticker_h}</b> — error")
        else:
            lines.append(_completed_digest_row(ticker_h, s))  # type: ignore[arg-type]
    if skipped_closed:
        skipped_h = ", ".join(_html_escape(t) for t in skipped_closed)
        lines.append(f"\n<i>⚫️ Skipped (markets closed): {skipped_h}</i>")
    return "\n".join(lines)


def _is_analyzing(s: object) -> bool:
    return isinstance(s, tuple) and len(s) == 3 and s[0] == "analyzing"


def _format_digest_summary(
    watchlist: list[str],
    status: dict[str, object],
    safe_date: str,
    skipped_closed: list[str] | None = None,
) -> str:
    """Final view (HTML): signal-emoji per row + Telegraph link,
    watchlist order.

    `safe_date` is pre-escaped (html.escape) by the caller.

    `skipped_closed` (optional) renders the same "markets closed" footnote
    as the progress view so the final message keeps the explanation."""
    lines = [f"🌙 <b>Daily Digest</b> — {safe_date}\n"]
    failed = cancelled = 0
    n = len(watchlist)
    for ticker in watchlist:
        s = status[ticker]
        ticker_h = _html_escape(ticker)
        if s == "cancelled":
            lines.append(f"⛔ <b>{ticker_h}</b> — cancelled")
            cancelled += 1
            continue
        # Anything else (None from a real failure, or — much more rarely —
        # leftover "pending"/"analyzing" from an interruption that didn't
        # route through the cancel path) renders as ❓ and counts as failed.
        if not isinstance(s, dict):
            lines.append(f"❓ <b>{ticker_h}</b> — error")
            failed += 1
            continue
        lines.append(_completed_digest_row(ticker_h, s))
    tally_parts: list[str] = []
    if cancelled:
        tally_parts.append(f"{cancelled} cancelled")
    if failed:
        tally_parts.append(f"{failed} failed")
    if tally_parts:
        lines.append(f"\n<i>{', '.join(tally_parts)} of {n}</i>")
    if skipped_closed:
        skipped_h = ", ".join(_html_escape(t) for t in skipped_closed)
        lines.append(f"\n<i>⚫️ Skipped (markets closed): {skipped_h}</i>")
    return "\n".join(lines)


def _digest_cancel_keyboard(message_id: int) -> InlineKeyboardMarkup:
    """Cancel button attached to the digest header. callback_data carries
    the message_id so the handler can find the right cancel-registry
    entry — chat_data is keyed by chat, not by message."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Cancel digest",
                    callback_data=f"digest_cancel:{message_id}",
                )
            ]
        ]
    )


async def run_user_digest(application, user_id: int, chat_id: int) -> None:
    """Fan-out for one user.

    Sends a header listing every watchlist ticker as `⏳` plus a
    "❌ Cancel digest" button, edits the header progressively as
    analyses finish (throttled to `_DIGEST_PROGRESS_INTERVAL`), and
    drops the button + replaces with the final signal-emoji summary
    when done.

    Cancellation: the button sets a shared `cancel_event` (threading)
    so in-flight tickers' LangChain callback raises
    `CancelledByUserError` at the next step boundary, and cancels
    each pending ticker's asyncio.Task so they unwind without
    acquiring a semaphore slot. Cancelled tickers render as `⛔`.

    Auto-disables the digest on `Forbidden` (user blocked the bot) so
    the JobQueue doesn't keep retrying every day.
    """
    full_watchlist = watchlist_storage.get_watchlist(user_id)
    today_date = date.today()
    today = today_date.isoformat()
    # All digest captions are HTML mode now — escape for HTML, not MarkdownV2.
    safe_date = _html_escape(today)

    if not full_watchlist:
        logger.info("digest: skipping user %s — empty watchlist", user_id)
        return

    # Belt-and-suspenders LLM precheck: the manual ▶ Run now path also
    # gates upstream, but the daily JobQueue callback comes through here
    # too — and a user who enabled digest before TRADINGAGENTS_LLM_PROVIDER
    # was set (or with a stale .env) would otherwise see a wall of
    # identical 401 errors.
    reason = check_llm_configured()
    if reason is not None:
        logger.info(
            "digest: skipping user %s — LLM not configured (%s)", user_id, reason
        )
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=llm_setup_error_message(reason),
                parse_mode="MarkdownV2",
            )
        except Forbidden:
            logger.warning(
                "digest: user %s blocked the bot, disabling on llm-precheck",
                user_id,
            )
            await user_config_storage.disable_digest(user_id)
            cancel_digest_job(application, user_id)
        except Exception as e:
            logger.debug("digest llm-precheck send failed: %s", e)
        return

    # Apply the digest filter, intersected with the live watchlist (auto-prune
    # tickers the user removed since the filter was last edited). Legacy
    # fallback (`tickers` key absent → all watchlist) lives in the storage
    # method so the `/list` UX view shares the same definition — see
    # `UserConfigStorage.get_enrolled_tickers`.
    watchlist = user_config_storage.get_enrolled_tickers(user_id, full_watchlist)

    if not watchlist:
        # Filter is set but resolved to nothing (user has cleared it, or every
        # selected ticker has since been removed from the watchlist). Send a
        # one-line nudge so they know the digest fired but had nothing to do.
        logger.info(
            "digest: empty filter for user %s — sending reminder",
            user_id,
        )
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🌙 <b>Daily Digest</b> — {safe_date}\n\n"
                    "<i>No tickers selected. Open /digest and tap "
                    "📋 Tickers to pick what to include.</i>"
                ),
                parse_mode="HTML",
            )
        except Forbidden:
            logger.warning(
                "digest: user %s blocked the bot, disabling on reminder", user_id
            )
            await user_config_storage.disable_digest(user_id)
            cancel_digest_job(application, user_id)
        except Exception as e:
            logger.debug("digest reminder send failed: %s", e)
        return

    # Calendar gate: per-ticker exchange-session check via `is_market_open_for`.
    # Drop tickers whose exchange is closed today (weekend OR holiday) — they
    # would just produce stale yesterday-data analyses. Watchlist order is
    # preserved so the user-visible row order stays stable across days. If
    # EVERY ticker's market is closed, the whole digest short-circuits with
    # a one-line heads-up (operators wanted explicit "closed" affordance, not
    # silent skip, per PR #71 scope discussion).
    skipped_closed = [t for t in watchlist if not is_market_open_for(t, today_date)]
    watchlist = [t for t in watchlist if t not in set(skipped_closed)]

    if not watchlist:
        logger.info(
            "digest: all %d enrolled tickers' markets closed on %s — sending heads-up",
            len(skipped_closed),
            today,
        )
        skipped_h = ", ".join(_html_escape(t) for t in skipped_closed)
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🌙 <b>Daily Digest</b> — {safe_date}\n\n"
                    f"⚫️ <i>Markets closed today — no digest.</i>\n"
                    f"<i>Skipped: {skipped_h}</i>"
                ),
                parse_mode="HTML",
            )
        except Forbidden:
            logger.warning(
                "digest: user %s blocked the bot, disabling on closed-markets",
                user_id,
            )
            await user_config_storage.disable_digest(user_id)
            cancel_digest_job(application, user_id)
        except Exception as e:
            logger.debug("digest closed-markets send failed: %s", e)
        return

    n = len(watchlist)
    logger.info(
        "digest: launching for user %s (%d/%d tickers after filter)",
        user_id,
        n,
        len(full_watchlist),
    )

    # status[ticker]: "pending" | ("analyzing", friendly, ordinal) |
    #                 "cancelled" | dict (completed result) | None (failed).
    status: dict[str, object] = {t: "pending" for t in watchlist}

    cancel_event = threading.Event()
    # Populated below once the per-ticker tasks exist; the cancel handler
    # iterates this list to .cancel() each. Mutable so the registry entry
    # references the same list we mutate.
    tasks_holder: list[asyncio.Task] = []

    try:
        header = await application.bot.send_message(
            chat_id=chat_id,
            text=_format_digest_progress(watchlist, status, safe_date, skipped_closed),
            parse_mode="HTML",
        )
    except Forbidden:
        logger.warning("digest: user %s blocked the bot, disabling", user_id)
        await user_config_storage.disable_digest(user_id)
        cancel_digest_job(application, user_id)
        return

    cancel_kb = _digest_cancel_keyboard(header.message_id)
    # Attach the cancel button now that the header has a message_id —
    # send_message can't carry the callback_data because it doesn't
    # know the id at send time. Cheap second call; the AIORateLimiter
    # paces it.
    try:
        await application.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=header.message_id,
            reply_markup=cancel_kb,
        )
    except Exception as e:
        logger.debug("digest: cancel-button attach skipped: %s", e)

    # Register the cancel handle so the digest_cancel:<msg_id> callback
    # can find it. Same per-chat pattern as analysis_cancels.
    chat_data = application.chat_data[chat_id]
    digest_cancels = chat_data.setdefault("digest_cancels", {})
    digest_cancels[header.message_id] = {
        "cancel_event": cancel_event,
        "tasks": tasks_holder,
    }

    last_edit_at = 0.0
    edit_in_flight = False
    pending_rerender = False
    blocked = False

    async def _render() -> None:
        """Single-flight, trampolining render.

        If a state change fires while an edit is already in flight, the
        bailed call sets `pending_rerender` and the running render loops
        back to repaint once its HTTP edit returns. Without this, the
        bailed render is permanently lost — observable when one ticker
        cache-hits (instant `status` flip → `_render` enters its HTTP
        edit) and the next ticker's `report_starting` arrives mid-flight:
        no future `_render` fires until the second ticker's first real
        LLM-callback event (5–30s into graph cold-start), so the row
        sits on ⏳ even though the slot is taken. The throttle is still
        respected because each loop iteration recomputes `wait` against
        `last_edit_at`.
        """
        nonlocal last_edit_at, edit_in_flight, pending_rerender, blocked
        if blocked:
            return
        if edit_in_flight:
            pending_rerender = True
            return
        edit_in_flight = True
        try:
            while True:
                pending_rerender = False
                wait = _DIGEST_PROGRESS_INTERVAL - (time.monotonic() - last_edit_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                if blocked:
                    return
                last_edit_at = time.monotonic()
                text = _format_digest_progress(
                    watchlist, status, safe_date, skipped_closed
                )
                try:
                    # Re-attach the cancel button on every edit — Telegram
                    # drops reply_markup unless re-sent with each edit.
                    await application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=header.message_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=cancel_kb,
                    )
                except Forbidden:
                    logger.warning(
                        "digest: user %s blocked the bot mid-run, disabling", user_id
                    )
                    await user_config_storage.disable_digest(user_id)
                    cancel_digest_job(application, user_id)
                    blocked = True
                    # User can't receive the output anyway — abort the
                    # remaining fan-out so we stop spending LLM tokens on
                    # a chat we'll never deliver to. Mirrors the user-tap
                    # cancel path: threading event for in-flight tickers,
                    # asyncio.cancel for pending ones.
                    cancel_event.set()
                    for t in tasks_holder:
                        if not t.done():
                            t.cancel()
                    return
                except Exception as e:
                    # "message is not modified" or transient — keep going.
                    logger.debug("digest progress edit skipped: %s", e)
                if not pending_rerender:
                    break
        finally:
            edit_in_flight = False

    async def _on_step(ticker: str, friendly: str, ordinal: int | None) -> None:
        # Don't overwrite a "cancelled" status with a late step event
        # that arrived after the cancel was processed but before the
        # in-flight LLM call returned.
        if status[ticker] == "cancelled":
            return
        status[ticker] = ("analyzing", friendly, ordinal)
        await _render()

    loop = asyncio.get_running_loop()

    async def _wrapped(ticker: str) -> tuple[str, object]:
        reporter = _DigestProgressReporter(
            ticker, loop, _on_step, cancel_event=cancel_event
        )
        try:
            result = await _analyze_one_for_digest(user_id, ticker, reporter)
            # Race: cancel could have fired after analyze returned but
            # before we record the result. Honour the flag — discard.
            if cancel_event.is_set():
                status[ticker] = "cancelled"
            else:
                status[ticker] = result
        except (CancelledByUserError, asyncio.CancelledError):
            status[ticker] = "cancelled"
        except Exception:
            logger.exception("digest: ticker %s failed unexpectedly", ticker)
            status[ticker] = None
        await _render()
        return ticker, status[ticker]

    try:
        tasks = [asyncio.create_task(_wrapped(t)) for t in watchlist]
        tasks_holder.extend(tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        digest_cancels.pop(header.message_id, None)

    if blocked:
        return

    # Final edit — drops the cancel button (run is over) and bypasses
    # the throttle so the summary always lands.
    try:
        await application.bot.edit_message_text(
            chat_id=chat_id,
            message_id=header.message_id,
            text=_format_digest_summary(watchlist, status, safe_date, skipped_closed),
            parse_mode="HTML",
            reply_markup=None,
        )
    except Forbidden:
        logger.warning("digest: user %s blocked the bot mid-run, disabling", user_id)
        await user_config_storage.disable_digest(user_id)
        cancel_digest_job(application, user_id)
    except Exception as e:
        logger.exception("digest: failed to edit summary for user %s: %s", user_id, e)

    # Email mirror — runs AFTER Telegram delivery (success or fail) so
    # email is independent. Caller-side gates: user opted in (email set
    # in storage) + env configured (RESEND_API_KEY + RESEND_FROM). Email
    # is a MIRROR, never a REPLACEMENT — `send_digest_email` swallows
    # its own exceptions internally, but we wrap it at the call site too
    # (defense in depth): a future bug in email_client that lets an
    # exception escape would otherwise break the Telegram digest, which
    # is the exact failure mode the mirror pattern exists to prevent.
    digest = user_config_storage.get_digest(user_id)
    email_to = (digest or {}).get("email")
    if email_to and is_email_configured():
        try:
            await send_digest_email(
                to_addr=email_to,
                watchlist=watchlist,
                status=status,
                safe_date=safe_date,
                date_iso=today,
                skipped_closed=skipped_closed,
            )
        except Exception as e:
            logger.warning(
                "digest: email mirror to %s raised unexpectedly (%s) — "
                "Telegram digest unaffected. This is a bug in email_client; "
                "send_digest_email is supposed to swallow its own failures.",
                email_to,
                type(e).__name__,
            )
            logger.debug("email mirror unexpected traceback:", exc_info=True)
    elif email_to and not is_email_configured():
        # User opted in but env isn't configured. Log loud so the
        # operator notices — `/email test` would have caught this too,
        # but a startup-time check in `_post_init` is the canonical
        # warning surface.
        logger.warning(
            "digest: user %s has email %s opt-in but RESEND_API_KEY/RESEND_FROM "
            "not set in .env; mirror skipped",
            user_id,
            email_to,
        )


async def _handle_digest_cancel(
    context: ContextTypes.DEFAULT_TYPE, query, message_id_str: str
) -> None:
    """Cancel an in-progress digest run identified by its header
    message_id. Sets the threading cancel_event (in-flight tickers
    raise CancelledByUserError at the next LLM-call boundary) and
    cancels each ticker's asyncio.Task (pending tickers unwind without
    acquiring a slot).
    """
    try:
        message_id = int(message_id_str)
    except ValueError:
        return
    digest_cancels = context.chat_data.get("digest_cancels") or {}
    entry = digest_cancels.get(message_id)
    if entry is None:
        # Already finished, or stale button from previous chat history.
        logger.info("digest_cancel: no in-flight run for message_id=%s", message_id)
        return
    entry["cancel_event"].set()
    cancelled = 0
    for t in entry["tasks"]:
        if not t.done():
            t.cancel()
            cancelled += 1
    logger.info(
        "digest_cancel: signalled message_id=%s — cancelled %d task(s)",
        message_id,
        cancelled,
    )


async def _digest_job_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback — `context.job.data` carries {user_id, chat_id}."""
    job = context.job
    if job is None or not job.data:
        return
    await run_user_digest(
        context.application,
        int(job.data["user_id"]),
        int(job.data["chat_id"]),
    )


def _digest_job_name(user_id: int) -> str:
    return f"digest:{user_id}"


def register_digest_job(application, user_id: int, digest: dict | None) -> None:
    """(Re-)schedule the daily run for `user_id`.

    Cancels any existing job under the same name first so an hour or tz
    change replaces (not duplicates) the previous schedule. No-ops if
    JobQueue isn't available, the digest is incomplete, or the digest
    is disabled — caller's responsibility to call cancel_digest_job
    explicitly when disabling.
    """
    if not application.job_queue:
        logger.warning("register_digest_job: JobQueue not configured")
        return
    cancel_digest_job(application, user_id)
    if not digest or not digest.get("enabled"):
        return
    hour = digest.get("hour_local")
    tz_str = digest.get("tz")
    chat_id = digest.get("chat_id")
    if hour is None or not tz_str or not chat_id:
        logger.warning(
            "register_digest_job: incomplete digest for user %s: %s", user_id, digest
        )
        return
    try:
        fire_time = dt_time(hour=int(hour), minute=0, tzinfo=ZoneInfo(tz_str))
    except (ZoneInfoNotFoundError, ValueError) as e:
        logger.warning("register_digest_job: bad tz/hour for user %s: %s", user_id, e)
        return
    application.job_queue.run_daily(
        _digest_job_callback,
        time=fire_time,
        name=_digest_job_name(user_id),
        data={"user_id": user_id, "chat_id": chat_id},
    )
    logger.info(
        "digest: registered for user %s at %02d:00 %s", user_id, int(hour), tz_str
    )


def cancel_digest_job(application, user_id: int) -> int:
    """Remove any scheduled digest job for `user_id`. Returns count cancelled."""
    if not application.job_queue:
        return 0
    jobs = application.job_queue.get_jobs_by_name(_digest_job_name(user_id))
    for j in jobs:
        j.schedule_removal()
    if jobs:
        logger.info("digest: cancelled %d job(s) for user %s", len(jobs), user_id)
    return len(jobs)
