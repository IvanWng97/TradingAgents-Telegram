"""Application wiring: handler registration and process entry point."""

import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from tg_bot.auth import authorize
from tg_bot.config import Config
from tg_bot.handlers import (
    add_ticker,
    add_via_reply,
    button_callback,
    config_cmd,
    del_ticker,
    help_cmd,
    list_watchlist,
    start,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


BOT_COMMANDS = [
    BotCommand("start", "Welcome message"),
    BotCommand("help", "Show available commands"),
    BotCommand("add", "Add a ticker to your watchlist"),
    BotCommand("del", "Remove a ticker from your watchlist"),
    BotCommand("watch", "Show your watchlist"),
    BotCommand("list", "Show your watchlist (alias)"),
    BotCommand("config", "Configure LLM provider and models"),
]


async def _post_init(application: Application) -> None:
    """Populate the Telegram client's Menu button + autocomplete."""
    await application.bot.set_my_commands(BOT_COMMANDS)


def _build_application() -> Application:
    application = (
        Application.builder()
        .token(Config.TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )

    # Auth gate runs at group=-1 so it intercepts every update before any
    # specific command/callback handler.
    application.add_handler(TypeHandler(Update, authorize), group=-1)
    if Config.ALLOWED_USER_IDS:
        logger.info("Auth enabled — ALLOWED_USER_IDS=%s", Config.ALLOWED_USER_IDS)
    else:
        logger.info("Auth disabled — ALLOWED_USER_IDS empty, all users allowed")

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("add", add_ticker))
    application.add_handler(CommandHandler("del", del_ticker))
    application.add_handler(CommandHandler("watch", list_watchlist))
    application.add_handler(CommandHandler("list", list_watchlist))
    application.add_handler(CommandHandler("config", config_cmd))
    application.add_handler(CallbackQueryHandler(button_callback))

    # Reply-driven /add: when the user replies to the bot's add-prompt,
    # parse the reply text as ticker(s). add_via_reply itself filters by
    # exact prompt-text match so other replies to the bot are ignored.
    application.add_handler(
        MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, add_via_reply)
    )

    return application


def main() -> None:
    if not Config.validate():
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables.")
        return
    application = _build_application()
    logger.info("Starting bot...")
    application.run_polling()
