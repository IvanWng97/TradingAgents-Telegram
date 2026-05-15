"""Ticker validation backed by yfinance — same source tradingagents reads.

Public entry point is `validate_ticker(raw)`. It normalizes case, applies the
US class-share dot→dash rewrite (`BRK.B` → `BRK-B`) when the original lookup
returns no data, and returns either a canonical symbol or a hint string the
caller can surface to the user.

Network call goes through `asyncio.to_thread` so PTB's event loop stays
responsive. Results are cached in-process with a TTL to avoid hammering Yahoo
on repeated `/add` of the same ticker (and to keep bulk-add fast).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

import yfinance as yf


logger = logging.getLogger(__name__)

# yfinance prints "possibly delisted" warnings to its own logger on every miss
# — quiet that down so a fat-fingered /add doesn't spam the bot logs.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


# Ticker shape validator — alnum groups separated by single `.` or `-`.
# Rejects "..", ".A", "A.", "A..B" so a fat-fingered or hostile input
# can't smuggle path-traversal sequences past validation. Imported by
# `history.normalize_ticker` to ensure the storage-write side and the
# disk-read side agree on what counts as a valid ticker — divergence
# would silently re-open the path-traversal hole the regex closes.
TICKER_RE = re.compile(r"^[A-Z0-9]+(?:[.\-][A-Z0-9]+)*$")

# US class-share pattern: alphanumerics + a single trailing letter after a dot
# (BRK.B, BF.B, RDS.A). Doesn't match international tickers like 0700.HK or
# BARC.L (those have multi-letter exchange suffixes Yahoo expects with the dot).
_CLASS_SHARE_RE = re.compile(r"^([A-Z0-9]+)\.([A-Z])$")

# {symbol: (is_valid, expires_at_unix)} — short TTL so corrections eventually
# show through if the user re-tries. Keep it small; this is best-effort.
# Bounded to _CACHE_MAX entries to neuter the trivial DoS where an open bot
# is spammed with `/add JUNK1 JUNK2 …` to grow the dict unboundedly.
_CACHE: dict[str, tuple[bool, float]] = {}
_CACHE_TTL = 300.0  # 5 minutes
_CACHE_MAX = 1024


def _normalize(raw: str) -> str | None:
    """Uppercase + regex-validate. Returns None for unsafe input."""
    t = raw.strip().upper()
    return t if TICKER_RE.match(t) else None


def _class_share_alt(symbol: str) -> str | None:
    """Return the dash-form of a US class-share symbol, or None if not one."""
    m = _CLASS_SHARE_RE.match(symbol)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _yfinance_has_data(symbol: str) -> bool:
    """Blocking — call inside `asyncio.to_thread`. True iff yfinance can fetch
    at least one daily bar for the symbol."""
    now = time.time()
    cached = _CACHE.get(symbol)
    if cached and cached[1] > now:
        return cached[0]
    try:
        df = yf.Ticker(symbol).history(period="1d", auto_adjust=False)
        ok = not df.empty
    except Exception as e:
        logger.debug("yfinance lookup failed for %s: %s", symbol, e)
        ok = False
    # Bounded FIFO eviction: pop the oldest insertion when full. Python
    # dicts preserve insertion order since 3.7, so iter() yields oldest first.
    if len(_CACHE) >= _CACHE_MAX and symbol not in _CACHE:
        try:
            _CACHE.pop(next(iter(_CACHE)))
        except StopIteration:
            pass
    _CACHE[symbol] = (ok, now + _CACHE_TTL)
    return ok


async def validate_ticker(raw: str) -> tuple[str | None, str | None]:
    """Validate `raw` against yfinance.

    Returns `(canonical_symbol, hint)`:
    - `(SYMBOL, None)` — valid; SYMBOL is the form to store/use.
    - `(SYMBOL, "auto-corrected …")` — caller-typed form failed but a
      class-share rewrite worked; SYMBOL is the corrected form.
    - `(None, "<reason>")` — invalid; surface the hint to the user.
    """
    symbol = _normalize(raw)
    if symbol is None:
        return None, f"{raw}: not a valid ticker symbol."

    if await asyncio.to_thread(_yfinance_has_data, symbol):
        return symbol, None

    # Try the class-share rewrite as a hint if applicable.
    alt = _class_share_alt(symbol)
    if alt and await asyncio.to_thread(_yfinance_has_data, alt):
        return alt, f"Note: {symbol} → {alt} (Yahoo class-share format)."

    return None, (
        f"{symbol}: not found on Yahoo Finance. "
        "Double-check the symbol — for class shares use a dash (e.g. BRK-B)."
    )


def normalize_ticker_for_display(ticker: str, *, uppercase: bool = True) -> str:
    """Return a display-formatted ticker string.

    Helper for future UI surfaces that may want to render tickers consistently
    across pickers, captions, and headers. Currently a thin wrapper around
    `.upper()` / `.strip()`, but centralizes the logic so future formatting
    rules (e.g. dot→dash rewriting at the display layer, locale-specific
    capitalization) land in one place.
    """
    cleaned = ticker.strip()
    if uppercase:
        cleaned = cleaned.upper()
    return cleaned
