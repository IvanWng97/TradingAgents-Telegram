"""Rendering helpers for the daily-digest picker.

Pure functions — no I/O, no globals beyond the static tz catalog. Callers
read the user's digest block from `UserConfigStorage` and pass it in. The
caller is responsible for delivering the returned (text, keyboard) pair
to Telegram.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
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


def tz_short(iana: Optional[str]) -> str:
    """Compact tz abbreviation for the status line. Falls back to the IANA
    name if not in our curated catalog (should be rare — picker only
    surfaces curated zones)."""
    if not iana:
        return ""
    return _TZ_SHORT.get(iana, iana)


def next_fire(hour_local: int, tz_str: str, now: Optional[datetime] = None) -> datetime:
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


def humanize_delta(target: datetime, now: Optional[datetime] = None) -> str:
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


def _status_line(digest: Optional[dict[str, Any]]) -> str:
    """MarkdownV2 status line. Variable parts (time, abbr, delta) escaped."""
    if not digest or not digest.get("tz"):
        return "*Daily Digest* — pick a time zone to begin\\."

    tz = digest["tz"]
    hour = digest.get("hour_local")
    enabled = digest.get("enabled", False)

    if hour is None:
        # tz set, no hour yet — first-time setup mid-flow.
        tz_v2 = escape_markdown(tz_short(tz), version=2)
        return f"*Daily Digest* — OFF, time zone {tz_v2}"

    time_label = f"{hour:02d}:00 {tz_short(tz)}"
    time_v2 = escape_markdown(time_label, version=2)
    if not enabled:
        return f"*Daily Digest* — OFF \\(last set: {time_v2}\\)"
    try:
        delta = humanize_delta(next_fire(hour, tz))
    except ZoneInfoNotFoundError:
        delta = "?"
    delta_v2 = escape_markdown(delta, version=2)
    return f"*Daily Digest* — ON, {time_v2} \\({delta_v2}\\)"


def _hour_keyboard(digest: Optional[dict[str, Any]]) -> InlineKeyboardMarkup:
    """6×4 hour grid + action row. Selected hour gets a ✅ prefix.
    "🔕 Off" only appears when enabled (no point un-disabling)."""
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
    actions: list[InlineKeyboardButton] = [
        InlineKeyboardButton("🌍 Time zone", callback_data="digest:tzpick"),
        InlineKeyboardButton("▶ Run now", callback_data="digest:run"),
    ]
    if (digest or {}).get("enabled"):
        actions.append(InlineKeyboardButton("🔕 Off", callback_data="digest:off"))
    rows.append(actions)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel:digest")])
    return InlineKeyboardMarkup(rows)


def _tz_keyboard(digest: Optional[dict[str, Any]]) -> InlineKeyboardMarkup:
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
    digest: Optional[dict[str, Any]],
    mode: str = "auto",
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the digest picker as (MarkdownV2 caption, inline keyboard).

    `digest` is the user's digest block (or None if never set).
    `mode`:
      - "auto" (default): pick the right screen — tz picker on first-time,
        hour picker once a tz exists.
      - "hours": force hour picker (used by the `digest:hourpick` back nav).
      - "tz": force tz picker (used by `digest:tzpick`).
    """
    if mode == "auto":
        mode = "hours" if digest and digest.get("tz") else "tz"

    text = _status_line(digest)
    if mode == "tz":
        if digest and digest.get("tz"):
            text += "\n\nTap a different zone, or ❌ Cancel\\."
        else:
            text += "\n\nTap a zone to begin\\."
        return text, _tz_keyboard(digest)

    # mode == "hours"
    text += "\n\nTap an hour to enable / change\\."
    return text, _hour_keyboard(digest)
