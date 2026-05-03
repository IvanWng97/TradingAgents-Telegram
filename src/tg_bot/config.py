"""Bot configuration sourced from environment variables."""

import os


def _parse_id_list(raw: str) -> list[int]:
    return [int(uid.strip()) for uid in raw.split(",") if uid.strip()]


def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ADMIN_USER_IDS = _parse_id_list(os.getenv("ADMIN_USER_IDS", ""))
    ALLOWED_USER_IDS = _parse_id_list(os.getenv("ALLOWED_USER_IDS", ""))
    # When True, TradingAgentsGraph runs in debug mode (streams every
    # langgraph chunk to stdout — fine for dev, noisy + slow in prod).
    TA_DEBUG = _parse_bool(os.getenv("TG_BOT_TA_DEBUG", ""))

    @classmethod
    def validate(cls) -> bool:
        return bool(cls.TELEGRAM_BOT_TOKEN)

    @classmethod
    def is_authorized(cls, user_id: int) -> bool:
        """Empty ALLOWED_USER_IDS means no restriction (open to everyone)."""
        if not cls.ALLOWED_USER_IDS:
            return True
        return user_id in cls.ALLOWED_USER_IDS
