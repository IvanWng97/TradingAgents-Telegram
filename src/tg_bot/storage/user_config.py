"""Per-user LLM configuration: provider + deep/quick model + digest schedule."""

import logging
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ._base import JsonStorage


logger = logging.getLogger(__name__)


class UserConfigStorage(JsonStorage):
    """Per-user LLM provider, model selections, and daily-digest schedule, keyed by user_id."""

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
    LLM_KEYS = frozenset({"llm_provider", *MODEL_KEYS.values()})
    DIGEST_KEY = "digest"

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

    async def clear(self, user_id: str) -> bool:
        """Remove LLM keys for a user; preserves non-LLM blocks (e.g., digest)
        so a /config Cancel rollback doesn't wipe an unrelated digest schedule.
        Drops the user entry entirely if nothing else is left in it."""
        user_id = str(user_id)
        bucket = self._data.get(user_id)
        if not bucket:
            return False
        removed = False
        for key in list(bucket.keys()):
            if key in self.LLM_KEYS:
                bucket.pop(key)
                removed = True
        if not bucket:
            del self._data[user_id]
        if removed:
            await self._save_async()
        return removed

    # ─── digest schedule ────────────────────────────────────────────────

    @staticmethod
    def _empty_digest() -> dict[str, Any]:
        return {"enabled": False, "hour_local": None, "tz": None, "chat_id": None}

    def get_digest(self, user_id: str) -> Optional[dict[str, Any]]:
        """Return the digest block (or None if never set). Read-only — copy
        before mutating."""
        return self._data.get(str(user_id), {}).get(self.DIGEST_KEY)

    async def set_digest_hour(self, user_id: str, hour: int, chat_id: int) -> bool:
        """Set hour (0-23), enable the digest, and capture chat_id.

        The job callback later fires unsolicited messages, so it needs a
        chat_id to send to — we can only learn that from a live Update.
        Hour selection is the natural moment to capture it.
        """
        if not (0 <= hour <= 23):
            return False
        user_id = str(user_id)
        bucket = self._data.setdefault(user_id, {})
        digest = bucket.setdefault(self.DIGEST_KEY, self._empty_digest())
        digest["enabled"] = True
        digest["hour_local"] = int(hour)
        digest["chat_id"] = int(chat_id)
        await self._save_async()
        return True

    async def set_digest_tz(self, user_id: str, tz: str) -> bool:
        """Set the IANA tz name for the user's digest schedule.

        Validated against `zoneinfo` so an invalid/typo'd tz never reaches
        the JobQueue. Doesn't touch `enabled` — a tz change on an active
        digest keeps it active; on a first-time setup the digest stays
        disabled until the user picks an hour.
        """
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as e:
            logger.warning("set_digest_tz: invalid tz %r (%s)", tz, e)
            return False
        user_id = str(user_id)
        bucket = self._data.setdefault(user_id, {})
        digest = bucket.setdefault(self.DIGEST_KEY, self._empty_digest())
        digest["tz"] = tz
        await self._save_async()
        return True

    async def disable_digest(self, user_id: str) -> bool:
        """Disable the digest; preserves hour + tz for one-tap re-enable."""
        digest = self._data.get(str(user_id), {}).get(self.DIGEST_KEY)
        if digest is None:
            return False
        digest["enabled"] = False
        await self._save_async()
        return True

    def iter_enabled_digests(self) -> list[tuple[str, dict[str, Any]]]:
        """List of (user_id, digest_block) for users with a fully-configured,
        enabled digest. Used by `_post_init` to re-register JobQueue jobs.

        Skips entries missing any of: enabled flag, hour_local, tz, chat_id —
        these are partial states from an in-progress picker session.
        """
        result: list[tuple[str, dict[str, Any]]] = []
        for user_id, bucket in self._data.items():
            digest = bucket.get(self.DIGEST_KEY)
            if not digest or not digest.get("enabled"):
                continue
            if (
                digest.get("hour_local") is None
                or not digest.get("tz")
                or not digest.get("chat_id")
            ):
                continue
            result.append((user_id, digest))
        return result
