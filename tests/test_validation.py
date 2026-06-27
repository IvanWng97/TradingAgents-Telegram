"""Smoke tests for ticker-validation regex (security regression coverage).

The `_TICKER_RE` in `tg_bot.history` and `tg_bot.validation` is the only
guard against path-traversal sequences before the ticker is joined into
a filesystem path (history) or sent to yfinance (validation). The
original `^[A-Z0-9.\\-]+$` matched literal `..` because `.` inside a
character class is not a metachar — `/history ..` would compose
`<results_dir>/../TradingAgentsStrategy_logs/full_states_log_<date>.json`.

These scenarios pin the invariant: alphanumeric tokens separated by a
single `.` or `-`, no consecutive separators, no leading/trailing
separator. Keep both modules in lockstep — they share the same security
contract.

Run with: pytest tests/test_validation.py
"""

from __future__ import annotations

import sys
from pathlib import Path


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_bot import validation  # noqa: E402
from tg_bot.history import normalize_ticker as history_normalize  # noqa: E402
from tg_bot.validation import _normalize as validation_normalize  # noqa: E402


VALID = ["NVDA", "AAPL", "BRK-B", "RDS.A", "BF.B", "7203.T", "0700.HK", "A.B-C"]
TRAVERSAL = [
    "..",
    ".",
    ".A",
    "A.",
    "A..B",
    "..A",
    "A..",
    "...",
    "-",
    "A-",
    "-A",
    "A--B",
]


def _check(label: str, fn) -> None:
    for sym in VALID:
        out = fn(sym)
        assert out == sym, f"{label}: rejected legit ticker {sym!r} -> {out!r}"
    for sym in TRAVERSAL:
        out = fn(sym)
        assert out is None, f"{label}: accepted unsafe input {sym!r} -> {out!r}"


def test_history_normalize_ticker() -> None:
    _check("history.normalize_ticker", history_normalize)


def test_validation_normalize() -> None:
    _check("validation._normalize", validation_normalize)


def test_lowercase_input_round_trips() -> None:
    """Both helpers uppercase before validating; lowercase legit tickers
    must round-trip, lowercase traversal must still be rejected."""
    assert history_normalize("brk-b") == "BRK-B"
    assert validation_normalize("brk-b") == "BRK-B"
    assert history_normalize("..") is None
    assert validation_normalize("..") is None


# --- Invariant #10: class-share dot→dash yfinance retry ----------------------
# `validate_ticker` re-queries the dash form (`BRK-B`) via `_class_share_alt`
# when the user-typed dot form (`BRK.B`) returns an empty yfinance history.
# The existing tests above only exercise the `_normalize` regex; the yfinance
# retry path is never invoked. These pin it by stubbing the `yf.Ticker(...).
# history(...)` seam validate_ticker actually calls (validation.py imports
# `import yfinance as yf`, so the live attribute is `validation.yf.Ticker`).


def _make_fake_ticker(known: set[str]):
    """Build a stand-in `yf.Ticker` whose `.history()` reports a non-empty
    frame only for symbols in `known` (mirrors `df.empty` truthiness)."""

    class _FakeHistory:
        def __init__(self, empty: bool) -> None:
            self.empty = empty

    class _FakeTicker:
        def __init__(self, symbol: str) -> None:
            self._symbol = symbol

        def history(self, *args: object, **kwargs: object) -> _FakeHistory:
            return _FakeHistory(empty=self._symbol not in known)

    return _FakeTicker


async def test_class_share_dot_retried_as_dash(monkeypatch) -> None:
    """BRK.B is empty on Yahoo but BRK-B is not → validate_ticker auto-corrects
    to the dash form and surfaces the class-share note."""
    validation._CACHE.clear()
    monkeypatch.setattr(validation.yf, "Ticker", _make_fake_ticker({"BRK-B"}))

    symbol, hint = await validation.validate_ticker("BRK.B")

    assert symbol == "BRK-B"
    assert hint is not None and "BRK-B" in hint


async def test_unknown_class_share_form_fails_after_both_lookups(
    monkeypatch,
) -> None:
    """When BOTH the dot form and its dash rewrite are empty, validate_ticker
    returns the not-found shape (None symbol + hint)."""
    validation._CACHE.clear()
    monkeypatch.setattr(validation.yf, "Ticker", _make_fake_ticker(set()))

    symbol, hint = await validation.validate_ticker("ZZZZ.Q")

    assert symbol is None
    assert hint is not None and "not found" in hint


def test_cache_eviction_at_capacity_stays_bounded_and_exception_free(
    monkeypatch,
) -> None:
    """L5: a full-cache eviction must pop the oldest key, keep the bound, and
    never raise. The fix snapshots the first key (`list(_CACHE)[0]`) and
    `pop(key, None)` instead of `pop(next(iter(_CACHE)))` — which, under the
    concurrent bulk-`/add` workload where workers mutate the unsynchronized
    `_CACHE` in parallel (yfinance releases the GIL), could raise
    `RuntimeError: dictionary changed size during iteration` or a double-pop
    `KeyError` and fail the whole `/add`. That race isn't deterministically
    reproducible single-threaded, so this is a coverage pin on the eviction
    arithmetic (bound preserved, oldest dropped, no raise)."""
    import time

    validation._CACHE.clear()
    future = time.time() + 10_000  # live TTL so nothing is treated as expired
    for i in range(validation._CACHE_MAX):
        validation._CACHE[f"SYM{i}"] = (True, future)
    oldest = next(iter(validation._CACHE))  # SYM0, inserted first
    assert len(validation._CACHE) == validation._CACHE_MAX

    monkeypatch.setattr(validation.yf, "Ticker", _make_fake_ticker({"NEWSYM"}))
    ok = validation._yfinance_has_data("NEWSYM")  # forces one eviction

    assert ok is True
    assert "NEWSYM" in validation._CACHE
    assert oldest not in validation._CACHE, "oldest entry should be evicted"
    assert len(validation._CACHE) == validation._CACHE_MAX, "bound preserved"
    validation._CACHE.clear()
