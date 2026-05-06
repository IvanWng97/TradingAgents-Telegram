"""Inline-button callback handlers."""

import asyncio
import logging
import threading
import time
import uuid

import markdown
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from tg_bot.analysis import (
    TRADINGAGENTS_AVAILABLE,
    get_model_options,
    has_model_catalog,
    run_trading_analysis,
)
from tg_bot.config import Config
from tg_bot.chart import finviz_chart_url
from tg_bot.digest import build_digest_response
from tg_bot.formatters import (
    extract_summary,
    format_analysis_result_markdown,
    format_short_message,
)
from tg_bot.handlers.commands import (
    build_del_keyboard,
    build_history_dates_response,
    build_history_response,
    build_history_tickers_response,
    build_watchlist_response,
)
from tg_bot.progress import CancelledByUserError, ProgressReporter
from tg_bot.storage import user_config_storage, watchlist_storage
from tg_bot.telegraph_client import publish_to_telegraph


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


def _try_acquire_nonblocking(sem: asyncio.Semaphore) -> bool:
    """Mimic asyncio.Semaphore.acquire()'s fast-path synchronously — safe
    in asyncio because coroutines are cooperative (no preemption between
    `if` and `_value -= 1`). Avoids the broken `wait_for(..., timeout=0)`
    pattern, which cancels the inner task before the event loop can run
    its fast-path, so it always reports failure.
    """
    if not sem.locked() and (
        sem._waiters is None or all(w.cancelled() for w in sem._waiters)
    ):
        sem._value -= 1
        return True
    return False


# Serializes cancel-ack caption edits so a multi-cancel burst doesn't
# overrun Telegram's per-chat edit limit (~1/sec). Each call holds the
# lock and sleeps if the last edit fired less than _MIN_CANCEL_INTERVAL
# seconds ago, then executes. Effectively a FIFO queue with rate-aware
# pacing — toasts already covered the user's immediate feedback, this
# just lands the persistent "Cancelling…" caption one by one.
_cancel_edit_lock = asyncio.Lock()
_last_cancel_edit_at: float = 0.0
_MIN_CANCEL_INTERVAL = 1.1  # safe margin under Telegram's per-chat limit


async def _queued_cancel_edit(bot, chat_id: int, message_id: int) -> None:
    """Lock-serialized variant of `edit_message_caption` for cancel-ack."""
    global _last_cancel_edit_at
    async with _cancel_edit_lock:
        now = time.monotonic()
        wait = _MIN_CANCEL_INTERVAL - (now - _last_cancel_edit_at)
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
        _last_cancel_edit_at = time.monotonic()


def _model_keyboard(mode: str, provider: str) -> InlineKeyboardMarkup:
    """One button per row — provider model labels can be long. A trailing
    Cancel row lets users bail out of the multi-step config flow without
    finishing every selection."""
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"{mode}:{provider}:{model_id}")]
        for label, model_id in get_model_options(provider, mode)
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel:config")])
    return InlineKeyboardMarkup(keyboard)


async def _handle_provider(query, user_id: int, provider: str) -> None:
    if not await user_config_storage.set_llm_provider(user_id, provider):
        await query.edit_message_text(
            f"Failed to set provider to `{provider}`\\.", parse_mode="MarkdownV2"
        )
        return
    if not has_model_catalog(provider):
        await query.edit_message_text(
            f"Provider set to `{provider}`\\.\n\n"
            "This provider needs a custom model ID — model selection isn't "
            f"wired up for it yet, so the run will use {escape_markdown('DEFAULT_CONFIG', version=2)} "
            "models\\.",
            parse_mode="MarkdownV2",
        )
        return
    await query.edit_message_text(
        f"Provider: `{provider}`\n\nChoose a *deep\\-think* model:",
        parse_mode="MarkdownV2",
        reply_markup=_model_keyboard("deep", provider),
    )


async def _handle_deep(query, user_id: int, provider: str, model: str) -> None:
    await user_config_storage.set_llm_model(user_id, "deep", model)
    await query.edit_message_text(
        f"Provider: `{provider}`\nDeep: `{model}`\n\nChoose a *quick\\-think* model:",
        parse_mode="MarkdownV2",
        reply_markup=_model_keyboard("quick", provider),
    )


async def _handle_quick(
    query, context: ContextTypes.DEFAULT_TYPE, user_id: int, provider: str, model: str
) -> None:
    await user_config_storage.set_llm_model(user_id, "quick", model)
    deep = user_config_storage.get_llm_model(user_id, "deep")
    # Quick is the last step of the /config flow — drop the rollback snapshot
    # set by config_cmd so the next /config doesn't restore stale state.
    context.user_data.pop("llm_snapshot", None)
    await query.edit_message_text(
        "LLM configuration saved\\.\n\n"
        f"Provider: `{provider}`\nDeep: `{deep}`\nQuick: `{model}`",
        parse_mode="MarkdownV2",
    )


async def _run_analysis_for_ticker(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    ticker: str,
) -> str:
    """Run a single analysis end-to-end. Sends its own progress photo +
    caption, registers a cancel event, and renders the final result.

    The graph instance comes from a pool — see analysis._get_or_create_pool.
    Pool serves both single-tap and parallel queue paths uniformly.

    Returns one of:
    - "completed" — analysis finished (success or non-cancel error).
    - "cancelled" — user tapped Cancel mid-run.
    - "unavailable" — tradingagents module not loaded.
    """
    cancel_registry = context.chat_data.setdefault("analysis_cancels", {})
    # /status counter — counts each analysis attempt across the whole bot.
    context.bot_data["analysis_count"] = context.bot_data.get("analysis_count", 0) + 1

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
        "event": cancel_event,
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

    if not TRADINGAGENTS_AVAILABLE:
        if acquired:
            sem.release()
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
            user_id,
            user_config_storage,
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

        markdown_content = format_analysis_result_markdown(ticker, final_state, signal)
        # `tables` extension generates <table> for GFM pipe-tables; the
        # telegraph_client then rewrites them into <ul> since Telegraph
        # strips <table>. Without this extension the pipes survive as
        # literal `|` text in the rendered page.
        rendered_md = markdown.markdown(markdown_content, extensions=["tables"])
        html_content = f'<img src="{chart_url}"/>{rendered_md}'
        telegraph_url = await publish_to_telegraph(f"{ticker} Analysis", html_content)

        # Re-check cancel flag — Telegraph publish is also a network round-trip
        # the user might cancel through.
        if cancel_event.is_set():
            logger.info("Analysis cancelled by user (post-publish) for %s", ticker)
            await _render_cancelled()
            return "cancelled"

        summary = extract_summary(final_state.get("final_trade_decision", ""))
        caption = format_short_message(ticker, signal, telegraph_url, summary=summary)
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption=caption,
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
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


async def _handle_select_toggle(
    query, context: ContextTypes.DEFAULT_TYPE, user_id: int, ticker: str
) -> None:
    """Toggle a ticker in the watchlist selection and re-render the keyboard."""
    selection: set[str] = context.chat_data.setdefault("watch_selection", set())
    if ticker in selection:
        selection.discard(ticker)
    else:
        selection.add(ticker)
    page = context.chat_data.get("watch_page", 0)
    text, kb = build_watchlist_response(user_id, selected=selection, page=page)
    if kb is None:
        await query.edit_message_text(text)
    else:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="MarkdownV2")


async def _handle_select_bulk(
    query, context: ContextTypes.DEFAULT_TYPE, user_id: int, action: str
) -> None:
    """Bulk-select action: `all` selects every ticker (across all pages),
    `clear` empties."""
    if action == "all":
        selection = set(watchlist_storage.get_watchlist(user_id))
    else:
        selection = set()
    context.chat_data["watch_selection"] = selection
    page = context.chat_data.get("watch_page", 0)
    text, kb = build_watchlist_response(user_id, selected=selection, page=page)
    if kb is None:
        await query.edit_message_text(text)
    else:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="MarkdownV2")


async def _handle_page_nav(
    query, context: ContextTypes.DEFAULT_TYPE, user_id: int, action: str
) -> None:
    """Paginate the /watch keyboard. `prev` / `next` step the page index;
    `noop` is the central indicator button (no-op)."""
    if action == "noop":
        return
    page = context.chat_data.get("watch_page", 0)
    if action == "next":
        page += 1
    elif action == "prev":
        page = max(0, page - 1)
    context.chat_data["watch_page"] = page
    selection = context.chat_data.get("watch_selection") or set()
    text, kb = build_watchlist_response(user_id, selected=selection, page=page)
    if kb is None:
        await query.edit_message_text(text)
    else:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="MarkdownV2")


async def _handle_done(query, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Unified entry point for both single and multi-ticker analysis runs.

    1 selected → cached graph (fast init, no parallel benefit anyway).
    N selected → fresh graph per ticker, run in parallel via asyncio.gather.
    """
    selection = sorted(context.chat_data.get("watch_selection") or set())
    if not selection:
        await query.answer("No tickers selected.", show_alert=True)
        return
    chat_id = query.message.chat_id
    context.chat_data.pop("watch_selection", None)

    if len(selection) == 1:
        # Single-ticker: replace the watchlist menu with the analysis flow.
        try:
            await query.delete_message()
        except Exception:
            pass
        await _run_analysis_for_ticker(context, chat_id, user_id, selection[0])
        return

    # Multi-ticker: parallel runs share the per-key graph pool — first run
    # in a fresh pool pays init cost, subsequent reuse warm instances.
    safe_list = escape_markdown(", ".join(selection), version=2)
    queue_msg_id = query.message.message_id
    try:
        await query.edit_message_text(
            f"🚀 Running queue: {safe_list}\n\n"
            "_Analyses run in parallel — cancel each independently\\._",
            parse_mode="MarkdownV2",
        )
    except Exception:
        pass

    logger.info("queue: launching gather for %d tickers: %s", len(selection), selection)
    results = await asyncio.gather(
        *(
            _run_analysis_for_ticker(context, chat_id, user_id, ticker)
            for ticker in selection
        ),
        return_exceptions=True,
    )
    logger.debug("queue: gather returned %d results", len(results))
    for ticker, r in zip(selection, results):
        if isinstance(r, BaseException):
            logger.error("[%s] task raised: %s (type=%s)", ticker, r, type(r).__name__)
        else:
            logger.debug("[%s] task result: %s", ticker, r)

    completed = sum(1 for r in results if r == "completed")
    cancelled = sum(1 for r in results if r == "cancelled")
    failed = len(results) - completed - cancelled
    parts = [f"{completed} completed"]
    if cancelled:
        parts.append(f"{cancelled} cancelled")
    if failed:
        parts.append(f"{failed} failed")
    final = f"✅ Queue done — {safe_list}: " + ", ".join(parts) + "\\."

    # Overwrite the original queue-header message instead of stacking a new
    # one — keeps the chat tidier. Falls back to a fresh message if the edit
    # fails (e.g., user deleted the header).
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=queue_msg_id,
            text=final,
            parse_mode="MarkdownV2",
        )
    except Exception:
        await context.bot.send_message(
            chat_id=chat_id, text=final, parse_mode="MarkdownV2"
        )


async def _handle_history(query, ticker: str, date_str: str) -> None:
    """Render a historical analysis selected via the date-picker keyboard."""
    caption = await build_history_response(ticker, date_str)
    back_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("← Back", callback_data=f"hist_back:dates:{ticker}")]]
    )
    await query.edit_message_text(
        caption, parse_mode="MarkdownV2", reply_markup=back_kb
    )


async def _handle_history_ticker(query, ticker: str) -> None:
    """Drill down from the ticker-picker into the date picker for `ticker`."""
    text, kb = build_history_dates_response(ticker)
    if kb is None:
        await query.edit_message_text(text, parse_mode="MarkdownV2")
    else:
        await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=kb)


async def _handle_history_back(query, target: str) -> None:
    """Navigate up one level in the /history flow.

    `target` is "tickers" (re-render ticker picker) or "dates:{ticker}"
    (re-render date picker for that ticker).
    """
    if target == "tickers":
        text, kb = build_history_tickers_response()
    elif target.startswith("dates:"):
        text, kb = build_history_dates_response(target.split(":", 1)[1])
    else:
        await query.edit_message_text("Cancelled.")
        return
    if kb is None:
        await query.edit_message_text(text, parse_mode="MarkdownV2")
    else:
        await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=kb)


async def _handle_del(query, user_id: int, ticker: str) -> None:
    """Remove a ticker via the inline-button picker and re-render the keyboard."""
    await watchlist_storage.remove_ticker(user_id, ticker)
    remaining = watchlist_storage.get_watchlist(user_id)
    if not remaining:
        await query.edit_message_text("Your watchlist is now empty.")
        return
    await query.edit_message_text(
        "Tap a ticker to remove it from your watchlist:",
        reply_markup=InlineKeyboardMarkup(build_del_keyboard(remaining)),
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
    entry["event"].set()
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


async def _handle_cancel(
    context: ContextTypes.DEFAULT_TYPE, query, user_id: int, what: str
) -> None:
    """Bail out of a multi-step flow. `what` names the flow being cancelled.

    For `config`, restores the (provider, deep, quick) snapshot taken when
    /config was first invoked, so any provider/model writes that happened
    mid-flow are rolled back.

    For `del`, just dismisses the picker (each ❌ tap committed already).
    """
    if what == "config":
        snapshot = context.user_data.pop("llm_snapshot", None)
        if snapshot is not None:
            if snapshot["provider"]:
                # Restore prior provider; set_llm_provider clears deep/quick,
                # so re-write them after.
                await user_config_storage.set_llm_provider(
                    user_id, snapshot["provider"]
                )
                if snapshot["deep"]:
                    await user_config_storage.set_llm_model(
                        user_id, "deep", snapshot["deep"]
                    )
                if snapshot["quick"]:
                    await user_config_storage.set_llm_model(
                        user_id, "quick", snapshot["quick"]
                    )
            else:
                # No prior provider — wipe whatever was just written.
                await user_config_storage.clear(user_id)
        await query.edit_message_text(
            "❌ LLM configuration cancelled — previous settings restored\\.",
            parse_mode="MarkdownV2",
        )
    elif what == "del":
        await query.edit_message_text("✅ Done\\.", parse_mode="MarkdownV2")
    elif what in ("watch", "hist", "digest"):
        try:
            await query.delete_message()
        except Exception:
            await query.edit_message_text("✅ Done\\.", parse_mode="MarkdownV2")
    else:
        await query.edit_message_text("Cancelled.")


async def _redraw_digest(query, user_id: int, mode: str) -> None:
    """Re-render the digest picker in `mode` (auto/hours/tz). Swallows the
    'message is not modified' BadRequest that fires when the user taps the
    same hour/tz they already had selected — there's nothing to update."""
    digest = user_config_storage.get_digest(user_id)
    text, kb = build_digest_response(digest, mode=mode)
    try:
        await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=kb)
    except Exception as e:
        logger.debug("digest redraw skipped: %s", e)


async def _handle_digest(
    query, context: ContextTypes.DEFAULT_TYPE, user_id: int, data: str
) -> None:
    """Dispatch `digest:*` callbacks emitted by the picker.

    Sub-actions:
      - `digest:hour:HH`    — set hour, enable, capture chat_id, redraw hours
      - `digest:tz:<IANA>`  — set tz; if active, stays active. Returns to hour grid.
      - `digest:tzpick`     — swap to the tz picker
      - `digest:hourpick`   — back-to-hours (← Back from tz screen)
      - `digest:off`        — disable (preserves hour + tz), redraw hours
      - `digest:run`        — fire fan-out now (stub until step 4 wires it)
    """
    chat_id = query.message.chat_id
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else None

    if action == "hour":
        try:
            hour = int(arg) if arg is not None else -1
        except ValueError:
            return
        await user_config_storage.set_digest_hour(user_id, hour, chat_id)
        await _redraw_digest(query, user_id, mode="hours")
    elif action == "tz":
        if arg and await user_config_storage.set_digest_tz(user_id, arg):
            # Tz pick lands you back on the hour grid — natural next step
            # for first-time setup; for a tz change it confirms by returning
            # to the screen showing the active digest.
            await _redraw_digest(query, user_id, mode="hours")
    elif action == "tzpick":
        await _redraw_digest(query, user_id, mode="tz")
    elif action == "hourpick":
        await _redraw_digest(query, user_id, mode="hours")
    elif action == "off":
        await user_config_storage.disable_digest(user_id)
        await _redraw_digest(query, user_id, mode="hours")
    elif action == "run":
        # Stub — wire to fan-out in the next commit. Sends a fresh message
        # so the picker stays interactable.
        await context.bot.send_message(
            chat_id,
            "🌙 Run-now fan-out is wired in the next commit.",
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch on the callback_data prefix."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data or ""

    if data.startswith("provider:"):
        await _handle_provider(query, user_id, data.split(":", 1)[1])
    elif data.startswith("deep:"):
        _, provider, model = data.split(":", 2)
        await _handle_deep(query, user_id, provider, model)
    elif data.startswith("quick:"):
        _, provider, model = data.split(":", 2)
        await _handle_quick(query, context, user_id, provider, model)
    elif data.startswith("multi:"):
        await _handle_select_toggle(query, context, user_id, data.split(":", 1)[1])
    elif data.startswith("wsel:"):
        await _handle_select_bulk(query, context, user_id, data.split(":", 1)[1])
    elif data.startswith("wpage:"):
        await _handle_page_nav(query, context, user_id, data.split(":", 1)[1])
    elif data.startswith("runall:"):
        await _handle_done(query, context, user_id)
    elif data.startswith("digest:"):
        await _handle_digest(query, context, user_id, data)
    elif data.startswith("del:"):
        await _handle_del(query, user_id, data.split(":", 1)[1])
    elif data.startswith("cancel_analysis:"):
        await _handle_cancel_analysis(context, query, data.split(":", 1)[1])
    elif data.startswith("cancel:"):
        await _handle_cancel(context, query, user_id, data.split(":", 1)[1])
    elif data.startswith("hist_back:"):
        await _handle_history_back(query, data.split(":", 1)[1])
    elif data.startswith("hist_t:"):
        await _handle_history_ticker(query, data.split(":", 1)[1])
    elif data.startswith("hist:"):
        _, ticker, date_str = data.split(":", 2)
        await _handle_history(query, ticker, date_str)
