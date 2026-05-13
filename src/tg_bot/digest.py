"""Rendering helpers for the daily-digest picker.

Pure functions — no I/O, no globals beyond the static tz catalog. Callers
read the user's digest block from `UserConfigStorage` and pass it in. The
caller is responsible for delivering the returned (text, keyboard) pair
to Telegram.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown


# Curated IANA zones for the picker — covers ~95% of users. Each entry is
# (display_label, iana_name). Display includes the abbreviation in parens
# so the user picks by familiar shorthand.
TZ_OPTIONS: list[tuple[str, str]] = [
    ("Pacific (PT)", "America/Los_Angeles"),
    ("Eastern (ET)", "America/New_York"),
    ("Central (CT)", "America/Chicago"),
    ("Mountain (MT)", "America/Denver"),
    ("UTC", "Etc/UTC"),
    ("GMT/BST", "Europe/London"),
    ("CET/CEST", "Europe/Paris"),
    ("IST (India)", "Asia/Kolkata"),
    ("JST (Japan)", "Asia/Tokyo"),
    ("AEST/AEDT", "Australia/Sydney"),
]

# Short label for the status line ("Daily Digest — ON, 10:00 PT").
_TZ_SHORT: dict[str, str] = {
    "America/Los_Angeles": "PT",
    "America/New_York": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "Etc/UTC": "UTC",
    "Europe/London": "UK",
    "Europe/Paris": "CE",
    "Asia/Kolkata": "IST",
    "Asia/Tokyo": "JST",
    "Australia/Sydney": "AU",
}


def tz_short(iana: str | None) -> str:
    """Compact tz abbreviation for the status line. Falls back to the IANA
    name if not in our curated catalog (should be rare — picker only
    surfaces curated zones)."""
    if not iana:
        return ""
    return _TZ_SHORT.get(iana, iana)


def next_fire(hour_local: int, tz_str: str, now: datetime | None = None) -> datetime:
    """Next absolute firing instant for a daily run at `hour_local:00` in
    `tz_str`. `now` defaults to the current time in that tz (overridable for
    tests). DST is handled by ZoneInfo."""
    tz = ZoneInfo(tz_str)
    if now is None:
        now = datetime.now(tz)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    target = now.replace(hour=hour_local, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def humanize_delta(target: datetime, now: datetime | None = None) -> str:
    """'in 4h 23m' / 'in 12m' / 'any moment'. Coarsest non-zero unit pair."""
    if now is None:
        now = datetime.now(target.tzinfo) if target.tzinfo else datetime.now()
    total_minutes = int((target - now).total_seconds() // 60)
    if total_minutes < 1:
        return "any moment"
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"in {minutes}m"
    if minutes == 0:
        return f"in {hours}h"
    return f"in {hours}h {minutes}m"


_TICKERS_PAGE_SIZE = 9  # 3×3, matches /watch for visual consistency


def _status_line(
    digest: dict[str, Any] | None,
    watchlist: list[str] | None = None,
) -> str:
    """MarkdownV2 status line. Variable parts (time, abbr, delta) escaped."""
    if not digest or not digest.get("tz"):
        return "*Daily Digest* — pick a time zone to begin\\."

    tz = digest["tz"]
    hour = digest.get("hour_local")
    enabled = digest.get("enabled", False)

    # Ticker-count suffix: only show when both a digest schedule exists AND
    # we have a watchlist context to render the M/N count meaningfully.
    suffix = ""
    if watchlist is not None:
        sel = set(digest.get("tickers") or [])
        in_wl = sum(1 for t in watchlist if t in sel)
        suffix = f" · 📋 {in_wl}/{len(watchlist)} tickers"
    suffix_v2 = escape_markdown(suffix, version=2)

    if hour is None:
        # tz set, no hour yet — first-time setup mid-flow.
        tz_v2 = escape_markdown(tz_short(tz), version=2)
        return f"*Daily Digest* — OFF, time zone {tz_v2}{suffix_v2}"

    time_label = f"{hour:02d}:00 {tz_short(tz)}"
    time_v2 = escape_markdown(time_label, version=2)
    if not enabled:
        return f"*Daily Digest* — OFF \\(last set: {time_v2}\\){suffix_v2}"
    try:
        delta = humanize_delta(next_fire(hour, tz))
    except ZoneInfoNotFoundError:
        delta = "?"
    delta_v2 = escape_markdown(delta, version=2)
    return f"*Daily Digest* — ON, {time_v2} \\({delta_v2}\\){suffix_v2}"


def _hour_keyboard(
    digest: dict[str, Any] | None,
    watchlist: list[str] | None = None,
) -> InlineKeyboardMarkup:
    """6×4 hour grid + action row. Selected hour gets a ✅ prefix.
    "🔕 Off" only appears when enabled (no point un-disabling).

    The 📋 Tickers button is only shown when the caller passes a watchlist
    (i.e. has the data to render the filter screen on tap)."""
    selected_hour = (
        (digest or {}).get("hour_local") if (digest or {}).get("enabled") else None
    )
    rows: list[list[InlineKeyboardButton]] = []
    for row_start in range(0, 24, 6):
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅{h:02d}" if h == selected_hour else f"{h:02d}",
                    callback_data=f"digest:hour:{h}",
                )
                for h in range(row_start, row_start + 6)
            ]
        )
    actions: list[InlineKeyboardButton] = []
    if watchlist is not None:
        sel = set((digest or {}).get("tickers") or [])
        in_wl = sum(1 for t in watchlist if t in sel)
        actions.append(
            InlineKeyboardButton(
                f"📋 Tickers ({in_wl}/{len(watchlist)})",
                callback_data="digest:tickerpick",
            )
        )
    actions.append(InlineKeyboardButton("🌍 Time zone", callback_data="digest:tzpick"))
    actions.append(InlineKeyboardButton("▶ Run now", callback_data="digest:run"))
    if (digest or {}).get("enabled"):
        actions.append(InlineKeyboardButton("🔕 Off", callback_data="digest:off"))
    # Two-per-row chunking so 3 / 4 / 5+ buttons all stay visually balanced
    # without a wrapping shock if the action set ever grows.
    for i in range(0, len(actions), 2):
        rows.append(actions[i : i + 2])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel:digest")])
    return InlineKeyboardMarkup(rows)


def _tickers_keyboard(
    watchlist: list[str], selected: set[str], page: int
) -> InlineKeyboardMarkup:
    """3×3 paginated ticker toggle grid + bulk row + nav row.

    Each ticker row carries `digest:tt:{TICKER}` so a tap toggles. Selection
    state lives on disk (digest.tickers); per-tap saves keep it durable
    across page changes and bot restarts. There's no explicit "Done" — the
    `← Back` button is always safe because every toggle has already saved.

    Caller (`build_digest_response`) gates the empty-watchlist case before
    calling here, so this function assumes `watchlist` is non-empty.
    """
    rows: list[list[InlineKeyboardButton]] = []
    total_pages = max(
        1, (len(watchlist) + _TICKERS_PAGE_SIZE - 1) // _TICKERS_PAGE_SIZE
    )
    page = max(0, min(page, total_pages - 1))
    start = page * _TICKERS_PAGE_SIZE
    page_items = watchlist[start : start + _TICKERS_PAGE_SIZE]

    # 3-per-row toggle grid for the current page.
    for i in range(0, len(page_items), 3):
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅ {t}" if t in selected else t,
                    callback_data=f"digest:tt:{t}",
                )
                for t in page_items[i : i + 3]
            ]
        )

    # Pagination row only when needed.
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        nav.append(
            InlineKeyboardButton(
                "← Prev" if page > 0 else " ",
                callback_data="digest:ttpage:prev"
                if page > 0
                else "digest:ttpage:noop",
            )
        )
        nav.append(
            InlineKeyboardButton(
                f"📄 {page + 1}/{total_pages}", callback_data="digest:ttpage:noop"
            )
        )
        nav.append(
            InlineKeyboardButton(
                "Next →" if page < total_pages - 1 else " ",
                callback_data=(
                    "digest:ttpage:next"
                    if page < total_pages - 1
                    else "digest:ttpage:noop"
                ),
            )
        )
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton("✓ Select all", callback_data="digest:ttall"),
            InlineKeyboardButton("✗ Clear", callback_data="digest:ttclear"),
        ]
    )
    rows.append([InlineKeyboardButton("← Back", callback_data="digest:hourpick")])
    return InlineKeyboardMarkup(rows)


def _tz_keyboard(digest: dict[str, Any] | None) -> InlineKeyboardMarkup:
    """5×2 tz grid. Selected tz gets a ✅ prefix.
    Back-to-hours only when an hour was set already (otherwise nowhere to
    go back to — the first-time flow flows tz → hours)."""
    current_tz = (digest or {}).get("tz")
    has_hour = (digest or {}).get("hour_local") is not None
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(TZ_OPTIONS), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅ {label}" if iana == current_tz else label,
                    callback_data=f"digest:tz:{iana}",
                )
                for label, iana in TZ_OPTIONS[i : i + 2]
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if has_hour:
        nav.append(InlineKeyboardButton("← Back", callback_data="digest:hourpick"))
    nav.append(InlineKeyboardButton("❌ Cancel", callback_data="cancel:digest"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


def build_digest_response(
    digest: dict[str, Any] | None,
    mode: str = "auto",
    watchlist: list[str] | None = None,
    page: int = 0,
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the digest picker as (MarkdownV2 caption, inline keyboard).

    `digest` is the user's digest block (or None if never set).
    `watchlist`, when provided, drives the 📋 Tickers (N/M) button on the
    hour screen and the toggle grid on the tickers screen. Callers without
    watchlist context (legacy paths) will see the hour screen without that
    button — graceful degradation.
    `mode`:
      - "auto" (default): pick the right screen — tz picker on first-time,
        hour picker once a tz exists.
      - "hours": force hour picker (used by the `digest:hourpick` back nav).
      - "tz": force tz picker (used by `digest:tzpick`).
      - "tickers": ticker filter screen (used by `digest:tickerpick`).
        Requires `watchlist`.
    """
    if mode == "auto":
        mode = "hours" if digest and digest.get("tz") else "tz"

    text = _status_line(digest, watchlist=watchlist)
    if mode == "tz":
        if digest and digest.get("tz"):
            text += "\n\nTap a different zone, or ❌ Cancel\\."
        else:
            text += "\n\nTap a zone to begin\\."
        return text, _tz_keyboard(digest)

    if mode == "tickers":
        if not watchlist:
            text += "\n\n_Watchlist is empty — add tickers via /add first\\._"
            empty_kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("← Back", callback_data="digest:hourpick")]]
            )
            return text, empty_kb
        text += "\n\nTap each ticker to include in the daily digest\\."
        selected = set((digest or {}).get("tickers") or [])
        return text, _tickers_keyboard(watchlist, selected, page)

    # mode == "hours"
    text += "\n\nTap an hour to enable / change\\."
    return text, _hour_keyboard(digest, watchlist=watchlist)
