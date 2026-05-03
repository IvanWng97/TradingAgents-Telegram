"""Command handlers (/start, /help, /add, /del, /watch, /list, /config)."""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from tg_bot.storage import (
    UserConfigStorage,
    user_config_storage,
    watchlist_storage,
)


logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Welcome to TradingAgents Bot!\n\n"
        "Available commands:\n"
        "/add <ticker> - Add a stock to watchlist\n"
        "/del <ticker> - Remove a stock from watchlist\n"
        "/watch or /list - Show your watchlist\n"
        "/config - Configure LLM provider\n"
        "/help - Show this help message"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Available commands:\n\n"
        "/add <ticker> [<ticker> ...] - Add stocks (e.g. /add NVDA AAPL TSLA)\n"
        "/del [<ticker> ...] - Remove stocks. With no args opens a picker.\n"
        "/watch or /list - Show your watchlist with clickable buttons\n"
        "/config - Configure LLM provider and deep/quick models\n"
        "/start - Welcome message"
    )


async def add_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Accept one or more tickers in a single command."""
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Please provide one or more ticker symbols.\nExample: /add NVDA AAPL TSLA"
        )
        return

    added: list[str] = []
    duplicate: list[str] = []
    for raw in context.args:
        ticker = raw.strip().upper()
        if not ticker:
            continue
        if await watchlist_storage.add_ticker(user_id, ticker):
            added.append(ticker)
        else:
            duplicate.append(ticker)

    parts: list[str] = []
    if added:
        parts.append(f"✅ Added: {', '.join(added)}")
    if duplicate:
        parts.append(f"➖ Already in watchlist: {', '.join(duplicate)}")
    if not parts:
        parts.append("No valid tickers provided.")
    await update.message.reply_text("\n".join(parts))


async def del_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """With args: bulk-remove. Without args: open an inline-button picker."""
    user_id = update.effective_user.id

    if not context.args:
        await _send_del_picker(update, user_id)
        return

    removed: list[str] = []
    missing: list[str] = []
    for raw in context.args:
        ticker = raw.strip().upper()
        if not ticker:
            continue
        if await watchlist_storage.remove_ticker(user_id, ticker):
            removed.append(ticker)
        else:
            missing.append(ticker)

    parts: list[str] = []
    if removed:
        parts.append(f"✅ Removed: {', '.join(removed)}")
    if missing:
        parts.append(f"❓ Not in watchlist: {', '.join(missing)}")
    if not parts:
        parts.append("No valid tickers provided.")
    await update.message.reply_text("\n".join(parts))


async def _send_del_picker(update: Update, user_id: int) -> None:
    """Render the watchlist as a keyboard of ❌-prefixed remove buttons."""
    watchlist = watchlist_storage.get_watchlist(user_id)
    if not watchlist:
        await update.message.reply_text("Your watchlist is empty.")
        return

    keyboard = build_del_keyboard(watchlist)
    await update.message.reply_text(
        "Tap a ticker to remove it from your watchlist:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def build_del_keyboard(tickers: list[str]) -> list[list[InlineKeyboardButton]]:
    """3-per-row grid of ❌-prefixed delete buttons."""
    return [
        [
            InlineKeyboardButton(f"❌ {t}", callback_data=f"del:{t}")
            for t in tickers[i : i + 3]
        ]
        for i in range(0, len(tickers), 3)
    ]


async def list_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    watchlist = watchlist_storage.get_watchlist(user_id)

    if not watchlist:
        await update.message.reply_text(
            "Your watchlist is empty.\nUse /add <ticker> to add stocks."
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(t, callback_data=f"info:{t}")
            for t in watchlist[i : i + 3]
        ]
        for i in range(0, len(watchlist), 3)
    ]
    # Tickers go inside `…` code spans, where the only chars needing escape
    # are backticks and backslashes — neither valid in a stock symbol.
    message = f"*Your Watchlist \\({len(watchlist)} stocks\\):*\n\n" + "\n".join(
        f"• `{t}`" for t in watchlist
    )
    await update.message.reply_text(
        message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2"
    )


async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    current_provider = (
        user_config_storage.get_llm_provider(user_id) or "default (openai)"
    )
    current_deep = user_config_storage.get_llm_model(user_id, "deep") or "default"
    current_quick = user_config_storage.get_llm_model(user_id, "quick") or "default"

    providers = UserConfigStorage.VALID_PROVIDERS
    keyboard = [
        [
            InlineKeyboardButton(p.title(), callback_data=f"provider:{p}")
            for p in providers[i : i + 2]
        ]
        for i in range(0, len(providers), 2)
    ]
    # Provider/model strings inside `…` code spans need no escaping.
    message = (
        "*LLM Configuration*\n\n"
        f"Provider: `{current_provider}`\n"
        f"Deep: `{current_deep}`\n"
        f"Quick: `{current_quick}`\n\n"
        "Pick a provider — you'll then choose deep and quick models\\."
    )
    await update.message.reply_text(
        message, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2"
    )
