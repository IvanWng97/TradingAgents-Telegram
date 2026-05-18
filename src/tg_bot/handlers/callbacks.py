"""Inline-button callback dispatch + picker UI.

The analysis-execution pipeline (cache lookup → semaphore → propagate →
Telegraph publish → render) and the digest scheduler live in
`analysis_runner.py`. This module owns the picker handlers (watchlist,
history, digest) and the `button_callback` prefix dispatcher.
"""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from tg_bot.pipeline.analysis import (
    check_llm_configured,
    llm_setup_error_message,
)
from tg_bot.digest import build_digest_response
from tg_bot.handlers.analysis_runner import (
    _handle_cancel_analysis,
    _handle_digest_cancel,
    _handle_get_full_md,
    _run_analysis_for_ticker,
    _run_digest_with_guard,
    cancel_digest_job,
    register_digest_job,
)
from tg_bot.handlers.pickers import (
    build_del_keyboard,
    build_history_dates_response,
    build_history_response,
    build_history_tickers_response,
    build_watchlist_response,
)
from tg_bot.storage import user_config_storage, watchlist_storage


logger = logging.getLogger(__name__)


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

    # Fail fast before launching N parallel tasks: a missing
    # TRADINGAGENTS_LLM_PROVIDER or its API key in .env would otherwise
    # produce N identical generic auth errors, one per ticker. Single
    # message at the entry point is much cleaner UX.
    reason = check_llm_configured()
    if reason is not None:
        await query.edit_message_text(
            llm_setup_error_message(reason), parse_mode="MarkdownV2"
        )
        return

    # Precheck passed — commit to running. Now drop the picker state.
    context.chat_data.pop("watch_mode", None)
    context.chat_data.pop("watch_selection", None)
    context.chat_data.pop("watch_page", None)

    # Refresh mode threads `force_refresh=True` into each run instead of
    # pre-invalidating the cache. `_run_analysis_for_ticker` then reads
    # the prior Telegraph URL from the cached entry (for edit_page) and
    # skips the cache-hit short-circuit so a fresh LLM run replaces the
    # entry in place. Pre-invalidation would drop the URL before we
    # could reuse it, defeating edit-in-place.
    force_refresh = mode == "refresh"

    if len(selection) == 1:
        # Single-ticker: replace the watchlist menu with the analysis flow.
        try:
            await query.delete_message()
        except Exception:
            pass
        await _run_analysis_for_ticker(
            context, chat_id, user_id, selection[0], force_refresh=force_refresh
        )
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
            _run_analysis_for_ticker(
                context, chat_id, user_id, ticker, force_refresh=force_refresh
            )
            for ticker in selection
        ),
        return_exceptions=True,
    )
    logger.debug("queue: gather returned %d results", len(results))
    for ticker, r in zip(selection, results, strict=True):
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
    caption, state, telegraph_url = await build_history_response(ticker, date_str)
    # Row 1: ← Back nav. Row 2 (only when a record exists): the action
    # buttons. State is None for missing logs — caption already says so.
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("← Back", callback_data=f"hist_back:dates:{ticker}")]
    ]
    if state is not None:
        action_row: list[InlineKeyboardButton] = []
        if telegraph_url:
            action_row.append(
                InlineKeyboardButton("📰 Instant View", url=telegraph_url)
            )
        action_row.append(
            InlineKeyboardButton(
                "📥 Download .md",
                callback_data=f"getmd:{ticker}:{date_str}",
            )
        )
        rows.append(action_row)
    await query.edit_message_text(
        caption, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
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


async def _handle_cancel(
    context: ContextTypes.DEFAULT_TYPE, query, user_id: int, what: str
) -> None:
    """Bail out of a multi-step flow. `what` names the flow being cancelled.

    For `del`, just dismisses the picker (each ❌ tap committed already).
    """
    if what == "del":
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
        # crafted button or a back-button race mid-edit could land here.
        # Decline and toast — never let a partial digest write happen.
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
        reason = check_llm_configured()
        if reason is not None:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=llm_setup_error_message(reason),
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


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch on the callback_data prefix."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data or ""

    if data.startswith("multi:"):
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
        parts = data.split(":", 2)
        if len(parts) == 3:
            await _handle_history(query, parts[1], parts[2])
        else:
            logger.warning("Malformed hist: callback_data %r", data)
    elif data.startswith("getmd:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            await _handle_get_full_md(query, context, parts[1], parts[2])
        else:
            logger.warning("Malformed getmd: callback_data %r", data)
