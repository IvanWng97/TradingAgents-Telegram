"""Application wiring: handler registration and process entry point."""

import asyncio
import logging
import time

from telegram import BotCommand, Update
from telegram.ext import (
    AIORateLimiter,
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
    digest_cmd,
    help_cmd,
    history_cmd,
    list_watchlist,
    start,
    status_cmd,
)
from tg_bot.handlers.callbacks import register_digest_job
from tg_bot.storage import user_config_storage


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
    BotCommand("digest", "Schedule a daily summary of your watchlist"),
    BotCommand("history", "Look up a past analysis"),
    BotCommand("status", "Show bot uptime, pool, and your LLM config"),
]


async def _post_init(application: Application) -> None:
    """Populate the Telegram client's Menu button + autocomplete, stamp the
    process start time used by /status, and re-register every enabled
    user's daily digest with the in-memory JobQueue.

    Storage is the source of truth; JobQueue holds in-memory schedules.
    On every restart we walk `user_config_storage` and reconstruct the
    schedule — partial / disabled rows are filtered by `iter_enabled_digests`.
    """
    application.bot_data["start_time"] = time.time()
    await application.bot.set_my_commands(BOT_COMMANDS)
    enabled = user_config_storage.iter_enabled_digests()
    for user_id_str, digest in enabled:
        try:
            register_digest_job(application, int(user_id_str), digest)
        except Exception as e:
            logger.warning(
                "post_init: failed to register digest for user %s: %s",
                user_id_str,
                e,
            )
    if enabled:
        logger.info("digest: registered %d user(s) on startup", len(enabled))


async def _post_stop(application: Application) -> None:
    """On SIGTERM/SIGINT: signal every in-flight analysis to abort, then
    give them ~2s to render their "❌ Cancelled" caption before the
    process exits. Without this, `docker compose up -d --build` rollouts
    mid-run can leave half-written JSON and stranded "Analyzing…" captions
    that no longer have a backing process.
    """
    cancelled = 0
    try:
        for _chat_id, cd in application.chat_data.items():
            registry = cd.get("analysis_cancels") or {}
            for entry in registry.values():
                entry["event"].set()
                async_event = entry.get("async_event")
                if async_event is not None:
                    async_event.set()
                cancelled += 1
    except Exception as e:
        logger.warning("post_stop: failed to iterate chat_data: %s", e)
    if cancelled:
        logger.info(
            "Graceful shutdown: signalled %d in-flight analyses; draining 2s",
            cancelled,
        )
        await asyncio.sleep(2.0)


def _build_application() -> Application:
    # concurrent_updates=True is load-bearing for cancellation. Without it
    # PTB processes updates with a single worker, so a Cancel-button tap
    # sits in the queue until the in-flight analysis handler returns —
    # exactly when we no longer need it. With concurrent_updates the
    # cancel handler runs in parallel with the awaiting analysis task,
    # sets the cancel_event, and ProgressReporter aborts at the next step.
    application = (
        Application.builder()
        .token(Config.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        # AIORateLimiter throttles outgoing Telegram calls under both the
        # per-bot (~30/s) and per-chat (~1/s) limits. Without it, parallel
        # send_photo / edit_message_caption bursts see RetryAfter exceptions
        # that PTB doesn't auto-retry — analyses get silently dropped.
        .rate_limiter(AIORateLimiter())
        # Default HTTP timeouts in PTB are ~5s; sendPhoto with a finviz URL
        # routinely exceeds that under burst load (Telegram fetches the
        # photo server-side). Without this, parallel queue launches see
        # multiple `TimedOut` errors and silently drop tickers.
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(15)
        .pool_timeout(30)
        .post_init(_post_init)
        .post_stop(_post_stop)
        .build()
    )

    # Auth gate runs at group=-1 so it intercepts every update before any
    # specific command/callback handler.
    application.add_handler(TypeHandler(Update, authorize), group=-1)
    if Config.ALLOWED_USER_IDS:
        logger.info("Auth enabled — ALLOWED_USER_IDS=%s", Config.ALLOWED_USER_IDS)
    else:
        logger.warning(
            "Auth disabled — ALLOWED_USER_IDS empty, the bot is open to anyone "
            "who finds it. Set ALLOWED_USER_IDS in .env to restrict access "
            "(your LLM tokens are at risk otherwise)."
        )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("add", add_ticker))
    application.add_handler(CommandHandler("del", del_ticker))
    application.add_handler(CommandHandler("watch", list_watchlist))
    application.add_handler(CommandHandler("list", list_watchlist))
    application.add_handler(CommandHandler("config", config_cmd))
    application.add_handler(CommandHandler("digest", digest_cmd))
    application.add_handler(CommandHandler("history", history_cmd))
    application.add_handler(CommandHandler("status", status_cmd))
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
