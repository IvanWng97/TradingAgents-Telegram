"""Inline-button callback handlers."""

import asyncio
import logging
import traceback

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
from tg_bot.handlers.commands import build_del_keyboard
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

    try:
        final_state, signal = await asyncio.to_thread(
            run_trading_analysis, ticker, user_id, user_config_storage
        )
        if final_state is None:
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                caption="Analysis failed. TradingAgents module not available.",
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
        )
    except Exception as e:
        logger.error("Error analyzing %s: %s", ticker, e)
        traceback.print_exc()
        await context.bot.edit_message_caption(
            chat_id=chat_id,
            message_id=progress_msg.message_id,
            caption=f"Error analyzing {ticker}.\n\nDetails: {str(e)[:200]}",
        )


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


async def _handle_cancel(query, what: str) -> None:
    """Bail out of a multi-step flow. `what` names the flow being cancelled."""
    if what == "config":
        await query.edit_message_text(
            "❌ LLM configuration cancelled — settings unchanged\\.",
            parse_mode="MarkdownV2",
        )
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
    elif data.startswith("cancel:"):
        await _handle_cancel(query, data.split(":", 1)[1])
