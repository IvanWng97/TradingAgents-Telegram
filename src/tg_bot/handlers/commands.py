"""Command handlers (/start, /help, /add, /del, /watch, /list, /config, /history, /status)."""

import asyncio
import logging
import time

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from tg_bot.pipeline.analysis import (
    check_llm_configured,
    llm_setup_error_message,
    pool_stats,
)
from tg_bot.handlers.analysis_runner import _run_analysis_for_ticker
from tg_bot.handlers.pickers import (
    build_del_keyboard,
    build_history_dates_response,
    build_history_response,
    build_history_tickers_response,
    build_watchlist_response,
)
from tg_bot.digest import build_digest_response, humanize_delta, next_fire, tz_short
from tg_bot.history import normalize_ticker
from tg_bot.storage import (
    UserConfigStorage,
    user_config_storage,
    watchlist_storage,
)
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
        "/list - Show your watchlist as text + digest enrolment "
        "(read-only).\n"
        "/watch - Paginated watchlist picker; tap to select, "
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
    for raw, (canonical, hint) in zip(cleaned, results, strict=True):
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


# Pre-block grid layout for /list. The whole grid renders inside a
# MarkdownV2 triple-backtick block so spaces inside it stay monospace —
# inline code-span padding wobbles because Telegram renders BETWEEN-span
# whitespace in the proportional message font.
_GRID_GUTTER = 2  # spaces between cells in the grid
_GRID_TARGET_WIDTH = 36  # mobile-safe target line width (≈ iPhone SE)
_GRID_MAX_COLS = 4  # cap regardless of viewport


def _format_ticker_grid(watchlist: list[str]) -> str:
    """Render a ticker grid inside a MarkdownV2 pre block.

    Cell width = max(len(t) for t in watchlist) + _GRID_GUTTER. Column
    count is clamped so a row fits within _GRID_TARGET_WIDTH characters
    on mobile viewports; falls back to 1 column on pathologically long
    tickers (≥ _GRID_TARGET_WIDTH chars).

    All cells in all rows pad to the same width, so a long ticker like
    `RELIANCE.NS` does not push subsequent cells out of column
    alignment — the entire grid widens uniformly.

    Tickers can only contain `[A-Z0-9.\\-]` (enforced by validation.py:
    TICKER_RE), so no escaping is needed inside the pre block — neither
    `\\`` nor `\\` characters can appear.
    """
    cell_width = max(len(t) for t in watchlist) + _GRID_GUTTER
    ncols = max(1, min(_GRID_MAX_COLS, _GRID_TARGET_WIDTH // cell_width))
    rows: list[str] = []
    for i in range(0, len(watchlist), ncols):
        row = "".join(f"{t:<{cell_width}}" for t in watchlist[i : i + ncols])
        rows.append(row.rstrip())
    return "```\n" + "\n".join(rows) + "\n```"


def _digest_enrolled_set(
    user_id: str, digest: dict | None, watchlist: list[str]
) -> set[str] | None:
    """`/list`-specific wrapper around `UserConfigStorage.get_enrolled_tickers`.

    The storage method returns `list[str]` (order-preserved for the
    fan-out's iteration semantics). `/list` needs a `set` for fast `in`
    checks during grid rendering AND a distinct sentinel for "digest
    off entirely" vs "digest on but resolves to empty enrolled" —
    those two states render different footer copy. The storage method
    can't distinguish them because it deliberately doesn't gate on
    `enabled` (the run-now path needs the filter even when scheduled
    runs are disabled). So this wrapper does the `enabled` check + the
    `None` sentinel here.
    """
    if not digest or not digest.get("enabled"):
        return None
    return set(user_config_storage.get_enrolled_tickers(user_id, watchlist))


def _format_list_view(
    watchlist: list[str],
    digest: dict | None,
    enrolled: set[str] | None,
) -> str:
    """MarkdownV2 view for `/list`.

    Composition:
      - Title line: "📋 *Watchlist* — N tickers"
      - Digest header (when enabled is true): "🔔 *Digest* — HH:00 TZ"
        plus either "· all N fire daily" suffix (legacy all-enrolled),
        or an indented "   → `T1`, `T2`, ..." subset line below.
      - Grid in a MarkdownV2 pre block. Bullet (🔔) markers are NEVER
        in the grid — they would break monospace alignment.
      - Footer: state-dependent reminder when digest is off OR enabled
        but no tickers enrolled.
    """
    parts: list[str] = []
    parts.append(f"📋 *Watchlist* — {len(watchlist)} tickers")

    # Digest header section
    if digest is not None and enrolled is not None:
        hour = digest.get("hour_local", 0)
        tz_label = tz_short(digest.get("tz"))
        # tz_label may contain a slash from raw IANA fallback ("America/Los_Angeles")
        # which is MarkdownV2-special and must be escaped outside code spans.
        safe_tz = escape_markdown(tz_label, version=2)

        all_watchlist = enrolled == set(watchlist)
        if all_watchlist:
            parts.append(
                f"🔔 *Digest* — `{hour:02d}:00` {safe_tz} · "
                f"all {len(watchlist)} fire daily"
            )
        else:
            parts.append(f"🔔 *Digest* — `{hour:02d}:00` {safe_tz}")
            # Only emit the "→ T1, T2" line when there's at least one
            # enrolled ticker. Empty enrolled set gets a footer reminder
            # instead (see below) — the picker UX is already a tap away.
            if enrolled:
                # Watchlist order preserved (sorted on-disk per
                # set_digest_tickers). Each ticker in monospace code span.
                cells = ", ".join(f"`{t}`" for t in watchlist if t in enrolled)
                parts.append(f"   → {cells}")

    parts.append("")  # blank line between header and grid
    parts.append(_format_ticker_grid(watchlist))

    # Footer section
    if digest is None:
        parts.append("")
        parts.append("_Daily digest off — use /digest to enable\\._")
    elif enrolled is not None and not enrolled:
        # Digest enabled but filter set excludes the entire watchlist.
        # The fan-out treats this as "nothing to do" and sends a reminder;
        # surface that state here so the user understands why.
        parts.append("")
        parts.append("_Digest enabled but no tickers enrolled — /digest to fix\\._")

    return "\n".join(parts)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Read-only watchlist view + digest enrolment status. Unlike /watch
    (the picker for actioning tickers), this is pure read — no
    chat_data, no callbacks, no inline keyboard. Surfaces the digest
    filter membership inline (🔔) since that subset is otherwise buried
    two screens deep inside /digest."""
    user_id = update.effective_user.id
    uid_str = str(user_id)
    watchlist = watchlist_storage.get_watchlist(uid_str)

    if not watchlist:
        await update.message.reply_text(
            "📋 Watchlist is empty\\. Use `/add NVDA AAPL` to start\\.",
            parse_mode="MarkdownV2",
        )
        return

    digest = user_config_storage.get_digest(uid_str)
    enrolled = _digest_enrolled_set(uid_str, digest, watchlist)
    text = _format_list_view(watchlist, digest, enrolled)
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force a fresh analysis on watchlist ticker(s), bypassing today's
    same-day result cache. Useful when intraday data has shifted enough
    that the user wants a re-analysis instead of the cached morning take.

    Two forms (mirrors `/del NVDA` vs `/del`):
      - `/refresh NVDA` → direct fast-path, single ticker
      - `/refresh` (no args) → paginated multi-select picker like `/watch`,
        but tapping Done invalidates today's cache for each selected
        ticker before launching the analyses
    """
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
            llm_setup_error_message(setup_reason), parse_mode="MarkdownV2"
        )
        return

    # `force_refresh=True` — the handler reads the prior Telegraph URL
    # from the cached entry (so edit_page reuses the same URL), then
    # bypasses the cache-hit short-circuit so the LLM re-runs and the
    # Telegraph page is updated in place. `result_cache.store(...)` at
    # the end of the fresh run overwrites the prior entry.
    chat_id = update.effective_chat.id
    await _run_analysis_for_ticker(
        context, chat_id, user_id, ticker, force_refresh=True
    )


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
        date_str = context.args[1].strip()
        caption, state, telegraph_url = await build_history_response(ticker, date_str)
        # Surface the action buttons only when a record exists; otherwise
        # they'd dead-end on the same "unavailable" path.
        kb = None
        if state is not None:
            buttons = []
            if telegraph_url:
                buttons.append(
                    InlineKeyboardButton("📰 Instant View", url=telegraph_url)
                )
            buttons.append(
                InlineKeyboardButton(
                    "📥 Download .md",
                    callback_data=f"getmd:{ticker}:{date_str}",
                )
            )
            kb = InlineKeyboardMarkup([buttons])
        await update.message.reply_text(caption, parse_mode="HTML", reply_markup=kb)
    else:
        text, kb = build_history_dates_response(ticker)
        if kb is None:
            await update.message.reply_text(text, parse_mode="MarkdownV2")
        else:
            await update.message.reply_text(
                text, parse_mode="MarkdownV2", reply_markup=kb
            )


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
    # MarkdownV2 code spans suppress most escaping but NOT backticks
    # inside the value — a backtick would close the span and break parsing.
    # Defensive escape so an exotic provider/model ID can't break /config.
    message = (
        "*LLM Configuration*\n\n"
        f"Provider: `{escape_markdown(current_provider, version=2)}`\n"
        f"Deep: `{escape_markdown(current_deep, version=2)}`\n"
        f"Quick: `{escape_markdown(current_quick, version=2)}`\n\n"
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

    # MarkdownV2 code spans (backtick-wrapped) suppress most escaping, but
    # a backtick *in the value itself* would break out of the span and
    # cause `Bad Request: can't parse entities`. Provider/model strings
    # come from user config (set via picker) so today never contain
    # backticks — but a future provider name or custom model ID could.
    # Defensive escape for any string that could carry user-influenced
    # content. Numeric values stay un-escaped.
    message = (
        "*Bot status*\n"
        f"• Uptime: `{escape_markdown(uptime_str, version=2)}`\n"
        f"• Analyses since boot: `{analyses_run}`\n"
        f"• Graph pool: `{pool_keys}` keys, `{pool_instances}` instances\n"
        f"{digest_line}\n"
        "*Your LLM config*\n"
        f"• Provider: `{escape_markdown(provider, version=2)}`\n"
        f"• Deep: `{escape_markdown(deep, version=2)}`\n"
        f"• Quick: `{escape_markdown(quick, version=2)}`"
    )
    if setup_reason is not None:
        message += f"\n\n⚠️ `{escape_markdown(setup_reason, version=2)}`"
    await update.message.reply_text(message, parse_mode="MarkdownV2")
