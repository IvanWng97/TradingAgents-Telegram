"""Read historical TradingAgents analyses from disk.

TradingAgents persists each propagate() run as JSON at:
    <results_dir>/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<YYYY-MM-DD>.json

`results_dir` defaults to `~/.tradingagents/logs` and is configurable via
`TRADINGAGENTS_RESULTS_DIR`. In Docker you'll want to bind-mount that
directory or set `TRADINGAGENTS_RESULTS_DIR=/app/data/ta-logs` so history
survives container restarts.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional


logger = logging.getLogger(__name__)


# Ticker validator shared with `validation.TICKER_RE` — single source of
# truth so the storage-write check and the disk-read check can never
# disagree on what counts as a valid ticker. Divergence would silently
# re-open the path-traversal hole the regex closes (see `validation.py`
# comment for the `..`/`.A` failure mode the earlier `[A-Z0-9.\-]+` form
# allowed).
from tg_bot.validation import TICKER_RE  # noqa: E402

_DATE_FILENAME_RE = re.compile(r"^full_states_log_(\d{4}-\d{2}-\d{2})\.json$")


def _results_dir() -> Optional[Path]:
    """Return the configured tradingagents `results_dir`, or None if the
    package isn't installed."""
    try:
        from tradingagents.default_config import DEFAULT_CONFIG

        return Path(DEFAULT_CONFIG["results_dir"])
    except Exception:
        return None


def _ticker_dir(ticker: str) -> Optional[Path]:
    base = _results_dir()
    if base is None:
        return None
    return base / ticker / "TradingAgentsStrategy_logs"


def normalize_ticker(raw: str) -> Optional[str]:
    """Uppercase + validate. Returns None for unsafe input (path traversal)."""
    t = raw.strip().upper()
    return t if TICKER_RE.match(t) else None


def list_available_tickers(limit: int = 20) -> list[str]:
    """Return tickers with at least one saved analysis under `results_dir`,
    sorted alphabetically. Capped at `limit` to keep the keyboard reasonable."""
    base = _results_dir()
    if base is None or not base.exists():
        return []

    found: list[str] = []
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        ticker = entry.name.upper()
        if normalize_ticker(ticker) is None:
            continue
        log_dir = entry / "TradingAgentsStrategy_logs"
        if not log_dir.exists():
            continue
        # Cheap probe: any file matching the pattern is enough.
        if any(
            _DATE_FILENAME_RE.match(p.name) for p in log_dir.iterdir() if p.is_file()
        ):
            found.append(ticker)
    found.sort()
    return found[:limit]


def list_available_dates(ticker: str, limit: int = 10) -> list[date]:
    """Return the most-recent `limit` dates with saved analyses for `ticker`."""
    safe = normalize_ticker(ticker)
    if not safe:
        return []
    directory = _ticker_dir(safe)
    if directory is None or not directory.exists():
        return []

    dates: list[date] = []
    for path in directory.iterdir():
        m = _DATE_FILENAME_RE.match(path.name)
        if not m:
            continue
        try:
            dates.append(date.fromisoformat(m.group(1)))
        except ValueError:
            continue
    dates.sort(reverse=True)
    return dates[:limit]


def load_historical_state(ticker: str, date_str: str) -> Optional[dict]:
    """Return the saved final_state dict for `ticker` on `date_str`, or None.

    Normalizes the on-disk log's `trader_investment_decision` key back to the
    live `trader_investment_plan` key so callers can reuse the formatters that
    expect live-state shape.
    """
    safe = normalize_ticker(ticker)
    if not safe:
        return None
    directory = _ticker_dir(safe)
    if directory is None:
        return None

    # Reject anything that doesn't parse as a valid ISO date — prevents
    # `../etc/passwd` style filenames from being constructed.
    try:
        date.fromisoformat(date_str)
    except ValueError:
        return None

    path = directory / f"full_states_log_{date_str}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read history %s: %s", path, e)
        return None

    # The log uses a slightly different key for the trader plan than live state.
    if "trader_investment_decision" in raw and "trader_investment_plan" not in raw:
        raw["trader_investment_plan"] = raw["trader_investment_decision"]
    return raw
