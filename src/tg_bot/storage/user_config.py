"""Per-user LLM configuration: provider + deep/quick model."""

from typing import Optional

from ._base import JsonStorage


class UserConfigStorage(JsonStorage):
    """Per-user LLM provider and model selections, keyed by user_id."""

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
    MODEL_KEYS = {"deep": "deep_think_llm", "quick": "quick_think_llm"}

    async def set_llm_provider(self, user_id: str, provider: str) -> bool:
        """Set provider for a user; clears any stored deep/quick models since
        they are provider-specific and don't carry across providers."""
        provider = provider.strip().lower()
        if provider not in self.VALID_PROVIDERS:
            return False
        user_id = str(user_id)
        bucket = self._data.setdefault(user_id, {})
        bucket["llm_provider"] = provider
        for key in self.MODEL_KEYS.values():
            bucket.pop(key, None)
        await self._save_async()
        return True

    def get_llm_provider(self, user_id: str) -> Optional[str]:
        return self._data.get(str(user_id), {}).get("llm_provider")

    async def set_llm_model(self, user_id: str, mode: str, model: str) -> bool:
        """mode must be 'deep' or 'quick'."""
        key = self.MODEL_KEYS.get(mode)
        if key is None:
            return False
        user_id = str(user_id)
        self._data.setdefault(user_id, {})[key] = model
        await self._save_async()
        return True

    def get_llm_model(self, user_id: str, mode: str) -> Optional[str]:
        key = self.MODEL_KEYS.get(mode)
        if key is None:
            return None
        return self._data.get(str(user_id), {}).get(key)

    def get_config(self, user_id: str) -> dict[str, str]:
        return self._data.get(str(user_id), {})
