"""
Callback handlers for button clicks.
"""
import asyncio
import logging
import traceback
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
import markdown

from storage import user_config_storage
from analysis import (
    run_trading_analysis,
    TRADINGAGENTS_AVAILABLE,
    get_model_options,
    has_model_catalog,
)
from utils import (
    publish_to_telegraph,
    format_analysis_result_markdown,
    format_short_message,
    finviz_chart_url,
)

logger = logging.getLogger(__name__)


def _model_keyboard(mode: str, provider: str):
    """Build a one-button-per-row keyboard for picking a deep/quick model."""
    keyboard = []
    for label, model_id in get_model_options(provider, mode):
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"{mode}:{provider}:{model_id}")]
        )
    return InlineKeyboardMarkup(keyboard)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button clicks."""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data and query.data.startswith("provider:"):
        provider = query.data.split(":", 1)[1]

        if not user_config_storage.set_llm_provider(user_id, provider):
            await query.edit_message_text(
                f"Failed to set provider to `{provider}`.", parse_mode="Markdown"
            )
            return

        if not has_model_catalog(provider):
            await query.edit_message_text(
                f"Provider set to `{provider}`.\n\n"
                "This provider needs a custom model ID — model selection isn't "
                "wired up for it yet, so the run will use DEFAULT_CONFIG models.",
                parse_mode="Markdown",
            )
            return

        await query.edit_message_text(
            f"Provider: `{provider}`\n\nChoose a *deep-think* model:",
            parse_mode="Markdown",
            reply_markup=_model_keyboard("deep", provider),
        )

    elif query.data and query.data.startswith("deep:"):
        _, provider, model = query.data.split(":", 2)
        user_config_storage.set_llm_model(user_id, "deep", model)
        await query.edit_message_text(
            f"Provider: `{provider}`\nDeep: `{model}`\n\nChoose a *quick-think* model:",
            parse_mode="Markdown",
            reply_markup=_model_keyboard("quick", provider),
        )

    elif query.data and query.data.startswith("quick:"):
        _, provider, model = query.data.split(":", 2)
        user_config_storage.set_llm_model(user_id, "quick", model)
        deep = user_config_storage.get_llm_model(user_id, "deep")
        await query.edit_message_text(
            "LLM configuration saved.\n\n"
            f"Provider: `{provider}`\nDeep: `{deep}`\nQuick: `{model}`",
            parse_mode="Markdown",
        )

    elif query.data and query.data.startswith("info:"):
        ticker = query.data.split(":", 1)[1]
        chat_id = update.effective_chat.id
        chart_url = finviz_chart_url(ticker)
        logger.info("[%s] initial chart_url=%s", ticker, chart_url)
        print(f"Chart URL for {ticker}: {chart_url}")

        # Replace the watchlist menu with a chart + "analyzing" caption.
        # send_photo can't replace edit a text message in place, so we delete
        # the original and post a fresh photo message that we'll edit later.
        try:
            await query.delete_message()
        except Exception:
            pass
        progress_msg = await context.bot.send_photo(
            chat_id=chat_id,
            photo=chart_url,
            caption=f"📊 Analyzing *{ticker}*… please wait.",
            parse_mode="Markdown",
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
                run_trading_analysis,
                ticker,
                user_id,
                user_config_storage,
            )

            if final_state is None:
                await context.bot.edit_message_caption(
                    chat_id=chat_id,
                    message_id=progress_msg.message_id,
                    caption="Analysis failed. TradingAgents module not available.",
                )
                return

            markdown_content = format_analysis_result_markdown(ticker, final_state, signal)
            html_content = markdown.markdown(markdown_content)
            # Embed chart at the top of the Telegraph page.
            logger.info("[%s] telegraph chart_url=%s", ticker, chart_url)
            html_content = f'<img src="{chart_url}"/>{html_content}'
            telegraph_url = await publish_to_telegraph(f"{ticker} Analysis", html_content)

            caption = format_short_message(ticker, signal, telegraph_url)
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                caption=caption,
                parse_mode="MarkdownV2",
            )

        except Exception as e:
            logger.error(f"Error analyzing {ticker}: {e}")
            traceback.print_exc()
            await context.bot.edit_message_caption(
                chat_id=chat_id,
                message_id=progress_msg.message_id,
                caption=f"Error analyzing {ticker}.\n\nDetails: {str(e)[:200]}",
            )
