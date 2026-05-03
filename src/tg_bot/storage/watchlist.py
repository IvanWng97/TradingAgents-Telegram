"""Per-user ticker watchlists."""

from typing import List

from ._base import JsonStorage


class WatchlistStorage(JsonStorage):
    """Watchlists keyed by Telegram user_id (stringified)."""

    def _load(self) -> dict[str, list[str]]:
        raw = super()._load()
        return {
            user_id: list(
                {t.upper() for t in tickers if isinstance(t, str) and t.strip()}
            )
            for user_id, tickers in raw.items()
        }

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

    def get_watchlist(self, user_id: str) -> List[str]:
        return self._data.get(str(user_id), []).copy()
