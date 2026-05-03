"""
Telegram Bot for TradingAgents - Watchlist Management.
Main entry point for the bot application.
"""
import sys
import logging
import os
from pathlib import Path


def setup_tradingagents_path() -> bool:
    """
    Setup TradingAgents path in sys.path.

    Priority order:
    1. TRADINGAGENTS_PATH env var
    2. ../TradingAgents (sibling directory)
    3. Assume installed as package

    Returns:
        True if TradingAgents was found in sys.path
    """
    # 1. Environment variable
    env_path = os.getenv("TRADINGAGENTS_PATH")
    if env_path and Path(env_path).exists():
        sys.path.insert(0, env_path)
        logging.info(f"Using TRADINGAGENTS_PATH: {env_path}")
        return True

    # 2. Sibling directory (../TradingAgents)
    current_dir = Path(__file__).parent
    # TradingAgents is at ../TradingAgents
    sibling_tradingagents_path = current_dir.parent / "TradingAgents"
    if sibling_tradingagents_path.exists():
        # Check if tradingagents module exists inside
        if (sibling_tradingagents_path / "tradingagents").exists():
            # Add sibling TradingAgents directory to path
            sys.path.insert(0, str(sibling_tradingagents_path))
            logging.info(f"Using sibling TradingAgents: {sibling_tradingagents_path}")
            return True

    # 3. Assume installed as package
    logging.info("Assuming TradingAgents is installed as a package")
    return True


# Setup TradingAgents path
setup_tradingagents_path()

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    TypeHandler,
)


BOT_COMMANDS = [
    BotCommand("start", "Welcome message"),
    BotCommand("help", "Show available commands"),
    BotCommand("add", "Add a ticker to your watchlist"),
    BotCommand("del", "Remove a ticker from your watchlist"),
    BotCommand("watch", "Show your watchlist"),
    BotCommand("list", "Show your watchlist (alias)"),
    BotCommand("config", "Configure LLM provider and models"),
]


async def post_init(application: Application) -> None:
    """Populate the Telegram client's Menu button with our command list."""
    await application.bot.set_my_commands(BOT_COMMANDS)

from config import Config
from handlers import (
    start,
    help_cmd,
    add_ticker,
    del_ticker,
    list_watchlist,
    config_cmd,
    button_callback,
)


async def authorize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Block updates from users not in ALLOWED_USER_IDS.

    Runs at group=-1 so it intercepts before any command/callback handler.
    """
    user = update.effective_user
    if user is None:
        return
    if Config.is_authorized(user.id):
        return

    logger.warning("Unauthorized access attempt from user_id=%s", user.id)
    if update.callback_query is not None:
        await update.callback_query.answer("Not authorized.", show_alert=True)
    elif update.effective_message is not None:
        await update.effective_message.reply_text(
            f"Not authorized. Your user ID is `{user.id}`.",
            parse_mode="Markdown",
        )
    raise ApplicationHandlerStop

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


def main():
    """Start the bot."""
    if not Config.validate():
        logger.error("TELEGRAM_BOT_TOKEN not found in environment variables!")
        logger.info("Please set TELEGRAM_BOT_TOKEN environment variable.")
        return

    application = (
        Application.builder()
        .token(Config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Auth gate: runs before any other handler.
    application.add_handler(TypeHandler(Update, authorize), group=-1)
    if Config.ALLOWED_USER_IDS:
        logger.info("Auth enabled — ALLOWED_USER_IDS=%s", Config.ALLOWED_USER_IDS)
    else:
        logger.info("Auth disabled — ALLOWED_USER_IDS empty, all users allowed")

    # Register command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("add", add_ticker))
    application.add_handler(CommandHandler("del", del_ticker))
    application.add_handler(CommandHandler("watch", list_watchlist))
    application.add_handler(CommandHandler("list", list_watchlist))
    application.add_handler(CommandHandler("config", config_cmd))

    # Register callback query handler for buttons
    application.add_handler(CallbackQueryHandler(button_callback))

    logger.info("Starting bot...")
    application.run_polling()


if __name__ == "__main__":
    main()
