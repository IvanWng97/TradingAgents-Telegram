"""
Data storage module for watchlists and user configs.
Stores user watchlists and configurations in JSON files.
"""
import json
from pathlib import Path
from typing import Dict, List, Optional

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
USER_CONFIG_FILE = DATA_DIR / "user_config.json"


class WatchlistStorage:
    """Storage for user watchlists."""

    def __init__(self, file_path: Path = WATCHLIST_FILE):
        self.file_path = file_path
        self._data: Dict[str, List[str]] = self._load()

    def _load(self) -> Dict[str, List[str]]:
        """Load watchlist from file."""
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    user_id: list(set(t.upper() for t in tickers if t.strip()))
                    for user_id, tickers in data.items()
                }
        except (json.JSONDecodeError, IOError):
            return {}

    def _save(self):
        """Save watchlist to file."""
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    def add_ticker(self, user_id: str, ticker: str) -> bool:
        """Add a ticker to user's watchlist. Returns True if added, False if already exists."""
        user_id = str(user_id)
        ticker = ticker.strip().upper()

        if not ticker:
            return False

        if user_id not in self._data:
            self._data[user_id] = []

        if ticker in self._data[user_id]:
            return False

        self._data[user_id].append(ticker)
        self._save()
        return True

    def remove_ticker(self, user_id: str, ticker: str) -> bool:
        """Remove a ticker from user's watchlist. Returns True if removed, False if not found."""
        user_id = str(user_id)
        ticker = ticker.strip().upper()

        if user_id not in self._data or ticker not in self._data[user_id]:
            return False

        self._data[user_id].remove(ticker)
        if not self._data[user_id]:
            del self._data[user_id]
        self._save()
        return True

    def get_watchlist(self, user_id: str) -> List[str]:
        """Get user's watchlist."""
        user_id = str(user_id)
        return self._data.get(user_id, []).copy()


class UserConfigStorage:
    """Storage for user configurations."""

    VALID_PROVIDERS = [
        "openai",
        "google",
        "anthropic",
        "xai",
        "deepseek",
        "qwen",
        "glm",
        "openrouter",
        "ollama",
        "azure",
    ]

    def __init__(self, file_path: Path = USER_CONFIG_FILE):
        self.file_path = file_path
        self._data: Dict[str, Dict[str, str]] = self._load()

    def _load(self) -> Dict[str, Dict[str, str]]:
        """Load user configs from file."""
        if not self.file_path.exists():
            return {}
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}

    def _save(self):
        """Save user configs to file."""
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

    MODEL_KEYS = {"deep": "deep_think_llm", "quick": "quick_think_llm"}

    def set_llm_provider(self, user_id: str, provider: str) -> bool:
        """Set LLM provider for a user. Returns True if set successfully.

        Clears any previously stored deep/quick model selections, since they
        are provider-specific and cannot be carried across providers.
        """
        user_id = str(user_id)
        provider = provider.strip().lower()

        if provider not in self.VALID_PROVIDERS:
            return False

        if user_id not in self._data:
            self._data[user_id] = {}

        self._data[user_id]["llm_provider"] = provider
        for key in self.MODEL_KEYS.values():
            self._data[user_id].pop(key, None)
        self._save()
        return True

    def get_llm_provider(self, user_id: str) -> Optional[str]:
        """Get LLM provider for a user."""
        user_id = str(user_id)
        return self._data.get(user_id, {}).get("llm_provider")

    def set_llm_model(self, user_id: str, mode: str, model: str) -> bool:
        """Store a deep/quick model selection. mode must be 'deep' or 'quick'."""
        key = self.MODEL_KEYS.get(mode)
        if key is None:
            return False
        user_id = str(user_id)
        if user_id not in self._data:
            self._data[user_id] = {}
        self._data[user_id][key] = model
        self._save()
        return True

    def get_llm_model(self, user_id: str, mode: str) -> Optional[str]:
        """Get the user's stored deep/quick model, or None if unset."""
        key = self.MODEL_KEYS.get(mode)
        if key is None:
            return None
        user_id = str(user_id)
        return self._data.get(user_id, {}).get(key)

    def get_config(self, user_id: str) -> Dict[str, str]:
        """Get all config for a user."""
        user_id = str(user_id)
        return self._data.get(user_id, {})
