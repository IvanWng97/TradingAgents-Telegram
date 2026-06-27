"""Per-user ticker watchlists."""

import logging

from ._base import JsonStorage


logger = logging.getLogger(__name__)


class WatchlistStorage(JsonStorage):
    """Watchlists keyed by Telegram user_id (stringified)."""

    def _load(self) -> dict[str, list[str]]:
        # Each user's list is dedup'd (case-folded) and sorted so every
        # consumer — /watch picker, /digest ticker filter, /history,
        # /del — sees a stable alphabetical order. Sorting here also
        # repairs older saves that landed in insertion / arbitrary order.
        raw = super()._load()
        cleaned: dict[str, list[str]] = {}
        for user_id, tickers in raw.items():
            # `JsonStorage._load` only recovers from JSONDecodeError, so a
            # structurally-valid file with a wrong value type slips through
            # to here. A non-list value (int/null → TypeError on iteration;
            # a bare string → silently char-split into single-letter
            # "tickers") would crash at module-import time (this runs in the
            # `watchlist_storage = WatchlistStorage(...)` singleton build) or
            # durably corrupt the watchlist. Skip + warn so a hand-edited /
            # externally-corrupted file degrades gracefully (L4); mirrors the
            # loud guard `set_digest_tickers` already has for this footgun.
            if not isinstance(tickers, list):
                logger.warning(
                    "watchlist: user %s has non-list value (%s) — skipping row",
                    user_id,
                    type(tickers).__name__,
                )
                continue
            cleaned[user_id] = sorted(
                {t.upper() for t in tickers if isinstance(t, str) and t.strip()}
            )
        return cleaned

    async def add_ticker(self, user_id: str, ticker: str) -> bool:
        """Returns True if added, False if already present or invalid."""
        user_id = str(user_id)
        ticker = ticker.strip().upper()
        if not ticker:
            return False
        bucket = self._data.setdefault(user_id, [])
        if ticker in bucket:
            return False
        bucket.append(ticker)
        bucket.sort()
        await self._save_async()
        return True

    async def remove_ticker(self, user_id: str, ticker: str) -> bool:
        """Returns True if removed, False if not found."""
        user_id = str(user_id)
        ticker = ticker.strip().upper()
        if user_id not in self._data or ticker not in self._data[user_id]:
            return False
        self._data[user_id].remove(ticker)
        if not self._data[user_id]:
            del self._data[user_id]
        await self._save_async()
        return True

    def get_watchlist(self, user_id: str) -> list[str]:
        return self._data.get(str(user_id), []).copy()
