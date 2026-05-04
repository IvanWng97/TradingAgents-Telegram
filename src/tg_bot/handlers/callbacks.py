"""Inline-button callback handlers."""

import asyncio
import logging
import threading

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
from tg_bot.chart import finviz_chart_url
from tg_bot.formatters import format_analysis_result_markdown, format_short_message
from tg_bot.handlers.commands import (
    build_del_keyboard,
    build_history_dates_response,
    build_history_response,
    build_history_tickers_response,
)
from tg_bot.progress import CancelledByUserError, ProgressReporter
from tg_bot.storage import user_config_storage, watchlist_storage
from tg_bot.telegraph_client import publish_to_telegraph


logger = logging.getLogger(__name__)


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


async def _handle_quick(query, user_id: int, provider: str, model: str) -> None:
    await user_config_storage.set_llm_model(user_id, "quick", model)
    deep = user_config_storage.get_llm_model(user_id, "deep")
    await query.edit_message_text(
        "LLM configuration saved\\.\n\n"
        f"Provider: `{provider}`\nDeep: `{deep}`\nQuick: `{model}`",
        parse_mode="MarkdownV2",
    )


async def _handle_info(
    query, context: ContextTypes.DEFAULT_TYPE, user_id: int, ticker: str
) -> None:
    chat_id = query.message.chat_id
    chart_url = finviz_chart_url(ticker)
    logger.info("[%s] chart_url=%s", ticker, chart_url)

    # Replace the watchlist menu with a chart + "analyzing" caption.
    # send_photo can't replace a text message in place, so we delete
    # the original and post a fresh photo message that we'll edit later.
    try:
        await query.delete_message()
    except Exception:
        pass

    progress_msg = await context.bot.send_photo(
        chat_id=chat_id,
        photo=chart_url,
        caption=f"📊 Analyzing *{escape_markdown(ticker, version=2)}*… please wait\\.",
        parse_mode="MarkdownV2",
    )

    if not TRADINGAGENTS_AVAILABLE:
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption="TradingAgents module not available.",
        )
        return

    # Per-run cancellation: button on the progress photo sets this event,
    # ProgressReporter checks it at each step boundary and raises
    # CancelledByUserError to abort the pipeline.
    cancel_event = threading.Event()
    cancel_registry = context.chat_data.setdefault("analysis_cancels", {})
    cancel_registry[progress_msg.message_id] = cancel_event

    cancel_kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"cancel_analysis:{progress_msg.message_id}",
                )
            ]
        ]
    )
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            reply_markup=cancel_kb,
        )
    except Exception:
        # Keyboard is best-effort — analysis still runs without it.
        pass

    reporter = ProgressReporter(
        bot=context.bot,
        chat_id=chat_id,
        message_id=progress_msg.message_id,
        ticker=ticker,
        loop=asyncio.get_running_loop(),
        cancel_event=cancel_event,
    )

    try:
        final_state, signal = await asyncio.to_thread(
            run_trading_analysis, ticker, user_id, user_config_storage, reporter
        )
        if final_state is None:
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                caption="Analysis failed. TradingAgents module not available.",
                reply_markup=None,
            )
            return

        markdown_content = format_analysis_result_markdown(ticker, final_state, signal)
        html_content = f'<img src="{chart_url}"/>{markdown.markdown(markdown_content)}'
        telegraph_url = await publish_to_telegraph(f"{ticker} Analysis", html_content)

        caption = format_short_message(ticker, signal, telegraph_url)
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption=caption,
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
    except CancelledByUserError:
        logger.info("Analysis cancelled by user for %s", ticker)
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption=f"❌ Analysis cancelled for *{escape_markdown(ticker, version=2)}*\\.",
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
    except Exception as e:
        logger.exception("Error analyzing %s", ticker)
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption=f"Error analyzing {ticker}.\n\nDetails: {str(e)[:200]}",
            reply_markup=None,
        )
    finally:
        cancel_registry.pop(progress_msg.message_id, None)


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
    context: ContextTypes.DEFAULT_TYPE, query, message_id_str: str
) -> None:
    """Set the cancel flag for a running analysis on a given message.

    The actual abort happens at the next pipeline step boundary inside
    ProgressReporter — the in-flight LLM call still completes. We update
    the caption immediately so the user sees acknowledgement.
    """
    try:
        message_id = int(message_id_str)
    except ValueError:
        return
    cancel_registry = context.chat_data.get("analysis_cancels") or {}
    event = cancel_registry.get(message_id)
    if event is None:
        # Already finished or never registered — nothing to abort.
        return
    event.set()
    try:
        await context.bot.edit_message_caption(
            chat_id=query.message.chat_id,
            message_id=message_id,
            caption=(
                "❌ Cancelling… will stop after the current step finishes\\."
            ),
            parse_mode="MarkdownV2",
            reply_markup=None,
        )
    except Exception as e:
        logger.debug("Cancel-acknowledgement edit skipped: %s", e)


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
    elif what in ("watch", "hist"):
        try:
            await query.delete_message()
        except Exception:
            await query.edit_message_text("✅ Done\\.", parse_mode="MarkdownV2")
    else:
        await query.edit_message_text("Cancelled.")


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
        await _handle_quick(query, user_id, provider, model)
    elif data.startswith("info:"):
        await _handle_info(query, context, user_id, data.split(":", 1)[1])
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
