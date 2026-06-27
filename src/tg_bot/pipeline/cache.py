"""Same-day result cache — skip the LLM run when an identical analysis
just happened.

Key: `(AnalysisConfigKey, ticker, date_iso)` — the dataclass carries
the (provider, deep_think_llm, quick_think_llm, max_debate_rounds,
effort) quintuple that defines a graph identity, and the cache layout
mirrors that:

    <TG_BOT_DATA_DIR>/result_cache/<key.slug()>/<TICKER>/<date>.json

The pool key in `analysis.py` uses the same `AnalysisConfigKey`
instance as its dict key, so by invariant #1 cache and pool are guaranteed
to share the same identity definition — adding a new config field
extends both surfaces in one place.

Each file carries `{final_state: dict, signal: str, telegraph_url:
str | None, generated_at: str}`. `generated_at` is an ISO UTC timestamp
the formatter renders on cache-hit responses so users see when the
cached decision was made (not the moment they tapped). The whole
`final_state` is persisted (not a slim shape) so future renderer changes
don't require re-running every cached ticker; LangChain message objects
are coerced via `_json_default`.

Filesystem-backed so the cache survives bot restarts; atomic writes
(tempfile + fsync + rename, same pattern as `JsonStorage._save`) guarantee
readers never see a partial file.

Eviction: lazy. Stale-date files for the same (config, ticker) are dropped
the next time a fresh result lands in that ticker's directory.

**Cache-hygiene gate built in:** `store` no-ops when `telegraph_url` is
falsy. Caching a failed publish would trap the user (every subsequent
`/watch` faithfully replays "Instant View unavailable" until midnight
rollover). One wasted LLM run on the ~3% transient failure rate is
cheaper than locking the user out of the slot. See PR #30 / INTU
2026-05-11 incident for the motivating wild observation.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import date as _date, datetime, UTC
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tg_bot.pipeline.config_key import AnalysisConfigKey


logger = logging.getLogger(__name__)


# `data` matches `Config`'s implicit default — kept in sync via the env var
# rather than importing Config here (would create a circular dependency
# with analysis.py once the cache is wired into the run path).
_DATA_DIR_ENV = "TG_BOT_DATA_DIR"
_CACHE_SUBDIR = "result_cache"


def _data_dir() -> Path:
    return Path(os.environ.get(_DATA_DIR_ENV, "data"))


def _now_iso() -> str:
    """UTC ISO timestamp, no microseconds — small + sortable + tz-aware
    when round-tripped via `datetime.fromisoformat`."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def today_iso(tz: ZoneInfo | None = None) -> str:
    """Local-date ISO string used as the cache lookup key AND the
    tradingagents log filename (`full_states_log_<date>.json`). Returned
    from a helper so callers don't repeat `date.today().isoformat()` and
    so the convention is documented in one place.

    With no `tz`, returns the server's local date (which inside Docker is
    typically UTC). Manual `/watch` taps call this no-arg form so their
    cache key matches tradingagents' on-disk log filename convention —
    using a UTC stamp would 404 for late-night runs from negative-UTC
    zones (e.g. 23:00 PDT → 06:00 UTC next day).

    With `tz` set, returns the user-local date in that timezone. The
    digest fan-out passes `ZoneInfo(digest.tz)` here so a Tokyo user
    firing at 09:00 JST = 00:00 UTC (prior day in server-local) sees
    today's date threaded through the cache key + calendar gate +
    rendered header consistently. Without this, the architect review
    of PR #71 traced the Tokyo failure mode where holiday-boundary
    days produced wrong market-closed skips.
    """
    if tz is None:
        return _date.today().isoformat()
    return datetime.now(tz).date().isoformat()


def parse_generated_at(s: str | None) -> datetime | None:
    """Round-trip a stored ISO-8601 timestamp back to an aware datetime.
    Returns None for missing or unparseable values so callers can fall
    through to "now" defaults — pre-PR cache entries (no `generated_at`
    field) and corrupted strings both land here gracefully."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _json_default(obj: Any) -> Any:
    """Fallback encoder for objects json doesn't natively understand.

    `final_state` from tradingagents contains LangChain message objects
    (e.g. `HumanMessage`) sprinkled through the agent debate fields —
    these aren't JSON-serializable by default. Try the pydantic v2/v1
    dump methods, fall back to a content-bearing dict, and finally to a
    typed repr so a single weird object never sinks the whole write."""
    for attr in ("model_dump", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                pass
    if hasattr(obj, "content"):
        return {"__type__": type(obj).__name__, "content": obj.content}
    return {"__type__": type(obj).__name__, "repr": repr(obj)}


def _path_for(key: AnalysisConfigKey, ticker: str, date_iso: str) -> Path:
    return _data_dir() / _CACHE_SUBDIR / key.slug() / ticker / f"{date_iso}.json"


def lookup(key: AnalysisConfigKey, ticker: str, date_iso: str) -> dict[str, Any] | None:
    """Return the cached `{final_state, signal, telegraph_url, generated_at}`
    dict, or `None` on miss. Quietly returns `None` on any read error — a
    corrupt or partially-written file should never block a fresh run.

    Self-healing on `JSONDecodeError`: the corrupt file is renamed aside
    to `<date>.json.corrupt-<unix_ts>` so subsequent same-day taps stop
    burning LLM credits in a loop trying to read it. Mirrors the same
    two-tier recovery pattern `JsonStorage._load` uses for the bot's
    persistent state files. Without this, a corrupt entry stuck around
    until the next-day `store` swept it — every tap in between forced a
    fresh LLM run. Other exception classes (OSError on read permission
    issues, etc.) log + return None but do NOT rename — the file might
    still be valid and recoverable on retry.

    Poisoned entries (`telegraph_url=None`) are returned as-is. The cache
    module stays out of the rendering policy; callers that care about the
    missing URL surface `⚠️ Instant View unavailable` and `/refresh` is
    the recovery path. The write side prevents *new* poisoned entries
    via `store`'s hygiene gate."""
    path = _path_for(key, ticker, date_iso)
    if not path.is_file():
        return None
    try:
        with path.open("r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        # Self-heal: rename the corrupt file aside so the next same-day
        # tap doesn't fall into this same branch and force a fresh LLM
        # run. The renamed sibling is preserved for post-mortem — never
        # auto-cleaned (operators sweep `data/result_cache/**/*.corrupt-*`
        # manually, same convention as the storage layer's backups).
        backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            os.rename(path, backup)
            logger.warning(
                "result_cache: corrupt JSON at %s (%s) — preserved as %s; "
                "next tap will recompute",
                path,
                e,
                backup,
            )
        except OSError as rename_err:
            logger.error(
                "result_cache: corrupt JSON at %s (%s) and rename to backup "
                "failed (%s); subsequent same-day taps will keep hitting "
                "this branch until the next-day store sweeps it",
                path,
                e,
                rename_err,
            )
        return None
    except Exception as e:
        logger.warning("result_cache: failed to read %s: %s", path, e)
        return None


def store(
    key: AnalysisConfigKey,
    ticker: str,
    date_iso: str,
    final_state: dict,
    signal: str,
    telegraph_url: str | None,
) -> None:
    """Write the cache entry atomically iff the Telegraph publish succeeded.

    **Cache-hygiene gate is built in.** A falsy `telegraph_url` (None or
    empty string) means the publish failed — we skip the store entirely
    so the user can retry on the next tap rather than being locked into
    "Instant View unavailable" until midnight. The cost is one wasted
    LLM run on the ~3% transient-failure rate; the benefit is that no
    cache entry is ever born poisoned.

    Best-effort overall — write failures are logged but don't propagate,
    since the live analysis flow is about to render the result regardless.

    Side effect on successful write: stale-date siblings for the same
    `(key, ticker)` are swept (lazy eviction). The sweep runs AFTER
    `os.replace` succeeds so a mid-write crash never orphans yesterday's
    still-valid entry.
    """
    if not telegraph_url:
        logger.info(
            "result_cache: skipping store for %s/%s/%s — falsy telegraph_url "
            "(publish failed; next tap will re-run + retry publish)",
            key.slug(),
            ticker,
            date_iso,
        )
        return

    path = _path_for(key, ticker, date_iso)
    # `tmp_path` may be referenced from the rescue branch on a tempfile
    # constructor failure (e.g., disk full mid-NamedTemporaryFile call).
    # Initialize before the try so the rescue's `os.unlink(tmp_path)`
    # doesn't UnboundLocalError-mask the real exception.
    tmp_path: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "final_state": final_state,
            "signal": signal,
            "telegraph_url": telegraph_url,
            "generated_at": _now_iso(),
        }
        # Atomic write: tempfile in the same dir → fsync → os.replace.
        # Same pattern as JsonStorage._save so readers never see partial
        # JSON even if the bot is killed mid-write.
        tmp_fd = tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, delete=False, suffix=".tmp"
        )
        tmp_path = tmp_fd.name
        try:
            try:
                json.dump(payload, tmp_fd, default=_json_default)
                tmp_fd.flush()
                os.fsync(tmp_fd.fileno())
            finally:
                tmp_fd.close()
            os.replace(tmp_path, path)
        except Exception:
            # Drop the partial tempfile so the dir doesn't accumulate
            # orphan .tmp leftovers across repeated failures.
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
        # Lazy eviction runs AFTER the atomic write succeeds — earlier
        # code swept stale siblings before the write, so a mid-write
        # failure would orphan yesterday's still-valid entry. The sweep
        # is best-effort: an unlink error here doesn't roll back the
        # successful write (the new entry is already on disk).
        #
        # Drop ONLY siblings strictly older than the just-written date. A
        # blanket "unlink every other name" sweep mis-fires whenever a
        # NEWER sibling legitimately coexists: (1) two runs straddling
        # midnight finish out of order — an older frozen-date store landing
        # last would delete the newer file; (2) manual /watch keys on the
        # server-local date while a digest keys on the user-tz date, so the
        # same wall-day can produce two stems that would otherwise evict
        # each other every run (breaking the one-LLM-run-per-day contract
        # for non-UTC-aligned users). Unparseable stems are left untouched
        # — never delete a file we can't date.
        try:
            written_date = _date.fromisoformat(date_iso)
        except ValueError:
            written_date = None
        if written_date is not None:
            for sibling in path.parent.glob("*.json"):
                if sibling.name == path.name:
                    continue
                try:
                    sibling_date = _date.fromisoformat(sibling.stem)
                except ValueError:
                    continue  # not a <date>.json file — leave it alone
                if sibling_date >= written_date:
                    continue  # same wall-day (other tz) or newer — keep
                try:
                    sibling.unlink()
                except OSError:
                    pass
    except Exception as e:
        logger.warning("result_cache: failed to write %s: %s", path, e)
