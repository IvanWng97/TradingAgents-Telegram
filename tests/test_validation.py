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
