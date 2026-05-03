"""
Command handlers for the bot.
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from storage import UserConfigStorage, watchlist_storage as storage, user_config_storage

logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    await update.message.reply_text(
        "Welcome to TradingAgents Bot!\n\n"
        "Available commands:\n"
        "/add <ticker> - Add a stock to watchlist\n"
        "/del <ticker> - Remove a stock from watchlist\n"
        "/watch or /list - Show your watchlist\n"
        "/config - Configure LLM provider\n"
        "/help - Show this help message"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    await update.message.reply_text(
        "Available commands:\n\n"
        "/add <ticker> - Add a stock to watchlist\n"
        "  Example: /add NVDA\n\n"
        "/del <ticker> - Remove a stock from watchlist\n"
        "  Example: /del NVDA\n\n"
        "/watch or /list - Show your watchlist with clickable buttons\n\n"
        "/config - Configure LLM provider (openai, google, anthropic, xai, openrouter, ollama)\n\n"
        "/start - Welcome message"
    )


async def add_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /add command."""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Please provide a ticker symbol.\n"
            "Example: /add NVDA"
        )
        return

    ticker = context.args[0].strip().upper()

    if storage.add_ticker(user_id, ticker):
        await update.message.reply_text(f"Added {ticker} to your watchlist!")
    else:
        await update.message.reply_text(f"{ticker} is already in your watchlist.")


async def del_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /del command."""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "Please provide a ticker symbol.\n"
            "Example: /del NVDA"
        )
        return

    ticker = context.args[0].strip().upper()

    if storage.remove_ticker(user_id, ticker):
        await update.message.reply_text(f"Removed {ticker} from your watchlist.")
    else:
        await update.message.reply_text(
            f"{ticker} is not in your watchlist.\n"
            "Use /watch to see your current watchlist."
        )


async def list_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /watch and /list commands."""
    user_id = update.effective_user.id

    watchlist = storage.get_watchlist(user_id)

    if not watchlist:
        await update.message.reply_text(
            "Your watchlist is empty.\n"
            "Use /add <ticker> to add stocks."
        )
        return

    # Create inline keyboard with ticker buttons
    keyboard = []
    for i in range(0, len(watchlist), 3):
        row = [
            InlineKeyboardButton(ticker, callback_data=f"info:{ticker}")
            for ticker in watchlist[i:i+3]
        ]
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)

    message = f"*Your Watchlist ({len(watchlist)} stocks):*\n\n" + "\n".join(f"• {t}" for t in watchlist)

    await update.message.reply_text(
        message,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /config command."""
    user_id = update.effective_user.id
    current_provider = user_config_storage.get_llm_provider(user_id) or "default (openai)"
    current_deep = user_config_storage.get_llm_model(user_id, "deep") or "default"
    current_quick = user_config_storage.get_llm_model(user_id, "quick") or "default"

    # Create inline keyboard with provider buttons
    providers = UserConfigStorage.VALID_PROVIDERS
    keyboard = []
    for i in range(0, len(providers), 2):
        row = [
            InlineKeyboardButton(p.title(), callback_data=f"provider:{p}")
            for p in providers[i:i+2]
        ]
        keyboard.append(row)

    reply_markup = InlineKeyboardMarkup(keyboard)

    message = (
        f"*LLM Configuration*\n\n"
        f"Provider: `{current_provider}`\n"
        f"Deep: `{current_deep}`\n"
        f"Quick: `{current_quick}`\n\n"
        f"Pick a provider — you'll then choose deep and quick models."
    )

    await update.message.reply_text(
        message,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
