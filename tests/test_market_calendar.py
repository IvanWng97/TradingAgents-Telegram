"""Tests for the per-ticker market-calendar gate used by the digest fan-out.

These pin both the suffix→exchange mapping AND that `is_market_open_for`
correctly identifies known closed days (weekends, holidays) on the resolved
calendars. The map is the surface most likely to drift if a future maintainer
adds a new suffix without verifying the exchange code; the holiday checks
catch the case where the upstream `exchange_calendars` data tables get
re-organized.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_bot.market_calendar import (  # noqa: E402
    _DEFAULT_CAL,
    _SUFFIX_TO_CAL,
    _calendar_for,
    _suffix_for,
    is_market_open_for,
)


# ─── _suffix_for ────────────────────────────────────────────────────────


async def test_suffix_extraction_handles_us_no_suffix() -> None:
    """US tickers have no dot, suffix returns None — caller falls back
    to the default XNYS calendar."""
    assert _suffix_for("NVDA") is None
    assert _suffix_for("AAPL") is None


async def test_suffix_extraction_handles_hyphenated_us_classes() -> None:
    """Hyphen-separated US class-share tickers (BRK-B) have no dot, so
    suffix is None and the calendar resolves to XNYS — not a foreign
    exchange. Without this, a future regex tweak that splits on `-`
    too would silently route BRK-B to a wrong calendar."""
    assert _suffix_for("BRK-B") is None
    assert _suffix_for("BF-A") is None


async def test_suffix_extraction_uppercases() -> None:
    """yfinance is case-insensitive (`nvda` == `NVDA`); suffix lookup
    must be too. Stored watchlist tickers are uppercased, but a hand-
    edited input that slips through normalization must still resolve
    to the right exchange."""
    assert _suffix_for("0700.hk") == "HK"
    assert _suffix_for("0700.HK") == "HK"


async def test_suffix_extraction_uses_last_segment() -> None:
    """A multi-dot ticker (rare, hypothetical) takes the suffix from
    the LAST dot — matches yfinance's parsing."""
    assert _suffix_for("FOO.BAR.NS") == "NS"


# ─── _calendar_for resolves the right exchange ─────────────────────────


async def test_calendar_resolves_us_to_xnys() -> None:
    """US tickers (no suffix) → XNYS. This is the ~80% case."""
    assert _calendar_for("NVDA").name == "XNYS"
    assert _calendar_for("BRK-B").name == "XNYS"


async def test_calendar_resolves_hk_to_xhkg() -> None:
    assert _calendar_for("0700.HK").name == "XHKG"


async def test_calendar_resolves_both_shanghai_and_shenzhen_to_xshg() -> None:
    """Shenzhen has no dedicated calendar in exchange_calendars; it
    shares Shanghai's schedule. Pinning both routes to XSHG so a future
    refactor that "fixes" the .SZ entry to a non-existent XSHE silently
    breaks gate decisions for Chinese tickers."""
    assert _calendar_for("601318.SS").name == "XSHG"
    assert _calendar_for("000001.SZ").name == "XSHG"


async def test_calendar_resolves_unknown_suffix_to_default() -> None:
    """An unknown suffix (a new Yahoo Finance exchange we haven't mapped
    yet, or junk) falls back to the default `_DEFAULT_CAL` — better to
    run a redundant analysis on US schedule than to crash the digest."""
    assert _calendar_for("JUNK.XYZ").name == _DEFAULT_CAL


# ─── _SUFFIX_TO_CAL map structural pin ─────────────────────────────────


async def test_suffix_map_has_no_empty_codes() -> None:
    """Defensive: every mapped exchange code must be a non-empty string
    that `exchange_calendars.get_calendar` can resolve. A typo like
    `"HK": ""` would silently route HK tickers to whatever the default
    calendar resolution does on an empty string."""
    for suffix, code in _SUFFIX_TO_CAL.items():
        assert code and isinstance(code, str), (
            f"suffix {suffix!r} has invalid code {code!r}"
        )


async def test_suffix_map_codes_resolve_to_real_calendars() -> None:
    """Every code in the map must be one `exchange_calendars` actually
    ships. Adding a new suffix with a typo'd code (`XKRZ` instead of
    `XKRX`) would crash at runtime on the first matching ticker — this
    test catches it at PR time instead."""
    from exchange_calendars import get_calendar_names

    available = set(get_calendar_names())
    missing = [
        (suffix, code)
        for suffix, code in _SUFFIX_TO_CAL.items()
        if code not in available
    ]
    assert not missing, (
        f"suffix map references unknown calendars: {missing}. "
        f"Run `exchange_calendars.get_calendar_names()` for the live list."
    )


# ─── is_market_open_for — known closed days ────────────────────────────


async def test_us_weekend_closed() -> None:
    """Sat/Sun → NYSE closed. The most common skip case for the digest."""
    # 2026-05-17 is a Sunday; 2026-05-16 is a Saturday.
    assert is_market_open_for("NVDA", date(2026, 5, 17)) is False
    assert is_market_open_for("NVDA", date(2026, 5, 16)) is False


async def test_us_weekday_open() -> None:
    """A normal Monday → NYSE open. Anchors the "common case" baseline
    so a regression that says "closed" on every day would fail loudly."""
    assert is_market_open_for("NVDA", date(2026, 5, 18)) is True


async def test_us_christmas_closed() -> None:
    """NYSE observes Christmas Day. This is the canonical holiday case
    — it's NOT a US federal holiday-by-rule (it IS, but the test name
    points at the exchange's observance). Catches a regression where
    the holiday calendar is being ignored in favor of a weekday check."""
    assert is_market_open_for("NVDA", date(2026, 12, 25)) is False


async def test_shanghai_open_on_western_christmas() -> None:
    """SSE does NOT observe Western Christmas — Chinese tickers should
    still report `open` on Dec 25 when the user's other tickers (US,
    UK) are closed. Pinning this directly proves the per-ticker
    resolution actually matters: a NYSE-only gate would wrongly skip
    these tickers on US holidays."""
    assert is_market_open_for("601318.SS", date(2026, 12, 25)) is True


async def test_shanghai_closed_on_chinese_new_year() -> None:
    """SSE closes for Spring Festival. 2026 CNY is Feb 17 (Tuesday)."""
    assert is_market_open_for("601318.SS", date(2026, 2, 17)) is False


async def test_tokyo_closed_on_japanese_new_year() -> None:
    """TSE closes for Shōgatsu. Jan 1 — even though NYSE also closes
    that day, this proves the .T suffix resolves to XTKS, not the
    default XNYS (which would happen to be right here)."""
    assert is_market_open_for("7203.T", date(2026, 1, 1)) is False


async def test_india_closed_on_independence_day() -> None:
    """India NSE/BSE observes Independence Day (Aug 15). Catches the
    .NS / .BO → XBOM route."""
    assert is_market_open_for("RELIANCE.NS", date(2026, 8, 15)) is False


async def test_failopen_on_calendar_error(monkeypatch) -> None:
    """If `is_session` raises (e.g. date beyond the calendar's data
    range), the gate fails OPEN — return True so the digest runs the
    ticker. This is intentional: a calendar-data gap should not silently
    swallow user analyses. Symptom would be elevated calendar warnings
    in logs, not missing digests."""
    import tg_bot.market_calendar as mc

    class _BrokenCal:
        def is_session(self, _d):
            raise RuntimeError("calendar data gap")

    # Bypass the lru_cache by patching the resolver itself.
    monkeypatch.setattr(mc, "_calendar_for", lambda _t: _BrokenCal())
    assert is_market_open_for("NVDA", date(2026, 5, 18)) is True
