"""Per-ticker market-session gate for the daily digest.

The digest fan-out should not run on days when no market is open — sending a
"here's nothing to say" message every Saturday / Christmas / Independence Day
trains users to ignore the morning ping. This module answers the single
question "is this ticker's exchange in session on this date?" by mapping the
yfinance ticker suffix to its exchange's `exchange_calendars` calendar.

Yahoo Finance suffix conventions (yfinance pass-through):

    .SS / .SZ  → Shanghai / Shenzhen (CN; share the XSHG calendar — Shenzhen
                 has no dedicated calendar in exchange_calendars but trades
                 on the same schedule)
    .HK        → Hong Kong (XHKG)
    .NS / .BO  → India NSE / BSE (XBOM — single Indian session for both)
    .T         → Tokyo (XTKS)
    .KS        → Korea KRX (XKRX)
    .DE        → Germany XETRA (XETR)
    .L         → UK LSE (XLON)
    .PA        → France Paris (XPAR)
    .AX        → Australia ASX (XASX)
    .TO        → Canada Toronto (XTSE)
    .SI        → Singapore SGX (XSES)
    .TW        → Taiwan TWSE (XTAI)
    .SA        → Brazil B3 (BVMF)
    (no suffix or unknown) → US NYSE (XNYS) — covers ~80% of typical
                              watchlists and is the right default for ADRs
                              of foreign companies traded in the US.

Adding a new suffix is a one-line change to `_SUFFIX_TO_CAL`; the test in
`tests/test_market_calendar.py` exercises representative suffixes per region
so a typo or a renamed calendar surfaces loudly.
"""

from __future__ import annotations

import logging
from datetime import date
from functools import lru_cache

from exchange_calendars import get_calendar


logger = logging.getLogger(__name__)


# yfinance suffix → exchange_calendars code. Suffix is matched case-insensitive
# against the segment AFTER the last `.` in the ticker. Tickers without a
# suffix (most US listings) fall through to the default `XNYS`.
_SUFFIX_TO_CAL: dict[str, str] = {
    "SS": "XSHG",  # Shanghai
    "SZ": "XSHG",  # Shenzhen — uses Shanghai's calendar (no dedicated XSHE)
    "HK": "XHKG",
    "NS": "XBOM",  # India NSE
    "BO": "XBOM",  # India BSE
    "T": "XTKS",  # Tokyo
    "KS": "XKRX",  # Korea
    "DE": "XETR",  # Germany XETRA
    "L": "XLON",  # London
    "PA": "XPAR",  # Paris
    "AX": "XASX",  # Australia
    "TO": "XTSE",  # Toronto
    "SI": "XSES",  # Singapore
    "TW": "XTAI",  # Taiwan
    "SA": "BVMF",  # Brazil
}

_DEFAULT_CAL = "XNYS"  # US tickers have no suffix


def _suffix_for(ticker: str) -> str | None:
    """Return the upper-cased suffix after the last `.`, or None for tickers
    with no `.` (typical US listings). `BRK-B` returns None (hyphen, not dot)."""
    if "." not in ticker:
        return None
    return ticker.rsplit(".", 1)[-1].upper()


@lru_cache(maxsize=64)
def _calendar_for(ticker: str):
    """Resolve the `exchange_calendars` calendar for this ticker.

    Cached on the ticker (not the suffix) so the LRU surface is per-ticker
    and matches the typical digest watchlist size (5–20 tickers). Even at
    100% miss rate the lookup is microseconds — the cache exists to avoid
    re-resolving the same calendar object across the fan-out loop."""
    suffix = _suffix_for(ticker)
    code = _SUFFIX_TO_CAL.get(suffix, _DEFAULT_CAL) if suffix else _DEFAULT_CAL
    return get_calendar(code)


def is_market_open_for(ticker: str, on_date: date) -> bool:
    """True iff this ticker's exchange has a trading session on `on_date`.

    Used by `run_user_digest` to drop tickers from today's fan-out when
    their market is closed (weekend or holiday). On any unexpected error
    from `is_session` (e.g. exchange_calendars data-range exhausted on a
    very-future date) we fail-OPEN and return True — better to run a
    redundant analysis than to silently drop the user's digest.

    Note: this fallback does NOT cover ImportError on `exchange_calendars`
    itself. The dep is imported at module load (top-of-file
    `from exchange_calendars import get_calendar`); a missing or broken
    install fails the entire bot at process start. The dep is declared
    in `pyproject.toml` as a hard requirement — restructuring the fallback
    to cover ImportError would mean making the gate optional, which would
    silently degrade to "always open" without operator visibility.
    """
    try:
        return _calendar_for(ticker).is_session(on_date)
    except Exception as e:
        # Fail-open is intentional. The exchange_calendars data tables
        # are bounded (~2050) and a request beyond the range raises; we
        # don't want a calendar-data gap to silently swallow digests.
        logger.warning(
            "market_calendar: is_session failed for %s on %s — failing OPEN: %s",
            ticker,
            on_date.isoformat(),
            e,
        )
        return True
