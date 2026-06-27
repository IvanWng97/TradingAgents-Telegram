"""Auth gate: blocks updates from users not in ALLOWED_USER_IDS."""

import logging

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from tg_bot.config import Config


logger = logging.getLogger(__name__)


async def authorize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run before every command/callback. Raises ApplicationHandlerStop to
    short-circuit the dispatch chain when the user isn't allowed."""
    user = update.effective_user
    if user is None:
        # Updates without an effective_user (channel_post, my_chat_member,
        # some inline_query variants). When an allowlist is set, fail
        # closed — we have no way to attribute the request to a user.
        # When ALLOWED_USER_IDS is empty (open mode), pass through so
        # bot management updates still reach handlers.
        if Config.ALLOWED_USER_IDS:
            raise ApplicationHandlerStop
        return
    if Config.is_authorized(user.id):
        return

    logger.warning("Unauthorized access attempt from user_id=%s", user.id)
    # The rejection notification is best-effort and MUST NOT gate the stop.
    # If the notify await raises — `BadRequest: Query is too old` on a stale
    # inline button (deterministic), or a transient TimedOut/NetworkError/
    # Forbidden — an un-guarded `raise` below would never be reached, PTB's
    # process_error would return False (no error handler historically
    # registered), and the unauthorized update would fall through into the
    # command/callback groups. Swallow the notify failure so the gate ALWAYS
    # fails closed (fail-closed contract; see module docstring).
    try:
        if update.callback_query is not None:
            await update.callback_query.answer("Not authorized.", show_alert=True)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(
                f"Not authorized\\. Your user ID is `{user.id}`\\.",
                parse_mode="MarkdownV2",
            )
    except Exception:
        logger.debug(
            "auth: failed to notify unauthorized user_id=%s (gate still closes)",
            user.id,
            exc_info=True,
        )
    raise ApplicationHandlerStop
