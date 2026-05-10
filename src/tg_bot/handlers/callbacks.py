"""Inline-button callback handlers."""

import asyncio
import logging
import threading
import time
import uuid
from datetime import date, time as dt_time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import markdown
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from tg_bot import cache as result_cache
from tg_bot.analysis import (
    TRADINGAGENTS_AVAILABLE,
    build_user_config,
    check_llm_configured,
    get_model_options,
    has_model_catalog,
    run_trading_analysis,
)
from tg_bot.config import Config
from tg_bot.chart import finviz_chart_url
from tg_bot.digest import build_digest_response
from tg_bot.formatters import (
    DECISION_EMOJI,
    build_config_summary,
    escape_md_v2_url,
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
from tg_bot.progress import (
    TOTAL_STEPS,
    CancelledByUserError,
    ProgressReporter,
    resolve_step,
)
from tg_bot.storage import user_config_storage, watchlist_storage
from tg_bot.storage.user_config import UserConfigStorage
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


def _llm_setup_error_message(reason: str) -> str:
    """Render the short reason from `check_llm_configured` as a friendly
    MarkdownV2 message for the user, including the next-step hint. Two
    flavors based on which failure mode we hit."""
    if reason.startswith("no provider"):
        return (
            "⚠️ *No LLM provider configured*\\.\n\n"
            "Tap /config to pick a provider \\+ deep/quick models, then try again\\."
        )
    # Mode B: provider picked, but matching env var missing. Format is
    # "deepseek picked but DEEPSEEK_API_KEY not set in .env".
    return (
        f"⚠️ *LLM key missing*\\.\n\n"
        f"`{escape_markdown(reason, version=2)}`\n\n"
        "Add the key to your `\\.env` and restart the bot \\(`docker\\-compose up \\-d`\\)\\."
    )


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
    # Two more steps follow: rounds (always) → effort (only for providers
    # with a thinking knob). Snapshot stays live until effort step (or the
    # rounds step for providers without effort) finishes.
    current_rounds = user_config_storage.get_max_debate_rounds(user_id)
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if n == current_rounds else ''}{n} — {label}",
                callback_data=f"rounds:{n}",
            )
        ]
        for n, label in [
            (1, "Fast (default, 1× cost)"),
            (2, "Balanced (~1.5× cost)"),
            (3, "Thorough (~2× cost)"),
        ]
    ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel:config")])
    await query.edit_message_text(
        f"Provider: `{provider}`\nDeep: `{deep}`\nQuick: `{model}`\n\n"
        "How many *bull/bear debate rounds*?\n"
        "Higher \\= more nuanced thesis, more LLM calls\\.",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _handle_rounds(
    query, context: ContextTypes.DEFAULT_TYPE, user_id: int, rounds: int
) -> None:
    if not await user_config_storage.set_max_debate_rounds(user_id, rounds):
        # Out-of-range value somehow reached us — bail without ending the flow.
        await query.answer("Invalid rounds value.", show_alert=False)
        return
    provider = user_config_storage.get_llm_provider(user_id)
    deep = user_config_storage.get_llm_model(user_id, "deep")
    quick = user_config_storage.get_llm_model(user_id, "quick")
    # Skip the effort step for providers without a thinking knob — for them
    # rounds is the last step, so finalize here.
    if provider not in UserConfigStorage.PROVIDERS_WITH_EFFORT:
        context.user_data.pop("llm_snapshot", None)
        await query.edit_message_text(
            "LLM configuration saved\\.\n\n"
            f"Provider: `{provider}`\nDeep: `{deep}`\nQuick: `{quick}`\n"
            f"Rounds: `{rounds}`",
            parse_mode="MarkdownV2",
        )
        return
    current_effort = user_config_storage.get_effort_level(user_id)
    rows = [
        [
            InlineKeyboardButton(
                f"{'✅ ' if (current_effort is None) else ''}Default (provider-decided)",
                callback_data="effort:none",
            )
        ]
    ]
    for level in UserConfigStorage.VALID_EFFORT_LEVELS:
        rows.append(
            [
                InlineKeyboardButton(
                    f"{'✅ ' if level == current_effort else ''}{level.title()}",
                    callback_data=f"effort:{level}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel:config")])
    await query.edit_message_text(
        f"Provider: `{provider}`\nDeep: `{deep}`\nQuick: `{quick}`\n"
        f"Rounds: `{rounds}`\n\n"
        "Reasoning *effort* on the deep\\-think model?\n"
        "Higher \\= deeper thinking on reasoning models, more tokens\\.",
        parse_mode="MarkdownV2",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def _handle_effort(
    query, context: ContextTypes.DEFAULT_TYPE, user_id: int, raw_level: str
) -> None:
    level = None if raw_level == "none" else raw_level
    if not await user_config_storage.set_effort_level(user_id, level):
        await query.answer("Invalid effort level.", show_alert=False)
        return
    provider = user_config_storage.get_llm_provider(user_id)
    deep = user_config_storage.get_llm_model(user_id, "deep")
    quick = user_config_storage.get_llm_model(user_id, "quick")
    rounds = user_config_storage.get_max_debate_rounds(user_id)
    # Effort is the last step — drop the rollback snapshot.
    context.user_data.pop("llm_snapshot", None)
    effort_display = level if level else "default"
    await query.edit_message_text(
        "LLM configuration saved\\.\n\n"
        f"Provider: `{provider}`\nDeep: `{deep}`\nQuick: `{quick}`\n"
        f"Rounds: `{rounds}`\nEffort: `{effort_display}`",
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
    # /status counter — counts each analysis attempt across the whole bot
    # (both cache hits and live runs, since the user requested an analysis
    # either way).
    context.bot_data["analysis_count"] = context.bot_data.get("analysis_count", 0) + 1

    # Cache short-circuit: if an identical analysis already ran today
    # (same provider + deep + quick + rounds + effort + ticker, any user),
    # render directly from the cached final state and skip the LLM run,
    # the progress flow, the Telegraph round-trip, and the cancel plumbing.
    config = build_user_config(user_id, user_config_storage)
    today_iso = date.today().isoformat()
    cache_kwargs = result_cache.cache_key_extras(config)
    cached = result_cache.lookup(
        config["llm_provider"],
        config["deep_think_llm"],
        config["quick_think_llm"],
        ticker,
        today_iso,
        **cache_kwargs,
    )
    if cached:
        logger.info("[%s] result_cache HIT — skipping LLM run", ticker)
        chart_url = finviz_chart_url(ticker)
        summary = extract_summary(cached["final_state"].get("final_trade_decision", ""))
        caption = format_short_message(
            ticker,
            cached["signal"],
            cached.get("telegraph_url"),
            summary=summary,
            config_summary=build_config_summary(config),
            generated_at=result_cache.parse_generated_at(cached.get("generated_at")),
        )
        try:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=chart_url,
                caption=caption,
                parse_mode="MarkdownV2",
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
        caption = format_short_message(
            ticker,
            signal,
            telegraph_url,
            summary=summary,
            config_summary=build_config_summary(config),
        )
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption=caption,
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
        # Persist for the rest of today so the next /watch tap or digest
        # fire on this ticker (any user with the same config) hits the
        # short-circuit at the top of the function. Best-effort — write
        # failures are logged but don't fail the user-facing flow.
        result_cache.store(
            config["llm_provider"],
            config["deep_think_llm"],
            config["quick_think_llm"],
            ticker,
            today_iso,
            final_state,
            signal,
            telegraph_url,
            **cache_kwargs,
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
    mode = context.chat_data.get("watch_mode", "watch")
    text, kb = build_watchlist_response(
        user_id, selected=selection, page=page, mode=mode
    )
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
    elif action == "clear":
        selection = set()
    else:
        # Unknown wsel:* sub-action — log and bail rather than silently
        # falling through to "clear", which would surprise a future
        # contributor who adds e.g. wsel:invert.
        logger.warning("_handle_select_bulk: unknown action=%r", action)
        return
    context.chat_data["watch_selection"] = selection
    page = context.chat_data.get("watch_page", 0)
    mode = context.chat_data.get("watch_mode", "watch")
    text, kb = build_watchlist_response(
        user_id, selected=selection, page=page, mode=mode
    )
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
    mode = context.chat_data.get("watch_mode", "watch")
    text, kb = build_watchlist_response(
        user_id, selected=selection, page=page, mode=mode
    )
    if kb is None:
        await query.edit_message_text(text)
    else:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="MarkdownV2")


async def _handle_done(query, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Unified entry point for both single and multi-ticker analysis runs.

    Mode-aware via `chat_data["watch_mode"]`:
      - "watch": runs cache-aware (lookup short-circuits before LLM call).
      - "refresh": invalidates today's cache for each selected ticker
        first, so every selection pays for a fresh LLM run. Same picker,
        same dispatch, only difference is the pre-flight invalidation.

    1 selected → cached graph (fast init, no parallel benefit anyway).
    N selected → fresh graph per ticker, run in parallel via asyncio.gather.
    """
    selection = sorted(context.chat_data.get("watch_selection") or set())
    if not selection:
        await query.answer("No tickers selected.", show_alert=True)
        return
    chat_id = query.message.chat_id

    # Read mode without popping yet — if the precheck fails we want chat_data
    # intact so the next /refresh attempt still sees the right mode (the
    # picker's edit-message-text replaces the keyboard, but we still want
    # state-coherent state for the user re-running the command).
    mode = context.chat_data.get("watch_mode", "watch")

    # Fail fast before launching N parallel tasks: a missing /config or
    # missing API key would otherwise produce N identical generic auth
    # errors, one per ticker. Single message at the entry point is much
    # cleaner UX.
    reason = check_llm_configured(user_id, user_config_storage)
    if reason is not None:
        await query.edit_message_text(
            _llm_setup_error_message(reason), parse_mode="MarkdownV2"
        )
        return

    # Precheck passed — commit to running. Now drop the picker state.
    context.chat_data.pop("watch_mode", None)
    context.chat_data.pop("watch_selection", None)
    context.chat_data.pop("watch_page", None)

    # Refresh mode: drop today's cache entries for each selected ticker
    # so the upcoming `_run_analysis_for_ticker` calls all miss and pay
    # for fresh LLM runs. Single config build covers all tickers since
    # the cache key uses the same provider/deep/quick/rounds/effort.
    if mode == "refresh":
        config = build_user_config(user_id, user_config_storage)
        today_iso = date.today().isoformat()
        cache_kwargs = result_cache.cache_key_extras(config)
        invalidated = 0
        for ticker in selection:
            if result_cache.invalidate(
                config["llm_provider"],
                config["deep_think_llm"],
                config["quick_think_llm"],
                ticker,
                today_iso,
                **cache_kwargs,
            ):
                invalidated += 1
        logger.info(
            "refresh: invalidated %d/%d cache entries for %s",
            invalidated,
            len(selection),
            selection,
        )

    if len(selection) == 1:
        # Single-ticker: replace the watchlist menu with the analysis flow.
        try:
            await query.delete_message()
        except Exception:
            pass
        await _run_analysis_for_ticker(context, chat_id, user_id, selection[0])
        return

    # Multi-ticker: parallel runs share the per-key graph pool — first run
    # in a fresh pool pays init cost, subsequent reuse warm instances. The
    # header verb mirrors the picker so the user's "🔄 Refresh (N)" tap
    # transitions to "🔄 Refreshing", not the generic queue verb.
    safe_list = escape_markdown(", ".join(selection), version=2)
    queue_msg_id = query.message.message_id
    header_verb = "🔄 Refreshing" if mode == "refresh" else "🚀 Running queue"
    try:
        await query.edit_message_text(
            f"{header_verb}: {safe_list}\n\n"
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
        await query.edit_message_text("Cancelled\\.", parse_mode="MarkdownV2")
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
            # Restore rounds + effort regardless of provider — they're
            # provider-agnostic, so they survived the wipe above and any
            # mid-flow rounds:/effort: write needs rolling back too.
            await user_config_storage.set_max_debate_rounds(
                user_id, snapshot.get("rounds") or 1
            )
            await user_config_storage.set_effort_level(user_id, snapshot.get("effort"))
        await query.edit_message_text(
            "❌ LLM configuration cancelled — previous settings restored\\.",
            parse_mode="MarkdownV2",
        )
    elif what == "del":
        await query.edit_message_text("✅ Done\\.", parse_mode="MarkdownV2")
    elif what in ("watch", "hist", "digest"):
        if what == "watch":
            # Mirror _handle_done: drop the picker state so a stale
            # wsel:/wpage:/multi: callback from an older message can't
            # mutate it before the next /watch or /refresh re-initializes.
            context.chat_data.pop("watch_mode", None)
            context.chat_data.pop("watch_selection", None)
            context.chat_data.pop("watch_page", None)
        try:
            await query.delete_message()
        except Exception:
            await query.edit_message_text("✅ Done\\.", parse_mode="MarkdownV2")
    else:
        await query.edit_message_text("Cancelled\\.", parse_mode="MarkdownV2")


async def _redraw_digest(query, user_id: int, mode: str, page: int = 0) -> None:
    """Re-render the digest picker in `mode` (auto/hours/tz/tickers).

    Threads the watchlist through `build_digest_response` so the hour screen
    can show the 📋 Tickers (N/M) button and the tickers screen has the
    toggle grid. Swallows the 'message is not modified' BadRequest that fires
    when the user taps a no-op (e.g. the same hour they already had)."""
    digest = user_config_storage.get_digest(user_id)
    watchlist = watchlist_storage.get_watchlist(user_id)
    text, kb = build_digest_response(digest, mode=mode, watchlist=watchlist, page=page)
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
      - `digest:hourpick`   — back-to-hours (← Back from tz/tickers screen)
      - `digest:off`        — disable (preserves hour + tz + tickers), redraw hours
      - `digest:run`        — fire fan-out now
      - `digest:tickerpick` — swap to the ticker filter screen
      - `digest:tt:<T>`     — toggle one ticker in the digest filter (per-tap save)
      - `digest:ttall`      — select every ticker on the watchlist
      - `digest:ttclear`    — clear the digest ticker filter
      - `digest:ttpage:{prev,next,noop}` — paginate the tickers screen
    """
    chat_id = query.message.chat_id
    parts = data.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else None

    if action == "hour":
        try:
            hour = int(arg) if arg is not None else -1
        except ValueError:
            await query.answer("Invalid hour.", show_alert=True)
            return
        if await user_config_storage.set_digest_hour(user_id, hour, chat_id):
            register_digest_job(
                context.application, user_id, user_config_storage.get_digest(user_id)
            )
            await _redraw_digest(query, user_id, mode="hours")
        else:
            await query.answer("Invalid hour.", show_alert=True)
    elif action == "tz":
        if arg and await user_config_storage.set_digest_tz(user_id, arg):
            # Tz pick lands you back on the hour grid — natural next step
            # for first-time setup; for a tz change it confirms by returning
            # to the screen showing the active digest. If the digest is
            # active, also re-register so the new tz takes effect.
            digest = user_config_storage.get_digest(user_id)
            if digest and digest.get("enabled"):
                register_digest_job(context.application, user_id, digest)
            await _redraw_digest(query, user_id, mode="hours")
        else:
            await query.answer("Invalid time zone.", show_alert=True)
    elif action == "tzpick":
        await _redraw_digest(query, user_id, mode="tz")
    elif action == "hourpick":
        # Reset ticker pagination so the user lands on page 1 next time
        # they open the filter — predictable.
        context.chat_data.pop("digest_tickers_page", None)
        await _redraw_digest(query, user_id, mode="hours")
    elif action == "tickerpick":
        # Same tz gate as the tt-* writes — a stale callback for a user with
        # no tz configured renders an awkward "pick a time zone" status line
        # under a ticker grid. Decline + toast keeps the flow consistent.
        digest = user_config_storage.get_digest(user_id)
        if not digest or not digest.get("tz"):
            await query.answer("Set a time zone first via /digest.", show_alert=True)
            return
        context.chat_data["digest_tickers_page"] = 0
        await _redraw_digest(query, user_id, mode="tickers", page=0)
    elif action in ("tt", "ttall", "ttclear"):
        # Defensive gate: a stale callback button could fire `tt:*` for a
        # user who has no digest configured yet (no tz/hour). The picker UI
        # never exposes the ticker screen before tz is set, but a hand-
        # crafted button or a /config Cancel mid-edit could land here. Decline
        # and toast — never let a partial digest write happen.
        digest = user_config_storage.get_digest(user_id)
        if not digest or not digest.get("tz"):
            await query.answer("Set a time zone first via /digest.", show_alert=True)
            return

        if action == "tt":
            # Toggle one ticker. Validate against the live watchlist so a stale
            # callback button (e.g. ticker just removed via /del in another
            # session) can't sneak a non-watchlist symbol into the filter.
            watchlist = set(watchlist_storage.get_watchlist(user_id))
            if not arg or arg not in watchlist:
                await query.answer(
                    "Ticker no longer in your watchlist.", show_alert=True
                )
                await _redraw_digest(
                    query,
                    user_id,
                    mode="tickers",
                    page=context.chat_data.get("digest_tickers_page", 0),
                )
                return
            selected = set(digest.get("tickers") or [])
            if arg in selected:
                selected.discard(arg)
            else:
                selected.add(arg)
            await user_config_storage.set_digest_tickers(user_id, sorted(selected))
        elif action == "ttall":
            wl = watchlist_storage.get_watchlist(user_id)
            # Skip the write when the filter already matches — saves an fsync
            # on a no-op tap (common when a user double-taps Select all).
            if set(digest.get("tickers") or []) != set(wl):
                await user_config_storage.set_digest_tickers(user_id, wl)
        else:  # ttclear
            if digest.get("tickers"):
                await user_config_storage.set_digest_tickers(user_id, [])

        await _redraw_digest(
            query,
            user_id,
            mode="tickers",
            page=context.chat_data.get("digest_tickers_page", 0),
        )
    elif action == "ttpage":
        # `arg` is "prev" / "next" / "noop". Page index lives in chat_data
        # so the user keeps their place across toggles within a session.
        page = context.chat_data.get("digest_tickers_page", 0)
        if arg == "next":
            page += 1
        elif arg == "prev":
            page = max(0, page - 1)
        # else: "noop" — central indicator, just redraw current page
        context.chat_data["digest_tickers_page"] = page
        await _redraw_digest(query, user_id, mode="tickers", page=page)
    elif action == "off":
        await user_config_storage.disable_digest(user_id)
        cancel_digest_job(context.application, user_id)
        await _redraw_digest(query, user_id, mode="hours")
    elif action == "run":
        # Fail fast on missing LLM setup before fan-out — otherwise every
        # ticker's analysis would 401 the same way and the digest message
        # would be a wall of identical errors.
        reason = check_llm_configured(user_id, user_config_storage)
        if reason is not None:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=_llm_setup_error_message(reason),
                    parse_mode="MarkdownV2",
                )
            except Exception as e:
                logger.warning("digest:run unconfigured-notice send failed: %s", e)
            await query.answer()
            return
        # Fire-and-forget so the callback returns immediately; the digest
        # may take minutes if the watchlist is long. The fan-out itself
        # serializes through the existing _run_semaphore so manual
        # /watch runs aren't starved.
        #
        # Re-entry guard: the picker stays open while a digest is running,
        # and a user mashing ▶ Run now would otherwise spawn N parallel
        # fan-outs, each posting its own header. A simple bool in chat_data
        # blocks repeats; cleared in the run_user_digest finally.
        running_key = f"digest_running:{user_id}"
        if context.chat_data.get(running_key):
            await query.answer("A digest is already running.", show_alert=True)
            return
        context.chat_data[running_key] = True
        asyncio.create_task(
            _run_digest_with_guard(
                context.chat_data, running_key, context.application, user_id, chat_id
            )
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
    # Cache short-circuit before acquiring a semaphore slot — a cached
    # result needs no LLM call, so we shouldn't burn a slot or trigger
    # the "Starting…" reporter event.
    config = build_user_config(user_id, user_config_storage)
    today_iso = date.today().isoformat()
    cache_kwargs = result_cache.cache_key_extras(config)
    cached = result_cache.lookup(
        config["llm_provider"],
        config["deep_think_llm"],
        config["quick_think_llm"],
        ticker,
        today_iso,
        **cache_kwargs,
    )
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
                user_id,
                user_config_storage,
                reporter,
            )
        except Exception:
            logger.exception("digest: analysis failed for %s", ticker)
            return None
        if final_state is None:
            return None

        # Publish the per-ticker Telegraph page so the digest summary can
        # link to a full report per row. Skip silently on failure — the
        # row just renders without a link.
        chart_url = finviz_chart_url(ticker)
        try:
            md = format_analysis_result_markdown(ticker, final_state, signal)
            html = markdown.markdown(md, extensions=["tables"])
            html = f'<img src="{chart_url}"/>{html}'
            telegraph_url = await publish_to_telegraph(f"{ticker} Analysis", html)
        except Exception as e:
            logger.warning("digest: telegraph publish failed for %s: %s", ticker, e)
            telegraph_url = None

        # Persist for the rest of today — same key the manual /watch flow
        # writes, so a digest fan-out followed by a manual /watch tap on
        # the same ticker only pays for one actual LLM run.
        result_cache.store(
            config["llm_provider"],
            config["deep_think_llm"],
            config["quick_think_llm"],
            ticker,
            today_iso,
            final_state,
            signal,
            telegraph_url,
            **cache_kwargs,
        )

        return {"ticker": ticker, "signal": signal, "telegraph_url": telegraph_url}
    finally:
        if acquired:
            sem.release()


_DIGEST_PROGRESS_INTERVAL = 2.0  # min seconds between progressive edits


def _completed_digest_row(ticker_v2: str, result: dict) -> str:
    """One MarkdownV2-safe row for a completed ticker. Used by both the
    progress view and the final summary so a row's appearance is stable
    once analysis lands — no jump from generic ✅ to a signal-coloured
    emoji at the final-edit boundary."""
    signal = (result.get("signal") or "—").strip()
    emoji = DECISION_EMOJI.get(signal.upper(), "📊")
    signal_v2 = escape_markdown(signal, version=2)
    if result.get("telegraph_url"):
        url = escape_md_v2_url(result["telegraph_url"])
        return f"{emoji} *{ticker_v2}* — *{signal_v2}* [📄]({url})"
    return f"{emoji} *{ticker_v2}* — *{signal_v2}*"


def _format_digest_progress(
    watchlist: list[str],
    status: dict[str, object],
    safe_date: str,
) -> str:
    """In-progress view. `status[ticker]` is one of:
      - "pending"           → ⏳ TICKER
      - ("analyzing", friendly, ordinal) → 📊 TICKER — Friendly (n/M)
      - "cancelled"         → ⛔ TICKER — cancelled
      - dict (result)       → ✅ TICKER — SIGNAL
      - None                → ❌ TICKER — error
    Watchlist order is preserved so the user can see exactly where the
    fan-out is at any point.
    """
    done = sum(1 for s in status.values() if not (s == "pending" or _is_analyzing(s)))
    n = len(watchlist)
    lines = [f"🌙 *Daily Digest* — {safe_date}  \\({done}/{n}\\)\n"]
    for ticker in watchlist:
        s = status[ticker]
        ticker_v2 = escape_markdown(ticker, version=2)
        if s == "pending":
            lines.append(f"⏳ {ticker_v2}")
        elif _is_analyzing(s):
            _, friendly, ordinal = s  # type: ignore[misc]
            friendly_v2 = escape_markdown(friendly, version=2)
            if ordinal is not None:
                lines.append(
                    f"📊 *{ticker_v2}* — {friendly_v2} \\({ordinal}/{TOTAL_STEPS}\\)"
                )
            else:
                lines.append(f"📊 *{ticker_v2}* — {friendly_v2}")
        elif s == "cancelled":
            lines.append(f"⛔ *{ticker_v2}* — cancelled")
        elif s is None:
            lines.append(f"❌ *{ticker_v2}* — error")
        else:
            lines.append(_completed_digest_row(ticker_v2, s))  # type: ignore[arg-type]
    return "\n".join(lines)


def _is_analyzing(s: object) -> bool:
    return isinstance(s, tuple) and len(s) == 3 and s[0] == "analyzing"


def _format_digest_summary(
    watchlist: list[str],
    status: dict[str, object],
    safe_date: str,
) -> str:
    """Final view: signal-emoji per row + Telegraph link, watchlist order."""
    lines = [f"🌙 *Daily Digest* — {safe_date}\n"]
    failed = cancelled = 0
    n = len(watchlist)
    for ticker in watchlist:
        s = status[ticker]
        ticker_v2 = escape_markdown(ticker, version=2)
        if s == "cancelled":
            lines.append(f"⛔ *{ticker_v2}* — cancelled")
            cancelled += 1
            continue
        # Anything else (None from a real failure, or — much more rarely —
        # leftover "pending"/"analyzing" from an interruption that didn't
        # route through the cancel path) renders as ❓ and counts as failed.
        if not isinstance(s, dict):
            lines.append(f"❓ *{ticker_v2}* — error")
            failed += 1
            continue
        lines.append(_completed_digest_row(ticker_v2, s))
    tally_parts: list[str] = []
    if cancelled:
        tally_parts.append(f"{cancelled} cancelled")
    if failed:
        tally_parts.append(f"{failed} failed")
    if tally_parts:
        lines.append(f"\n_{', '.join(tally_parts)} of {n}_")
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
    today = date.today().isoformat()
    safe_date = escape_markdown(today, version=2)

    if not full_watchlist:
        logger.info("digest: skipping user %s — empty watchlist", user_id)
        return

    # Belt-and-suspenders LLM precheck: the manual ▶ Run now path also
    # gates upstream, but the daily JobQueue callback comes through here
    # too — and a user who enabled digest before running /config (or with
    # a stale .env) would otherwise see a wall of identical 401 errors.
    reason = check_llm_configured(user_id, user_config_storage)
    if reason is not None:
        logger.info(
            "digest: skipping user %s — LLM not configured (%s)", user_id, reason
        )
        try:
            await application.bot.send_message(
                chat_id=chat_id,
                text=_llm_setup_error_message(reason),
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
    # tickers the user removed since the filter was last edited). When `tickers`
    # is missing entirely (legacy save predating the filter feature), fall back
    # to the full watchlist for backward compat — the _post_init migration
    # backfills these on next startup.
    digest_block = user_config_storage.get_digest(user_id) or {}
    raw_filter = digest_block.get("tickers")
    if raw_filter is None:
        watchlist = full_watchlist
    else:
        # `t in full_watchlist` is implicit — t is iterated from it.
        filter_set = set(raw_filter)
        watchlist = [t for t in full_watchlist if t in filter_set]

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
                    f"🌙 *Daily Digest* — {safe_date}\n\n"
                    "_No tickers selected\\. Open /digest and tap "
                    "📋 Tickers to pick what to include\\._"
                ),
                parse_mode="MarkdownV2",
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
            text=_format_digest_progress(watchlist, status, safe_date),
            parse_mode="MarkdownV2",
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
    blocked = False

    async def _render() -> None:
        """Single-flight, throttle-deferring render.

        If two state changes fire within the throttle window (common at
        fan-out start when N tickers acquire the sem simultaneously),
        only the first one schedules an edit — the rest bail. The
        scheduled edit waits out the remaining cooldown and then reads
        the *current* status dict, so the second ticker's transition
        from ⏳ → 📊 isn't lost just because it raced with the first.
        """
        nonlocal last_edit_at, edit_in_flight, blocked
        if blocked or edit_in_flight:
            return
        edit_in_flight = True
        try:
            wait = _DIGEST_PROGRESS_INTERVAL - (time.monotonic() - last_edit_at)
            if wait > 0:
                await asyncio.sleep(wait)
            if blocked:
                return
            last_edit_at = time.monotonic()
            text = _format_digest_progress(watchlist, status, safe_date)
            try:
                # Re-attach the cancel button on every edit — Telegram
                # drops reply_markup unless re-sent with each edit.
                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=header.message_id,
                    text=text,
                    parse_mode="MarkdownV2",
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
            except Exception as e:
                # "message is not modified" or transient — keep going.
                logger.debug("digest progress edit skipped: %s", e)
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
            text=_format_digest_summary(watchlist, status, safe_date),
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
    except Forbidden:
        logger.warning("digest: user %s blocked the bot mid-run, disabling", user_id)
        await user_config_storage.disable_digest(user_id)
        cancel_digest_job(application, user_id)
    except Exception as e:
        logger.exception("digest: failed to edit summary for user %s: %s", user_id, e)


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
    elif data.startswith("rounds:"):
        try:
            rounds = int(data.split(":", 1)[1])
        except ValueError:
            return
        await _handle_rounds(query, context, user_id, rounds)
    elif data.startswith("effort:"):
        await _handle_effort(query, context, user_id, data.split(":", 1)[1])
    elif data.startswith("multi:"):
        await _handle_select_toggle(query, context, user_id, data.split(":", 1)[1])
    elif data.startswith("wsel:"):
        await _handle_select_bulk(query, context, user_id, data.split(":", 1)[1])
    elif data.startswith("wpage:"):
        await _handle_page_nav(query, context, user_id, data.split(":", 1)[1])
    elif data.startswith("runall:"):
        await _handle_done(query, context, user_id)
    elif data.startswith("digest_cancel:"):
        await _handle_digest_cancel(context, query, data.split(":", 1)[1])
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
