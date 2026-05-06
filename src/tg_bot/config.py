"""Bot configuration sourced from environment variables."""

import os


def _parse_id_list(raw: str) -> list[int]:
    return [int(uid.strip()) for uid in raw.split(",") if uid.strip()]


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(raw: str, default: int) -> int:
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return default


class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ALLOWED_USER_IDS = _parse_id_list(os.getenv("ALLOWED_USER_IDS", ""))
    # When True, TradingAgentsGraph runs in debug mode (streams every
    # langgraph chunk to stdout — fine for dev, noisy + slow in prod).
    TA_DEBUG = _parse_bool(os.getenv("TG_BOT_TA_DEBUG", ""))
    # Max concurrent analyses across the whole bot. Acts as a coroutine-
    # level FIFO queue (asyncio.Semaphore). Beyond this, runs sit in a
    # "queued" state with their cancel button — cancellable while waiting.
    # The graph pool is sized to match this so its blocking-queue branch
    # is unreachable and per-key memory is bounded by the same knob.
    # Higher = more parallelism, more memory (each graph pins ~50-200 MB
    # of LLM clients + ChromaDB). Lower = memory-friendly, bigger queues
    # serialize.
    MAX_CONCURRENT_ANALYSES = _parse_int(
        os.getenv("TG_BOT_MAX_CONCURRENT_ANALYSES", ""), 3
    )

    @classmethod
    def validate(cls) -> bool:
        return bool(cls.TELEGRAM_BOT_TOKEN)

    @classmethod
    def is_authorized(cls, user_id: int) -> bool:
        """Empty ALLOWED_USER_IDS means no restriction (open to everyone)."""
        if not cls.ALLOWED_USER_IDS:
            return True
        return user_id in cls.ALLOWED_USER_IDS
