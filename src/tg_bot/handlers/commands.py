"""Command handlers (/start, /help, /add, /del, /watch, /list, /history, /status)."""

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

from tg_bot.config import Config
from tg_bot.pipeline.analysis import (
    EFFORT_KEY_BY_PROVIDER,
    build_config,
    check_llm_configured,
    llm_setup_error_message,
    pool_stats,
)
from tg_bot.pipeline.pricing import estimate_token_cost_usd
from tg_bot.pipeline.progress import get_token_totals
from tg_bot.handlers.analysis_runner import _run_analysis_for_ticker
from tg_bot.rendering.email_client import is_email_configured, send_digest_email
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
    user_config_storage,
    watchlist_storage,
)
from tg_bot.validation import validate_ticker


logger = logging.getLogger(__name__)


# Sentinel string used to recognize replies to our add-prompt. Matched
# verbatim against `update.message.reply_to_message.text` so the reply
# handler doesn't fire on every reply to the bot.
ADD_PROMPT = "📝 Send the ticker symbol(s) to add (e.g. NVDA AAPL TSLA):"
EMAIL_PROMPT = "📧 Send your email address to mirror the daily digest:"

# Email-mirror abuse controls (M3). The mirror sends through the operator's
# verified Resend domain to a user-supplied recipient, so two guards apply:
#   1. Open-mode refusal — when ALLOWED_USER_IDS is empty the bot is open to
#      anyone, so a stranger could point `/email` at an arbitrary victim and
#      relay/spam them on the operator's domain. The email feature is disabled
#      entirely in that mode (enforced here in `email_cmd` / `email_via_reply`,
#      with a backstop in `send_digest_email`). `/email off` stays allowed so a
#      stale address can always be cleared.
#   2. Per-user cooldown on immediate test sends (`/email test` and the
#      test-send leg of `/email diagnose`) so even a vetted, allow-listed user
#      can't hold the button down and hammer Resend. The dict is bounded by the
#      allowlist size (open mode is refused before it is ever reached), so no
#      eviction is needed.
_EMAIL_OPEN_MODE_NOTICE = (
    "📧 The email mirror is disabled while the bot is open to everyone "
    "\\(`ALLOWED_USER_IDS` is empty\\)\\.\n\n"
    "It relays through the operator's email domain, so it's only enabled once "
    "the bot is locked down to an allowlist\\. Ask the operator to set "
    "`ALLOWED_USER_IDS` and restart\\."
)
_EMAIL_TEST_COOLDOWN_S = 60.0
_email_test_last_sent: dict[int, float] = {}


def _email_test_cooldown_remaining(user_id: int) -> float | None:
    """Per-user throttle for immediate test sends. Returns the remaining
    cooldown in seconds if `user_id` sent a test within the window; otherwise
    records this attempt and returns None. Monotonic clock so a wall-clock
    adjustment can't widen or collapse the window."""
    now = time.monotonic()
    last = _email_test_last_sent.get(user_id)
    if last is not None and (remaining := _EMAIL_TEST_COOLDOWN_S - (now - last)) > 0:
        return remaining
    _email_test_last_sent[user_id] = now
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Onboarding nudge — leads first-time users through the minimum
    setup before /watch can actually run anything. Full command reference
    lives in /help so this stays short."""
    await update.message.reply_text(
        "👋 Welcome to TradingAgents Bot!\n\n"
        "LLM provider + models are configured in your `.env` file (single source "
        "of truth, applies to every user). Check /status to see what's active.\n\n"
        "First-time setup in Telegram:\n"
        "1. /add NVDA AAPL — add tickers to your watchlist\n"
        "2. /watch — tap Done to run your first analysis\n\n"
        "Optional:\n"
        "• /digest — schedule a Mon-Fri auto-run (pick which tickers to include)\n"
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
        "/digest - Schedule a Mon-Fri auto-run; pick time zone, hour, "
        "and a ticker filter (multi-select).\n"
        "/email [<addr>|off|test] - Mirror the daily digest to email "
        "(Resend, opt-in). No args shows current setting.\n"
        "/history [<ticker>] [YYYY-MM-DD] - Browse past analyses. "
        "No args → ticker picker.\n"
        "/refresh <ticker> - Force a fresh re-analysis on a watchlist "
        "ticker, bypassing today's cached result.\n"
        "/status - Bot uptime, graph pool stats, active LLM config (from "
        "`.env`), next digest fire time.\n"
        "/start - Onboarding message.\n\n"
        "LLM provider + models live in `.env` (TRADINGAGENTS_LLM_PROVIDER, "
        "TRADINGAGENTS_DEEP_THINK_LLM, etc.). See docs/CONFIGURATION.md.\n\n"
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


async def email_via_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle replies to our EMAIL_PROMPT message — treat reply text as an
    email address. Mirrors `add_via_reply`'s strict-prompt-match shape: we
    early-return on replies that aren't to our specific prompt, so this
    handler can coexist in a different PTB group from `add_via_reply`
    without either swallowing the other's traffic.
    """
    msg = update.message
    if msg is None or msg.reply_to_message is None:
        return
    replied = msg.reply_to_message
    if not replied.from_user or not replied.from_user.is_bot:
        return
    if (replied.text or "") != EMAIL_PROMPT:
        return

    user_id = update.effective_user.id
    # Open-mode abuse gate (M3) — same policy as `email_cmd`. The bare
    # `/email` that opened this prompt is itself gated, so this only fires if
    # the allowlist was removed after the prompt was shown; refuse the save.
    if not Config.ALLOWED_USER_IDS:
        await msg.reply_text(_EMAIL_OPEN_MODE_NOTICE, parse_mode="MarkdownV2")
        return
    addr = (msg.text or "").strip()
    ok = await user_config_storage.set_digest_email(str(user_id), addr)
    if not ok:
        await msg.reply_text(
            f"❌ `{escape_markdown(addr, version=2)}` doesn't look like a valid "
            "email\\. Expected format: `name@domain\\.tld`\\.",
            parse_mode="MarkdownV2",
        )
        return

    confirmation = (
        f"✅ Email mirror set to `{escape_markdown(addr, version=2)}`\\. "
        "Daily digest will mirror here\\."
    )
    if not is_email_configured():
        confirmation += (
            "\n\n⚠️ `RESEND_API_KEY` / `RESEND_FROM` not set in `\\.env` — "
            "email won't actually send until the operator wires those\\."
        )
    else:
        confirmation += " Run `/email test` to send a one-off test\\."
    await msg.reply_text(confirmation, parse_mode="MarkdownV2")


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
        # tz_label may be the raw IANA name on uncurated zones ("America/Los_Angeles");
        # periods, underscores and other MarkdownV2-special characters must be escaped
        # outside code spans.
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
    setup_reason = check_llm_configured()
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


def _format_token_count(n: int) -> str:
    """Render integer token counts compactly: `1234567` → `1.2M`, `5678` → `5.7K`.
    Used in `/status` so a multi-million-token cumulative total doesn't
    blow out the message width."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


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


async def _email_diagnose(update: Update, current: str | None) -> None:
    """End-to-end operator diagnosis for the email-mirror pipeline.

    Reports four checks in one MarkdownV2 message:
      1. `RESEND_API_KEY` present
      2. `RESEND_FROM` present
      3. Resend domain status — pings `resend.Domains.list()` and reports
         the verification state of the domain in `RESEND_FROM`
      4. Test send — invokes `send_digest_email` against the user's
         configured address and shows the result + Resend message id

    Designed for the "I just set up Resend, does it actually work?"
    moment when domain verification + API key + env wiring all need
    to be confirmed together. Faster than chasing the four surfaces
    separately (dashboard / .env / `/email test` / inbox)."""
    import os
    from datetime import date as _date
    from html import escape as _html_escape

    from tg_bot.rendering.email_client import check_resend_domain

    # Title + intentional blank line, then bullets. Using explicit
    # blank-line separator (vs the prior `"🔍 …\n"` form which would
    # compound with `"\n".join(...)` into a double blank line).
    lines = ["🔍 *Email diagnose*", ""]

    api_key_set = bool(os.environ.get("RESEND_API_KEY"))
    from_addr = os.environ.get("RESEND_FROM", "")
    lines.append(f"• `RESEND_API_KEY`: {'✅ set' if api_key_set else '❌ not set'}")
    lines.append(
        "• `RESEND_FROM`: "
        + (
            f"✅ `{escape_markdown(from_addr, version=2)}`"
            if from_addr
            else "❌ not set"
        )
    )

    # Domain status — `check_resend_domain` lives in email_client.py so
    # all Resend SDK access stays in one module (parity with how
    # telegraph_client.py owns every Telegraph SDK call).
    if api_key_set and from_addr and "@" in from_addr:
        expected_domain = from_addr.rsplit("@", 1)[1]
        domain_h = escape_markdown(expected_domain, version=2)
        status, error = await check_resend_domain(expected_domain)
        if error == "not_in_account":
            lines.append(
                f"• Domain status: ❌ `{domain_h}` not in your Resend account "
                "— add it in the dashboard\\."
            )
        elif error is not None:
            lines.append(
                f"• Domain status: ❌ Resend API error "
                f"\\(`{escape_markdown(error, version=2)}`\\) "
                "— check `RESEND_API_KEY` value"
            )
        elif status == "verified":
            lines.append(f"• Domain status: ✅ `{domain_h}` verified")
        else:
            status_h = escape_markdown(status or "unknown", version=2)
            lines.append(
                f"• Domain status: ⏳ `{domain_h}` is `{status_h}` "
                "\\(DNS not propagated yet, or pending in Resend\\)"
            )
    else:
        lines.append(
            "• Domain status: ⏭ skipped \\(needs both env vars \\+ valid FROM\\)"
        )

    # Test send — only if all prereqs above are met AND user has an
    # address. Reuses `send_digest_email` with the same synthetic payload
    # `/email test` uses so the two surfaces stay in lockstep.
    if not current:
        lines.append(
            "• Test send: ⏭ skipped \\(no recipient — run `/email <addr>` first\\)"
        )
    elif not is_email_configured():
        lines.append("• Test send: ⏭ skipped \\(env not configured\\)")
    elif (
        cooldown := _email_test_cooldown_remaining(update.effective_user.id)
    ) is not None:
        # Shares the `/email test` cooldown bucket — both burn the operator's
        # Resend quota, so a diagnose right after a test is throttled too.
        lines.append(
            f"• Test send: ⏳ rate\\-limited "
            f"\\(wait {int(cooldown) + 1}s — a test was sent recently\\)"
        )
    else:
        today = _date.today().isoformat()
        result = await send_digest_email(
            to_addr=current,
            watchlist=["TEST"],
            status={
                "TEST": {
                    "ticker": "TEST",
                    "signal": "HOLD",
                    "telegraph_url": None,
                }
            },
            safe_date=_html_escape(today),
            date_iso=today,
            skipped_closed=["FAKE.HK"],
        )
        if result.ok:
            lines.append(
                f"• Test send: ✅ sent to `{escape_markdown(current, version=2)}` "
                f"\\(id: `{escape_markdown(result.message_id or '?', version=2)}`\\)"
            )
        else:
            lines.append(
                f"• Test send: ❌ failed "
                f"\\(`{escape_markdown(result.error or 'unknown', version=2)}`\\) "
                "— check bot logs"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


async def email_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Configure the daily-digest email mirror.

    Sub-commands:
      `/email`                 — open a ForceReply prompt (UX parity with /add)
      `/email foo@bar.com`     — set the address (validates locally)
      `/email off`             — clear the address (digest stays Telegram-only)
      `/email test`            — send a one-off test email
      `/email diagnose`        — full end-to-end pipeline check (env vars,
                                 Resend domain status, test send)

    All responses are MarkdownV2 (Invariant #4 — pickers and status lines
    use MarkdownV2; only the analysis-output captions use HTML)."""
    user_id = update.effective_user.id
    args = context.args or []

    digest = user_config_storage.get_digest(user_id) or {}
    current = digest.get("email")

    sub = args[0].lower().strip() if args else None

    # Open-mode abuse gate (M3): refuse the whole feature when the bot is open
    # to everyone, EXCEPT `/email off` (which only clears a possibly-stale
    # address — always safe). See `_EMAIL_OPEN_MODE_NOTICE` for the rationale.
    if sub != "off" and not Config.ALLOWED_USER_IDS:
        await update.message.reply_text(
            _EMAIL_OPEN_MODE_NOTICE, parse_mode="MarkdownV2"
        )
        return

    if not args:
        # Bare `/email` — open a ForceReply prompt, same UX as bare `/add`.
        # The current setting (if any) lives in `/status` now; surfacing it
        # here AND prompting for a new one would be two messages worth of
        # noise on every check.
        await update.message.reply_text(
            EMAIL_PROMPT,
            reply_markup=ForceReply(selective=True),
        )
        return

    if sub == "off":
        ok = await user_config_storage.clear_digest_email(str(user_id))
        if ok:
            msg = "📧 Email mirror disabled\\. Digest will go to Telegram only\\."
        else:
            msg = "📧 No email was set — nothing to clear\\."
        await update.message.reply_text(msg, parse_mode="MarkdownV2")
        return

    if sub == "diagnose":
        await _email_diagnose(update, current)
        return

    if sub == "test":
        if not current:
            await update.message.reply_text(
                "📧 Set an address first with `/email <addr>`\\.",
                parse_mode="MarkdownV2",
            )
            return
        if not is_email_configured():
            await update.message.reply_text(
                "⚠️ `RESEND_API_KEY` / `RESEND_FROM` not set in `\\.env`\\.\n\n"
                "Ask the operator to add them and restart the bot\\.",
                parse_mode="MarkdownV2",
            )
            return
        cooldown = _email_test_cooldown_remaining(user_id)
        if cooldown is not None:
            await update.message.reply_text(
                f"⏳ Slow down — wait {int(cooldown) + 1}s before sending "
                "another test email\\.",
                parse_mode="MarkdownV2",
            )
            return
        # Synthetic test payload — one fake "HOLD" ticker so the template
        # renders the same shape a real digest produces. Include a fake
        # `skipped_closed` entry so the footnote template path is exercised
        # too (the architect review for PR #73 noted the template-coverage
        # gap). `safe_date` goes through `_html_escape` to match the
        # documented contract in `_build_html` ("safe_date is pre-escaped
        # by the caller") — for ISO dates this is a no-op today, but a
        # future locale-aware date format would break injection safety on
        # the test path without it.
        from datetime import date as _date
        from html import escape as _html_escape

        today = _date.today().isoformat()
        result = await send_digest_email(
            to_addr=current,
            watchlist=["TEST"],
            status={
                "TEST": {
                    "ticker": "TEST",
                    "signal": "HOLD",
                    "telegraph_url": None,
                }
            },
            safe_date=_html_escape(today),
            date_iso=today,
            skipped_closed=["FAKE.HK"],
        )
        if result.ok:
            await update.message.reply_text(
                f"✅ Test email sent to `{escape_markdown(current, version=2)}`\\. "
                "Check your inbox \\(and spam folder\\)\\.",
                parse_mode="MarkdownV2",
            )
        else:
            await update.message.reply_text(
                "❌ Test email failed — check the bot logs for the Resend error "
                "\\(likely a bad API key or unverified sender domain\\)\\.",
                parse_mode="MarkdownV2",
            )
        return

    # Any other arg = "treat as address". Single-token only — `/email
    # foo@bar.com extra junk` is rejected to avoid accidentally swallowing
    # multi-arg typos.
    if len(args) > 1:
        await update.message.reply_text(
            "📧 Too many arguments\\. Use `/email <single address>` "
            "or `/email off`/`test`\\.",
            parse_mode="MarkdownV2",
        )
        return

    addr = args[0].strip()
    ok = await user_config_storage.set_digest_email(str(user_id), addr)
    if not ok:
        await update.message.reply_text(
            f"❌ `{escape_markdown(addr, version=2)}` doesn't look like a valid "
            "email\\. Expected format: `name@domain\\.tld`\\.",
            parse_mode="MarkdownV2",
        )
        return

    msg = (
        f"📧 Saved\\. Daily digest will be mirrored to "
        f"`{escape_markdown(addr, version=2)}`\\."
    )
    if not is_email_configured():
        msg += (
            "\n\n⚠️ `RESEND_API_KEY` / `RESEND_FROM` not set in `\\.env` — "
            "email won't actually send until the operator wires those\\."
        )
    else:
        msg += " Run `/email test` to send a one-off test message\\."
    await update.message.reply_text(msg, parse_mode="MarkdownV2")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Operational snapshot: uptime, # analyses since boot, graph pool size,
    active LLM config (from `.env`), and the requesting user's digest schedule.
    Useful for spotting a silently-broken bot (expired LLM key, blown pool
    cap, wrong provider in `.env`) without running a full analysis."""
    user_id = update.effective_user.id

    start_ts = context.bot_data.get("start_time")
    uptime_str = _format_uptime(int(time.time() - start_ts)) if start_ts else "unknown"
    analyses_run = context.bot_data.get("analysis_count", 0)
    pool_keys, pool_instances = pool_stats()

    # Surface a precheck warning so users can spot a missing TRADINGAGENTS_*
    # config or a provider-key mismatch without having to fail an actual
    # analysis first. LLM config is bot-wide (one .env, all users) — same
    # values everyone sees, so `/status` shows the active config from
    # `build_config()` rather than per-user fields.
    setup_reason = check_llm_configured()
    config = build_config()
    provider = config.get("llm_provider") or "(not set)"
    deep = config.get("deep_think_llm") or "default"
    quick = config.get("quick_think_llm") or "default"
    rounds = config.get("max_debate_rounds") or 1
    # Effort lives under a per-provider key — read the one matching the
    # active provider so the display matches what AnalysisConfigKey
    # actually threads through.
    effort_key = EFFORT_KEY_BY_PROVIDER.get(provider)
    effort = (config.get(effort_key) if effort_key else None) or "default"
    # Temperature is a cross-provider knob (v0.3.0+); surfaced only when set —
    # when unset it's the provider default and not part of the config identity
    # (mirrors AnalysisConfigKey omitting it from slug/caption/title).
    temperature = config.get("temperature")

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
            # Don't let a bad stored tz / hour (or a future delta-math bug)
            # silently drop the whole line — that defeats /status's "spot a
            # broken bot" purpose. Log + fall back to the empty line (L6).
            logger.warning(
                "status: failed to render next-digest line for user %s",
                user_id,
                exc_info=True,
            )

    # Email mirror line: always shown (set OR not set) so users can verify
    # their own opt-in state from `/status` without invoking `/email`.
    # When set + env not configured, a trailing ⚠️ flags the operator-side
    # gap — same signal the digest summary footer surfaces, just visible
    # outside the daily fire.
    email_addr = (digest or {}).get("email")
    if email_addr:
        email_status = f"`{escape_markdown(email_addr, version=2)}`"
        if not is_email_configured():
            email_status += " ⚠️ env not configured"
    else:
        email_status = "`not set`"
    email_line = f"• Email mirror: {email_status}\n"

    # Token usage + estimated cost line. Tokens accumulate on every
    # `on_llm_end` callback; estimated cost is derived from the active
    # provider/model via `LLM_PRICE_USD_PER_M`. Best-effort — the dollar
    # figure is ±30% (assumes 50/50 deep/quick split per call) and falls
    # back to tokens-only when neither model is in the price table.
    in_tokens, out_tokens = get_token_totals()
    if in_tokens == 0 and out_tokens == 0:
        # No analyses since boot — render zero rather than skipping the
        # line so the operator knows the counter exists.
        tokens_line = "• Tokens since boot: `0 in / 0 out`\n"
    else:
        in_str = _format_token_count(in_tokens)
        out_str = _format_token_count(out_tokens)
        cost = estimate_token_cost_usd(provider, deep, quick, in_tokens, out_tokens)
        if cost is not None:
            # Wrap the dollar figure in its own code span: the `.` in
            # `12.34` (and `$`, `~`, `(`, `)`) are MarkdownV2-reserved and
            # would break the whole message with `can't parse entities` if
            # left bare outside a span. Inside a code span they're literal.
            tokens_line = (
                f"• Tokens since boot: `{in_str} in / {out_str} out` `(~${cost:.2f})`\n"
            )
        else:
            tokens_line = f"• Tokens since boot: `{in_str} in / {out_str} out`\n"

    # MarkdownV2 code spans (backtick-wrapped) suppress most escaping, but
    # a backtick *in the value itself* would break out of the span and
    # cause `Bad Request: can't parse entities`. Provider/model strings
    # come from .env so today never contain backticks — but a future
    # provider name or custom model ID could. Defensive escape for any
    # string that could carry external content. Numeric values stay
    # un-escaped.
    # Graph pool render: the pool is built lazily on first /watch tap, so
    # before any analysis runs `pool_stats()` returns (0, 0). Surface that
    # as "not yet built" rather than "0 instances" — the latter looks like
    # a broken state. After init: "1 pool, N instance(s) built".
    if pool_keys == 0:
        pool_line = "• Graph pool: `not yet built`\n"
    else:
        pool_line = f"• Graph pool: `{pool_instances}` instance\\(s\\) built\n"

    # Temperature bullet appears only when the operator set it — keeps the
    # common-case LLM block tight, consistent with caption()/slug() omitting
    # it when unset.
    temp_line = (
        f"\n• Temperature: `{escape_markdown(str(temperature), version=2)}`"
        if temperature not in (None, "")
        else ""
    )

    message = (
        "*Bot status*\n"
        f"• Uptime: `{escape_markdown(uptime_str, version=2)}`\n"
        f"• Analyses since boot: `{analyses_run}`\n"
        f"{pool_line}"
        f"{tokens_line}"
        f"{digest_line}"
        f"{email_line}\n"
        "*LLM config* \\(`\\.env`\\)\n"
        f"• Provider: `{escape_markdown(provider, version=2)}`\n"
        f"• Deep: `{escape_markdown(deep, version=2)}`\n"
        f"• Quick: `{escape_markdown(quick, version=2)}`\n"
        f"• Rounds: `{rounds}`\n"
        f"• Effort: `{escape_markdown(effort, version=2)}`"
        f"{temp_line}"
    )
    if setup_reason is not None:
        message += f"\n\n⚠️ `{escape_markdown(setup_reason, version=2)}`"
    await update.message.reply_text(message, parse_mode="MarkdownV2")
