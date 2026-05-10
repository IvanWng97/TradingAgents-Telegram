"""Same-day result cache — skip the LLM run when an identical analysis
just happened.

Key: (provider, deep_think_llm, quick_think_llm, ticker, date_iso). The
LLM output for a given config+ticker+date is identical regardless of
which user triggered the run, so a hit saves the bill-payer's tokens
AND lets a manual /watch followed by the daily digest cost only one
actual run.

Layout: `<TG_BOT_DATA_DIR>/result_cache/<config_slug>/<TICKER>/<date>.json`.
Each file carries `{final_state: dict, signal: str, telegraph_url: str | None}`.
Filesystem-backed so the cache survives bot restarts; atomic writes
(tempfile + fsync + rename, same pattern as `JsonStorage._save`) guarantee
readers never see a partial file.

Eviction: lazy. Stale-date files for the same (config, ticker) are dropped
the next time a fresh result lands in that ticker's directory. Disk is
cheap; we don't run a nightly sweep job — a watchlist with N tickers and
M users on K configs leaves at most N*K files lingering until the next
trigger hits each ticker.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


# `data` matches `Config`'s implicit default — kept in sync via the env var
# rather than importing Config here (would create a circular dependency
# with analysis.py once the cache is wired into the run path).
_DATA_DIR_ENV = "TG_BOT_DATA_DIR"
_CACHE_SUBDIR = "result_cache"

# Provider / model strings can contain "/" or ":" (e.g. `openai/gpt-4o`,
# `meta:llama3`) — squash to a directory-safe slug. Slug form keeps the
# tree grep-able when debugging; a hash would be shorter but opaque.
_SLUG_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _data_dir() -> Path:
    return Path(os.environ.get(_DATA_DIR_ENV, "data"))


def _slug(provider: str, deep: str, quick: str) -> str:
    return _SLUG_SAFE.sub("_", f"{provider}__{deep}__{quick}")


def _path_for(provider: str, deep: str, quick: str, ticker: str, date_iso: str) -> Path:
    return (
        _data_dir()
        / _CACHE_SUBDIR
        / _slug(provider, deep, quick)
        / ticker
        / f"{date_iso}.json"
    )


def lookup(
    provider: str, deep: str, quick: str, ticker: str, date_iso: str
) -> Optional[dict[str, Any]]:
    """Return the cached `{final_state, signal, telegraph_url}` dict, or
    `None` on miss. Quietly returns `None` on any read error — a corrupt
    or partially-written file should never block a fresh run."""
    path = _path_for(provider, deep, quick, ticker, date_iso)
    if not path.is_file():
        return None
    try:
        with path.open("r") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("result_cache: failed to read %s: %s", path, e)
        return None


def store(
    provider: str,
    deep: str,
    quick: str,
    ticker: str,
    date_iso: str,
    final_state: dict,
    signal: str,
    telegraph_url: Optional[str],
) -> None:
    """Write the entry atomically. Best-effort — write failures are
    logged but don't propagate, since the live analysis flow is about
    to render the result regardless."""
    path = _path_for(provider, deep, quick, ticker, date_iso)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Lazy eviction: while we have the directory open, drop any
        # stale-date entries for this (config, ticker). Cheap (one
        # listdir) and bounds disk growth without a separate cron job.
        for sibling in path.parent.glob("*.json"):
            if sibling.name != path.name:
                try:
                    sibling.unlink()
                except OSError:
                    pass
        payload = {
            "final_state": final_state,
            "signal": signal,
            "telegraph_url": telegraph_url,
        }
        # Atomic write: tempfile in the same dir → fsync → os.replace.
        # Same pattern as JsonStorage._save so readers never see partial
        # JSON even if the bot is killed mid-write.
        tmp_fd = tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False, suffix=".tmp"
        )
        try:
            json.dump(payload, tmp_fd)
            tmp_fd.flush()
            os.fsync(tmp_fd.fileno())
            tmp_path = tmp_fd.name
        finally:
            tmp_fd.close()
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("result_cache: failed to write %s: %s", path, e)


def invalidate(
    provider: str, deep: str, quick: str, ticker: str, date_iso: str
) -> bool:
    """Remove the entry. Returns True if a file was removed.

    Used by `/refresh <TICKER>` so the next run pays the full LLM cost
    again — useful when intraday data has shifted enough that the user
    wants a fresh take, or when the previous run produced a bad result."""
    path = _path_for(provider, deep, quick, ticker, date_iso)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("result_cache: failed to remove %s: %s", path, e)
        return False
