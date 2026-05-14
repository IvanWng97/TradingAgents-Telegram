"""Shared response builders consumed by both commands.py and callbacks.py.

Four are keyboard builders for the paginated picker UIs (`/watch`,
`/refresh`, `/del`, `/history`); one (`build_history_response`)
republishes a saved analysis to Telegraph and returns the HTML caption.
All five are pure callees of the handler layer — they read storage and
build response payloads but never register PTB handlers themselves.

Lives in `handlers/` because it's handler-layer code, but the
dependency direction now flows correctly:

    commands.py ──┐
                  ├──> pickers.py ──> storage/, rendering/, bot/
    callbacks.py ─┘

Before this extraction, `callbacks.py` imported these from `commands.py`
(the dispatcher importing from the entry-point file). After: both
import from `pickers.py`; neither file imports from the other.
"""

from __future__ import annotations

from datetime import date
from html import escape as _html_escape

import markdown
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

from tg_bot.history import (
    list_available_dates,
    list_available_tickers,
    load_historical_state,
)
from tg_bot.rendering.formatters import format_analysis_result_markdown
from tg_bot.rendering.telegraph_client import publish_to_telegraph
from tg_bot.storage import watchlist_storage


# `WATCHLIST_PAGE_SIZE` defines the picker grid (3 columns × 3 rows).
# Used by `build_watchlist_response` here AND read by callbacks.py to
# compute clamp bounds when paginating, so it must be importable.
WATCHLIST_PAGE_SIZE = 9


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


async def build_history_response(
    ticker: str, date_str: str
) -> tuple[str, dict | None, str | None]:
    """Load + publish a historical analysis.

    Returns `(html_caption, final_state, telegraph_url)`. Caption is
    HTML (callers must pass `parse_mode="HTML"`). `final_state` is the
    raw on-disk tradingagents log or None if no record exists.
    `telegraph_url` is the published page URL, or None if publish failed.
    Callers pass `telegraph_url` to `_full_report_keyboard` so the
    `📰 Instant View` button links to the right page; the inline `<a>`
    Telegraph link was removed from the caption when the two-button
    keyboard replaced it.
    """
    safe_ticker = _html_escape(ticker)
    safe_date = _html_escape(date_str)

    state = load_historical_state(ticker, date_str)
    if state is None:
        return f"No analysis found for {safe_ticker} on {safe_date}.", None, None

    # Pass the historical date so the Telegraph page leads with a
    # "Generated YYYY-MM-DD" header. config is unknown for /history (the
    # tradingagents on-disk log doesn't record it), so config_summary
    # stays None.
    try:
        gen_date = date.fromisoformat(date_str)
    except ValueError:
        gen_date = None
    md_body = format_analysis_result_markdown(
        ticker, state, signal="historical", generated_at=gen_date
    )
    html = markdown.markdown(md_body, extensions=["tables"])
    # `/history` republishes a snapshot of a past analysis whose original
    # config is unknown (tradingagents' on-disk log doesn't record it), so
    # we can't use `AnalysisConfigKey.telegraph_title()` here. The bare
    # title pattern previously used (`f"{ticker} {date_str}"`) collided
    # across multiple `/history` invocations of the same (ticker, date)
    # and Telegraph appended `-2`/`-3` to disambiguate — defeating URL
    # stability the way PR #30 fixed for `/watch`. The `Historical` token
    # both differentiates from the live `<TICKER> Analysis · ...` titles
    # and makes the URL self-document as a republished snapshot.
    telegraph_url = await publish_to_telegraph(f"{ticker} Historical {date_str}", html)

    msg = f"📜 <b>{safe_ticker}</b> — {safe_date}"
    if not telegraph_url:
        msg += "\n\n⚠️ Instant View unavailable (Telegraph publish failed)."
    return msg, state, telegraph_url
