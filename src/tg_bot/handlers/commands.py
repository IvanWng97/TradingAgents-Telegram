"""Command handlers (/start, /help, /add, /del, /watch, /list, /config, /history, /status)."""

import asyncio
import logging
import time

import markdown
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from tg_bot.analysis import check_llm_configured, pool_stats
from tg_bot.digest import build_digest_response, humanize_delta, next_fire, tz_short
from tg_bot.formatters import escape_md_v2_url, format_analysis_result_markdown
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
    """Onboarding nudge — leads first-time users through the minimum
    setup before /watch can actually run anything. Full command reference
    lives in /help so this stays short."""
    await update.message.reply_text(
        "👋 Welcome to TradingAgents Bot!\n\n"
        "First-time setup:\n"
        "1. /config — pick your LLM provider + deep/quick models\n"
        "2. /add NVDA AAPL — add tickers to your watchlist\n"
        "3. /watch — tap Done to run your first analysis\n\n"
        "Optional:\n"
        "• /digest — schedule a daily auto-run (pick which tickers to include)\n"
        "• /history — browse past analyses\n\n"
        "Full command list: /help\n\n"
        "⭐ Enjoying the bot? Star it on GitHub:\n"
        "https://github.com/IvanWng97/TradingAgents-Telegram",
        disable_web_page_preview=True,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Canonical command reference. Keep in sync with `BOT_COMMANDS` in
    `app.py` (which feeds Telegram's /-autocomplete + Menu button)."""
    await update.message.reply_text(
        "Available commands:\n\n"
        "/add <ticker> [...] - Add tickers (e.g. /add NVDA AAPL). "
        "With no args, the bot prompts you.\n"
        "/del [<ticker> ...] - Remove tickers. With no args, opens a picker.\n"
        "/watch or /list - Paginated watchlist; tap to select, "
        "Done to run (parallel for multiple).\n"
        "/config - Pick LLM provider + deep/quick think models.\n"
        "/digest - Schedule a daily auto-run; pick time zone, hour, "
        "and a ticker filter (multi-select).\n"
        "/history [<ticker>] [YYYY-MM-DD] - Browse past analyses. "
        "No args → ticker picker.\n"
        "/refresh <ticker> - Force a fresh re-analysis on a watchlist "
        "ticker, bypassing today's cached result.\n"
        "/status - Bot uptime, graph pool stats, your current LLM config, "
        "next digest fire time.\n"
        "/start - Onboarding message.\n\n"
        "⭐ Like the bot? Star it: "
        "https://github.com/IvanWng97/TradingAgents-Telegram",
        disable_web_page_preview=True,
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


async def add_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/add NVDA AAPL` adds inline; bare `/add` opens a reply prompt."""
    user_id = update.effective_user.id
    if context.args:
        await update.message.reply_text(await _apply_add(user_id, context.args))
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

    tokens = (msg.text or "").split()
    summary = await _apply_add(update.effective_user.id, tokens)
    await msg.reply_text(summary)


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


WATCHLIST_PAGE_SIZE = 9


def build_watchlist_response(
    user_id: int,
    selected: set[str] | None = None,
    page: int = 0,
    mode: str = "watch",
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Render the watchlist as MarkdownV2 + a paginated select-mode keyboard.

    Every visible ticker is a toggle button (callback `multi:<ticker>`);
    selected ones get a ✅ prefix. Selection persists across pages — the
    Done counter shows the total selected, not just on this page.

    `mode` toggles between the standard `/watch` styling and `/refresh` —
    keyboard structure is identical in both, only the header text and
    the Done-button label change. Behavior on tap is differentiated in
    `_handle_done` via `chat_data["watch_mode"]`.

    Layout (multi-page):
        [T1] [T2] [T3]
        [T4] [T5] [T6]
        [T7] [T8] [T9]
        [← Prev]  [📄 1/2]  [Next →]
        [✓ Select all]  [✗ Clear]
        [✅ Done (3)]   [❌ Cancel]

    Returns (text, keyboard) — keyboard is None when the watchlist is empty.
    """
    watchlist = watchlist_storage.get_watchlist(user_id)
    if not watchlist:
        return ("Your watchlist is empty.\nUse /add <ticker> to add stocks.", None)

    selected = selected or set()
    is_refresh = mode == "refresh"

    total_pages = max(
        1, (len(watchlist) + WATCHLIST_PAGE_SIZE - 1) // WATCHLIST_PAGE_SIZE
    )
    page = max(0, min(page, total_pages - 1))  # clamp into bounds
    start = page * WATCHLIST_PAGE_SIZE
    visible = watchlist[start : start + WATCHLIST_PAGE_SIZE]

    keyboard = [
        [
            InlineKeyboardButton(
                f"✅ {t}" if t in selected else t,
                callback_data=f"multi:{t}",
            )
            for t in visible[i : i + 3]
        ]
        for i in range(0, len(visible), 3)
    ]

    # Pagination row — only when there's more than one page.
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("← Prev", callback_data="wpage:prev"))
        nav_row.append(
            InlineKeyboardButton(
                f"📄 {page + 1}/{total_pages}", callback_data="wpage:noop"
            )
        )
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next →", callback_data="wpage:next"))
        keyboard.append(nav_row)

    keyboard.append(
        [
            InlineKeyboardButton("✓ Select all", callback_data="wsel:all"),
            InlineKeyboardButton("✗ Clear", callback_data="wsel:clear"),
        ]
    )
    done_label = (
        f"🔄 Refresh ({len(selected)})" if is_refresh else f"✅ Done ({len(selected)})"
    )
    keyboard.append(
        [
            InlineKeyboardButton(done_label, callback_data="runall:go"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel:watch"),
        ]
    )
    # Tickers are already visible as buttons — no point listing them in the
    # message body too. Just a short header. Refresh-mode header tells the
    # user the cache will be dropped so they don't fire it expecting a
    # cheap re-render.
    if is_refresh:
        message = (
            f"*🔄 Force Refresh \\({len(watchlist)} stocks\\)* — "
            "tap to select, then 🔄 Refresh\\.\n"
            "_Drops today's cached result — pays for the LLM run again\\._"
        )
    else:
        message = (
            f"*Your Watchlist \\({len(watchlist)} stocks\\)* — "
            "tap to select, then ✅ Done\\."
        )
    return (message, InlineKeyboardMarkup(keyboard))


async def list_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    # Fresh /watch always starts on page 0 with no selection — clear any
    # leftover state from an abandoned previous render in this chat. The
    # watch_mode flag distinguishes /watch from /refresh's picker so
    # paging callbacks and the Done handler behave correctly per mode.
    context.chat_data["watch_selection"] = set()
    context.chat_data["watch_page"] = 0
    context.chat_data["watch_mode"] = "watch"
    text, kb = build_watchlist_response(user_id, selected=set(), page=0, mode="watch")
    if kb is None:
        await update.message.reply_text(text)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode="MarkdownV2")


async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force a fresh analysis on watchlist ticker(s), bypassing today's
    same-day result cache. Useful when intraday data has shifted enough
    that the user wants a re-analysis instead of the cached morning take.

    Two forms (mirrors `/del NVDA` vs `/del`):
      - `/refresh NVDA` → direct fast-path, single ticker
      - `/refresh` (no args) → paginated multi-select picker like `/watch`,
        but tapping Done invalidates today's cache for each selected
        ticker before launching the analyses

    Late-imports `_run_analysis_for_ticker` and the cache helpers to
    avoid a module-import cycle (callbacks.py already imports from
    commands.py at top level).
    """
    from datetime import date

    from tg_bot import cache as result_cache
    from tg_bot.analysis import build_user_config
    from tg_bot.handlers.callbacks import (
        _llm_setup_error_message,
        _run_analysis_for_ticker,
    )

    user_id = update.effective_user.id
    args = context.args or []

    # No-args → render the same picker as /watch, but flagged so the
    # Done handler invalidates the cache for each selected ticker
    # before running. Sharing the keyboard means pagination, bulk
    # select-all/clear, and selection state all work identically.
    if not args:
        context.chat_data["watch_selection"] = set()
        context.chat_data["watch_page"] = 0
        context.chat_data["watch_mode"] = "refresh"
        text, kb = build_watchlist_response(
            user_id, selected=set(), page=0, mode="refresh"
        )
        if kb is None:
            await update.message.reply_text(text)
        else:
            await update.message.reply_text(
                text, reply_markup=kb, parse_mode="MarkdownV2"
            )
        return

    # With args → direct fast-path. One-step UX for power users who
    # already know the ticker; skips the picker entirely. Multi-ticker
    # refresh has a dedicated picker UX (the no-args form), so reject
    # extra args explicitly rather than silently dropping them — `/refresh
    # NVDA AAPL` would otherwise look like it queued both but only run NVDA.
    if len(args) > 1:
        await update.message.reply_text(
            "`/refresh` takes one ticker at a time\\. "
            "For multiple, run `/refresh` alone and use the picker\\.",
            parse_mode="MarkdownV2",
        )
        return
    ticker = args[0].strip().upper()
    if ticker not in watchlist_storage.get_watchlist(user_id):
        await update.message.reply_text(
            f"`{escape_markdown(ticker, version=2)}` is not in your watchlist\\. "
            "Use /add first\\.",
            parse_mode="MarkdownV2",
        )
        return

    # LLM precheck — same gate as /watch's Done button. A /refresh on an
    # unconfigured user would otherwise produce the same generic auth
    # error the precheck was added to short-circuit.
    setup_reason = check_llm_configured(user_id, user_config_storage)
    if setup_reason is not None:
        await update.message.reply_text(
            _llm_setup_error_message(setup_reason), parse_mode="MarkdownV2"
        )
        return

    # Drop today's cached entry for this (config, ticker, rounds, effort).
    # Next analysis will miss the cache and pay for the LLM run, then
    # repopulate.
    config = build_user_config(user_id, user_config_storage)
    today_iso = date.today().isoformat()
    result_cache.invalidate(
        config["llm_provider"],
        config["deep_think_llm"],
        config["quick_think_llm"],
        ticker,
        today_iso,
        **result_cache.cache_key_extras(config),
    )
    chat_id = update.effective_chat.id
    await _run_analysis_for_ticker(context, chat_id, user_id, ticker)


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
    html = markdown.markdown(md_body, extensions=["tables"])
    telegraph_url = await publish_to_telegraph(f"{ticker} {date_str}", html)

    msg = f"📜 *{safe_ticker}* — {safe_date}\n\n"
    if telegraph_url:
        msg += f"📄 [View Full Report]({escape_md_v2_url(telegraph_url)})"
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
    # `rounds` and `effort` are graph/vocabulary knobs that survive provider
    # switches but still need to roll back if the user cancels mid-flow.
    context.user_data["llm_snapshot"] = {
        "provider": user_config_storage.get_llm_provider(user_id),
        "deep": user_config_storage.get_llm_model(user_id, "deep"),
        "quick": user_config_storage.get_llm_model(user_id, "quick"),
        "rounds": user_config_storage.get_max_debate_rounds(user_id),
        "effort": user_config_storage.get_effort_level(user_id),
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


def _format_uptime(seconds: int) -> str:
    """Render '2d 3h 14m' style — coarsest non-zero unit downward."""
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


async def digest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open the daily-digest picker.

    First-time users land on the tz picker (must pick a zone before an hour
    makes sense). Returning users see the hour grid with their current tz +
    hour reflected in the status line and ✅ prefix.
    """
    user_id = update.effective_user.id
    digest = user_config_storage.get_digest(user_id)
    # Pass the watchlist through so the first render carries the
    # 📋 Tickers (N/M) button + count suffix; otherwise the user has to
    # tap any other action to discover the filter exists.
    watchlist = watchlist_storage.get_watchlist(user_id)
    text, kb = build_digest_response(digest, watchlist=watchlist)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="MarkdownV2")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Operational snapshot: uptime, # analyses since boot, graph pool size,
    requesting user's LLM config. Useful for spotting a silently-broken bot
    (expired LLM key, blown pool cap) without running a full analysis."""
    user_id = update.effective_user.id

    start_ts = context.bot_data.get("start_time")
    uptime_str = _format_uptime(int(time.time() - start_ts)) if start_ts else "unknown"
    analyses_run = context.bot_data.get("analysis_count", 0)
    pool_keys, pool_instances = pool_stats()

    # Surface a precheck warning so users can spot a missing /config or a
    # provider-key mismatch without having to fail an actual analysis first.
    setup_reason = check_llm_configured(user_id, user_config_storage)
    provider = user_config_storage.get_llm_provider(user_id) or "(not set)"
    deep = user_config_storage.get_llm_model(user_id, "deep") or "default"
    quick = user_config_storage.get_llm_model(user_id, "quick") or "default"

    # Digest line: only shown when fully configured + enabled. Surfaces the
    # next-firing instant + human-readable delta so users can sanity-check
    # their UTC↔local arithmetic without leaving /status.
    digest_line = ""
    digest = user_config_storage.get_digest(user_id)
    if (
        digest
        and digest.get("enabled")
        and digest.get("hour_local") is not None
        and digest.get("tz")
    ):
        try:
            fire = next_fire(int(digest["hour_local"]), digest["tz"])
            time_label = f"{int(digest['hour_local']):02d}:00 {tz_short(digest['tz'])}"
            digest_line = (
                f"• Next digest: `{escape_markdown(time_label, version=2)}` "
                f"\\({escape_markdown(humanize_delta(fire), version=2)}\\)\n"
            )
        except Exception:
            pass

    # Numbers + simple ASCII labels are MarkdownV2-safe; user-facing values
    # go inside `…` code spans (no escape needed).
    message = (
        "*Bot status*\n"
        f"• Uptime: `{escape_markdown(uptime_str, version=2)}`\n"
        f"• Analyses since boot: `{analyses_run}`\n"
        f"• Graph pool: `{pool_keys}` keys, `{pool_instances}` instances\n"
        f"{digest_line}\n"
        "*Your LLM config*\n"
        f"• Provider: `{provider}`\n"
        f"• Deep: `{deep}`\n"
        f"• Quick: `{quick}`"
    )
    if setup_reason is not None:
        message += f"\n\n⚠️ `{escape_markdown(setup_reason, version=2)}`"
    await update.message.reply_text(message, parse_mode="MarkdownV2")
