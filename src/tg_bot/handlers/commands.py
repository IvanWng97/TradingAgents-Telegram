"""Command handlers (/start, /help, /add, /del, /watch, /list, /config, /history)."""

import asyncio
import logging

import markdown
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from tg_bot.formatters import format_analysis_result_markdown
from tg_bot.history import (
    list_available_dates,
    list_available_tickers,
    load_historical_state,
    normalize_ticker,
)
from tg_bot.storage import (
    UserConfigStorage,
    user_config_storage,
    watchlist_storage,
)
from tg_bot.telegraph_client import publish_to_telegraph
from tg_bot.validation import validate_ticker


logger = logging.getLogger(__name__)


# Sentinel string used to recognize replies to our add-prompt. Matched
# verbatim against `update.message.reply_to_message.text` so the reply
# handler doesn't fire on every reply to the bot.
ADD_PROMPT = "📝 Send the ticker symbol(s) to add (e.g. NVDA AAPL TSLA):"


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
        "/history [<ticker>] [YYYY-MM-DD] - Browse past analyses. "
        "With no args, shows tickers with saved history.\n"
        "/start - Welcome message"
    )


async def _apply_add(user_id: int, tokens: list[str]) -> str:
    """Add `tokens` (raw ticker strings) to the user's watchlist; return a
    summary. Each token is validated against yfinance in parallel before any
    storage write — invalid tickers report a hint instead of silently joining
    the watchlist."""
    cleaned = [t for t in (raw.strip() for raw in tokens) if t]
    if not cleaned:
        return "No valid tickers provided."

    results = await asyncio.gather(*(validate_ticker(t) for t in cleaned))

    added: list[str] = []
    duplicate: list[str] = []
    invalid: list[str] = []
    notes: list[str] = []
    for raw, (canonical, hint) in zip(cleaned, results):
        if canonical is None:
            invalid.append(hint or f"`{raw}` is invalid.")
            continue
        if hint:  # auto-correction note (canonical differs from raw)
            notes.append(hint)
        if await watchlist_storage.add_ticker(user_id, canonical):
            added.append(canonical)
        else:
            duplicate.append(canonical)

    parts: list[str] = []
    if added:
        parts.append(f"✅ Added: {', '.join(added)}")
    if duplicate:
        parts.append(f"➖ Already in watchlist: {', '.join(duplicate)}")
    if notes:
        parts.extend(notes)
    if invalid:
        parts.append("❌ " + " ".join(invalid))
    if not parts:
        parts.append("No valid tickers provided.")
    return "\n".join(parts)


async def _send_summary_with_watchlist(message, user_id: int, summary: str) -> None:
    """Reply with the add/del summary, then re-render the watchlist below it."""
    await message.reply_text(summary)
    text, kb = build_watchlist_response(user_id)
    if kb is None:
        await message.reply_text(text)
    else:
        await message.reply_text(text, reply_markup=kb, parse_mode="MarkdownV2")


async def add_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/add NVDA AAPL` adds inline; bare `/add` opens a reply prompt."""
    user_id = update.effective_user.id
    if context.args:
        summary = await _apply_add(user_id, context.args)
        await _send_summary_with_watchlist(update.message, user_id, summary)
        return

    # No args: open a ForceReply prompt. Telegram clients pop the reply box
    # automatically; the user types tickers and add_via_reply catches them.
    await update.message.reply_text(
        ADD_PROMPT,
        reply_markup=ForceReply(selective=True),
    )


async def add_via_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle replies to our ADD_PROMPT message — treat reply text as tickers."""
    msg = update.message
    if msg is None or msg.reply_to_message is None:
        return
    replied = msg.reply_to_message
    # Only fire when the reply is to OUR add prompt (not random replies to the bot).
    if not replied.from_user or not replied.from_user.is_bot:
        return
    if (replied.text or "") != ADD_PROMPT:
        return

    user_id = update.effective_user.id
    tokens = (msg.text or "").split()
    summary = await _apply_add(user_id, tokens)
    await _send_summary_with_watchlist(msg, user_id, summary)


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
    await _send_summary_with_watchlist(update.message, user_id, "\n".join(parts))


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
    """3-per-row grid of ❌-prefixed delete buttons + a Done row to close
    the picker. Deletes happen on each tap, so the trailing button is just
    a dismissal — not a rollback."""
    rows = [
        [
            InlineKeyboardButton(f"❌ {t}", callback_data=f"del:{t}")
            for t in tickers[i : i + 3]
        ]
        for i in range(0, len(tickers), 3)
    ]
    rows.append([InlineKeyboardButton("✅ Done", callback_data="cancel:del")])
    return rows


def build_watchlist_response(
    user_id: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render the watchlist as MarkdownV2 + a tap-to-analyze keyboard.

    Returns (text, keyboard) — keyboard is None when the watchlist is empty
    so the caller can decide whether to attach it.
    """
    watchlist = watchlist_storage.get_watchlist(user_id)
    if not watchlist:
        return ("Your watchlist is empty.\nUse /add <ticker> to add stocks.", None)

    keyboard = [
        [
            InlineKeyboardButton(t, callback_data=f"info:{t}")
            for t in watchlist[i : i + 3]
        ]
        for i in range(0, len(watchlist), 3)
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel:watch")])
    # Tickers go inside `…` code spans, where the only chars needing escape
    # are backticks and backslashes — neither valid in a stock symbol.
    message = f"*Your Watchlist \\({len(watchlist)} stocks\\):*\n\n" + "\n".join(
        f"• `{t}`" for t in watchlist
    )
    return (message, InlineKeyboardMarkup(keyboard))


async def list_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text, kb = build_watchlist_response(user_id)
    if kb is None:
        await update.message.reply_text(text)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="MarkdownV2")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Look up a past TradingAgents analysis from disk.

    Forms:
      /history                  -> ticker picker (all tickers with any history)
      /history NVDA             -> date picker for that ticker
      /history NVDA 2026-04-15  -> publish that day's saved analysis to Telegraph
    """
    if not context.args:
        text, kb = build_history_tickers_response()
        if kb is None:
            await update.message.reply_text(text, parse_mode="MarkdownV2")
        else:
            await update.message.reply_text(
                text, parse_mode="MarkdownV2", reply_markup=kb
            )
        return

    ticker = normalize_ticker(context.args[0])
    if ticker is None:
        await update.message.reply_text("Invalid ticker symbol.")
        return

    if len(context.args) >= 2:
        caption = await build_history_response(ticker, context.args[1].strip())
        await update.message.reply_text(caption, parse_mode="MarkdownV2")
    else:
        text, kb = build_history_dates_response(ticker)
        if kb is None:
            await update.message.reply_text(text, parse_mode="MarkdownV2")
        else:
            await update.message.reply_text(
                text, parse_mode="MarkdownV2", reply_markup=kb
            )


def build_history_tickers_response() -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the ticker-picker keyboard for users with saved analyses.

    Returns (MarkdownV2 caption, keyboard) — keyboard is None when there's
    nothing to pick. Shared by /history (no args) and any caller that wants
    to re-render the picker in place.
    """
    tickers = list_available_tickers()
    if not tickers:
        return (
            "No history yet — run /watch and tap a ticker to start building history\\.",
            None,
        )
    keyboard = [
        [
            InlineKeyboardButton(t, callback_data=f"hist_t:{t}")
            for t in tickers[i : i + 3]
        ]
        for i in range(0, len(tickers), 3)
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel:hist")])
    return "📜 Pick a ticker:", InlineKeyboardMarkup(keyboard)


def build_history_dates_response(
    ticker: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the date-picker keyboard for a single ticker.

    Returns (MarkdownV2 caption, keyboard) — keyboard is None when no logs
    exist. Shared by /history <ticker> and the `hist_t:` callback so both
    surfaces produce identical output.
    """
    safe_ticker = escape_markdown(ticker, version=2)
    dates = list_available_dates(ticker)
    if not dates:
        return f"No history found for {safe_ticker}\\.", None
    keyboard = [
        [
            InlineKeyboardButton(
                d.isoformat(), callback_data=f"hist:{ticker}:{d.isoformat()}"
            )
        ]
        for d in dates
    ]
    keyboard.append(
        [
            InlineKeyboardButton("← Back", callback_data="hist_back:tickers"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel:hist"),
        ]
    )
    return (
        f"📜 History for `{safe_ticker}` — pick a date:",
        InlineKeyboardMarkup(keyboard),
    )


async def build_history_response(ticker: str, date_str: str) -> str:
    """Load + publish a historical analysis. Returns a MarkdownV2 caption.

    Shared by the /history command and the `hist:` inline-button callback so
    both surfaces produce identical output.
    """
    safe_ticker = escape_markdown(ticker, version=2)
    safe_date = escape_markdown(date_str, version=2)

    state = load_historical_state(ticker, date_str)
    if state is None:
        return f"No analysis found for {safe_ticker} on {safe_date}\\."

    md_body = format_analysis_result_markdown(ticker, state, signal="historical")
    html = markdown.markdown(md_body)
    telegraph_url = await publish_to_telegraph(f"{ticker} {date_str}", html)

    msg = f"📜 *{safe_ticker}* — {safe_date}\n\n"
    if telegraph_url:
        # MarkdownV2 link URLs only need to escape ')' and '\'.
        safe_url = telegraph_url.replace("\\", "\\\\").replace(")", "\\)")
        msg += f"📄 [View Full Report]({safe_url})"
    else:
        msg += "⚠️ Full report unavailable " + escape_markdown(
            "(Telegraph publish failed).", version=2
        )
    return msg


async def config_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    current_provider = (
        user_config_storage.get_llm_provider(user_id) or "default (openai)"
    )
    current_deep = user_config_storage.get_llm_model(user_id, "deep") or "default"
    current_quick = user_config_storage.get_llm_model(user_id, "quick") or "default"

    # Snapshot the current LLM state so a Cancel during the flow can restore it.
    context.user_data["llm_snapshot"] = {
        "provider": user_config_storage.get_llm_provider(user_id),
        "deep": user_config_storage.get_llm_model(user_id, "deep"),
        "quick": user_config_storage.get_llm_model(user_id, "quick"),
    }

    providers = UserConfigStorage.VALID_PROVIDERS
    keyboard = [
        [
            InlineKeyboardButton(p.title(), callback_data=f"provider:{p}")
            for p in providers[i : i + 2]
        ]
        for i in range(0, len(providers), 2)
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel:config")])
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
