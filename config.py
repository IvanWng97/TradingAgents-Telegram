"""
Configuration for Telegram Bot.
Load environment variables for Telegram bot token.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")


class Config:
    """Bot configuration."""

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ADMIN_USER_IDS = [
        int(uid.strip())
        for uid in os.getenv("ADMIN_USER_IDS", "").split(",")
        if uid.strip()
    ]
    ALLOWED_USER_IDS = [
        int(uid.strip())
        for uid in os.getenv("ALLOWED_USER_IDS", "").split(",")
        if uid.strip()
    ]

    @classmethod
    def validate(cls) -> bool:
        """Validate configuration."""
        return bool(cls.TELEGRAM_BOT_TOKEN)

    @classmethod
    def is_authorized(cls, user_id: int) -> bool:
        """Whether a Telegram user is allowed to use the bot.

        An empty ALLOWED_USER_IDS means no restriction — everyone is allowed.
        Populate the env var to lock the bot down to specific user IDs.
        """
        if not cls.ALLOWED_USER_IDS:
            return True
        return user_id in cls.ALLOWED_USER_IDS
