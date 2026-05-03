"""Process-wide storage singletons.

Handlers import `watchlist_storage` / `user_config_storage` from here so all
readers/writers share the same in-memory dict — there is exactly one instance
per process, regardless of how many handler modules import it.
"""

import os
from pathlib import Path

from .user_config import UserConfigStorage
from .watchlist import WatchlistStorage


DATA_DIR = Path(os.getenv("TG_BOT_DATA_DIR", "data"))

watchlist_storage = WatchlistStorage(DATA_DIR / "watchlist.json")
user_config_storage = UserConfigStorage(DATA_DIR / "user_config.json")

__all__ = [
    "DATA_DIR",
    "WatchlistStorage",
    "UserConfigStorage",
    "watchlist_storage",
    "user_config_storage",
]
