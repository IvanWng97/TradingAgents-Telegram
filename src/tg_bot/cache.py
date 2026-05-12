"""Same-day result cache — skip the LLM run when an identical analysis
just happened.

Key: (provider, deep_think_llm, quick_think_llm, ticker, date_iso, rounds,
effort) — `rounds` and `effort` are graph-baked and would change output,
so they belong in the key alongside the model triple. The pool key in
analysis.py shares the 5-element config quintuple (provider, deep, quick,
rounds, effort) so two users on different config knobs get isolated cache
entries AND isolated graph instances. The LLM output for a given
config+ticker+date is identical regardless of which user triggered the
run, so a hit saves the bill-payer's tokens AND lets a manual /watch
followed by the daily digest cost only one actual run.

Layout: `<TG_BOT_DATA_DIR>/result_cache/<config_slug>/<TICKER>/<date>.json`.
Slug shape: `<provider>__<deep>__<quick>` for default users; `__r{n}`
appended only when rounds≠1, `__e{level}` only when effort is set, so
default-config users keep their existing cache slot when a customized
run lands.

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
the next time a fresh result lands in that ticker's directory. Disk is
cheap; we don't run a nightly sweep job — a watchlist with N tickers and
M users on K configs leaves at most N*K files lingering until the next
trigger hits each ticker.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """UTC ISO timestamp, no microseconds — small + sortable + tz-aware
    when round-tripped via `datetime.fromisoformat`."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def cache_key_extras(config: dict) -> dict:
    """Pull the cache-key-affecting bits out of a resolved tradingagents
    config dict — `rounds` and `effort` are the only knobs beyond
    (provider, deep, quick) that change output, so they're the only ones
    the cache needs to disambiguate. Returns a kwargs dict ready to splat
    into `lookup` / `store` / `invalidate`."""
    # Late import: analysis.py imports from cache (transitively via
    # callbacks), so importing analysis at module load creates a cycle.
    # The dict is module-level on analysis so the lookup is O(1) once
    # imported, and the import itself is cached after first call.
    from tg_bot.analysis import EFFORT_KEY_BY_PROVIDER

    rounds = config.get("max_debate_rounds", 1)
    effort = next(
        (config[k] for k in EFFORT_KEY_BY_PROVIDER.values() if config.get(k)),
        None,
    )
    return {"rounds": rounds, "effort": effort}


def parse_generated_at(s: Optional[str]) -> Optional[datetime]:
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


# `data` matches `Config`'s implicit default — kept in sync via the env var
# rather than importing Config here (would create a circular dependency
# with analysis.py once the cache is wired into the run path).
_DATA_DIR_ENV = "TG_BOT_DATA_DIR"
_CACHE_SUBDIR = "result_cache"


def _data_dir() -> Path:
    return Path(os.environ.get(_DATA_DIR_ENV, "data"))


def _slug(
    provider: str,
    deep: str,
    quick: str,
    rounds: int = 1,
    effort: Optional[str] = None,
) -> str:
    """Cache slug — thin delegator so the caller's positional API stays
    unchanged. The actual shape lives in `AnalysisConfigKey.slug()` which
    is the single source of truth shared with the caption "via" line and
    the Telegraph title suffix (see `config_key.py`)."""
    from tg_bot.config_key import AnalysisConfigKey

    return AnalysisConfigKey(
        provider=provider, deep=deep, quick=quick, rounds=rounds, effort=effort
    ).slug()


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


def _path_for(
    provider: str,
    deep: str,
    quick: str,
    ticker: str,
    date_iso: str,
    rounds: int = 1,
    effort: Optional[str] = None,
) -> Path:
    return (
        _data_dir()
        / _CACHE_SUBDIR
        / _slug(provider, deep, quick, rounds, effort)
        / ticker
        / f"{date_iso}.json"
    )


def lookup(
    provider: str,
    deep: str,
    quick: str,
    ticker: str,
    date_iso: str,
    rounds: int = 1,
    effort: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return the cached `{final_state, signal, telegraph_url}` dict, or
    `None` on miss. Quietly returns `None` on any read error — a corrupt
    or partially-written file should never block a fresh run.

    `rounds` and `effort` extend the cache key so customized runs don't
    collide with default-config runs. Defaults match DEFAULT_CONFIG so
    callers that don't set them keep their existing cache slot.

    **Poisoned entries (`telegraph_url=None`) are returned as-is.** The
    caller is responsible for the policy: `callbacks._run_analysis_for_ticker`
    and `_analyze_one_for_digest` detect the poisoned state and re-publish
    from the cached `final_state` (no LLM run needed — the LLM bill was
    already paid; only the Telegraph publish needs to retry). Putting
    this policy in the cache module would conflate storage with
    rendering."""
    path = _path_for(provider, deep, quick, ticker, date_iso, rounds, effort)
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
    rounds: int = 1,
    effort: Optional[str] = None,
) -> None:
    """Write the entry atomically. Best-effort — write failures are
    logged but don't propagate, since the live analysis flow is about
    to render the result regardless."""
    path = _path_for(provider, deep, quick, ticker, date_iso, rounds, effort)
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
        # Stamp the moment of analysis so cache-hit renders show when the
        # decision was actually generated, not the time the user tapped
        # the button. Caller can override (e.g. for backfilled fixtures);
        # default to "now" so callers don't need to pass anything.
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
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.warning("result_cache: failed to write %s: %s", path, e)


def invalidate(
    provider: str,
    deep: str,
    quick: str,
    ticker: str,
    date_iso: str,
    rounds: int = 1,
    effort: Optional[str] = None,
) -> bool:
    """Remove the entry. Returns True if a file was removed.

    Used by `/refresh <TICKER>` so the next run pays the full LLM cost
    again — useful when intraday data has shifted enough that the user
    wants a fresh take, or when the previous run produced a bad result."""
    path = _path_for(provider, deep, quick, ticker, date_iso, rounds, effort)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.warning("result_cache: failed to remove %s: %s", path, e)
        return False
