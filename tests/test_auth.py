"""Smoke tests for the auth gate (`tg_bot.auth.authorize`).

The gate runs at `group=-1` for every incoming Update — the widest
invocation surface in the bot. The architect review on PR #34
flagged it as the highest-value untested surface: a regression in
the fail-closed `effective_user is None` path would be silent in
logs and let bot-management updates through to handlers in a closed
deployment.

Four scenarios:
  - allowlist empty + any user → pass through (open mode)
  - allowlist set + user in list → pass through
  - allowlist set + user NOT in list → raise ApplicationHandlerStop
  - allowlist set + effective_user is None → raise (fail-closed)

Run with: pytest tests/test_auth.py
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Suppress the WARN "Unauthorized access attempt" log lines that the
# rejection scenarios deliberately trigger — they're expected, not output.
import logging  # noqa: E402

logging.getLogger("tg_bot.auth").setLevel(logging.ERROR)


def _make_update(user_id: int | None, kind: str = "message"):
    """Build a minimal Update stub that matches what `authorize` reads.

    `kind` is "message" (has `effective_message`) or "callback" (has
    `callback_query`). Both flavors are needed because the unauthorized
    branch routes the rejection message differently.
    """

    answered: list[tuple[str, bool]] = []
    replied: list[str] = []

    class _Query:
        async def answer(self, text: str = "", show_alert: bool = False) -> None:
            answered.append((text, show_alert))

    class _Message:
        async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
            replied.append(text)

    user = SimpleNamespace(id=user_id) if user_id is not None else None
    update = SimpleNamespace(
        effective_user=user,
        callback_query=_Query() if kind == "callback" else None,
        effective_message=_Message() if kind == "message" else None,
    )
    return update, answered, replied


def _reload_auth_with_allowlist(allowlist: str) -> None:
    """Re-import `tg_bot.auth` with the given allowlist env var so Config
    picks up the new value. authorize() reads Config.ALLOWED_USER_IDS
    once per call, so re-import isn't strictly required, but reloading
    Config keeps the test self-contained."""
    os.environ["ALLOWED_USER_IDS"] = allowlist
    # Force a Config reload so the class attribute reflects the new env.
    import importlib

    import tg_bot.config
    import tg_bot.auth  # noqa: F401 (imported for re-export side effect)

    importlib.reload(tg_bot.config)
    import tg_bot.auth as auth_mod

    importlib.reload(auth_mod)
    return auth_mod


# ─── Scenarios ──────────────────────────────────────────────────────


async def test_open_mode_passes_any_user() -> None:
    auth = _reload_auth_with_allowlist("")
    update, answered, replied = _make_update(user_id=12345)
    # No raise = passes through.
    await auth.authorize(update, context=None)
    assert answered == [], f"unexpected query answers in open mode: {answered}"
    assert replied == [], f"unexpected message replies in open mode: {replied}"


async def test_open_mode_passes_missing_user() -> None:
    """Channel post / my_chat_member updates have no `effective_user`.
    Open mode passes them through so bot management still works."""
    auth = _reload_auth_with_allowlist("")
    update, _, _ = _make_update(user_id=None)
    await auth.authorize(update, context=None)  # no raise


async def test_allowlist_passes_authorized_user() -> None:
    auth = _reload_auth_with_allowlist("42,99")
    update, answered, replied = _make_update(user_id=42)
    await auth.authorize(update, context=None)
    assert answered == [] and replied == []


async def test_allowlist_blocks_unauthorized_command() -> None:
    """Unauthorized via /command surface — should reply with rejection
    text and raise ApplicationHandlerStop so dispatch stops."""
    from telegram.ext import ApplicationHandlerStop

    auth = _reload_auth_with_allowlist("42,99")
    update, _, replied = _make_update(user_id=7, kind="message")
    try:
        await auth.authorize(update, context=None)
    except ApplicationHandlerStop:
        pass
    else:
        raise AssertionError("expected ApplicationHandlerStop on unauthorized user")
    assert len(replied) == 1, f"expected 1 rejection reply, got {len(replied)}"
    assert "Not authorized" in replied[0]
    assert "`7`" in replied[0], "rejection should echo the user_id"


async def test_allowlist_blocks_unauthorized_callback() -> None:
    """Unauthorized via callback_query — should answer the query with a
    toast (not reply to a message that may not exist)."""
    from telegram.ext import ApplicationHandlerStop

    auth = _reload_auth_with_allowlist("42,99")
    update, answered, _ = _make_update(user_id=7, kind="callback")
    try:
        await auth.authorize(update, context=None)
    except ApplicationHandlerStop:
        pass
    else:
        raise AssertionError("expected ApplicationHandlerStop on unauthorized user")
    assert len(answered) == 1, f"expected 1 callback answer, got {len(answered)}"
    text, show_alert = answered[0]
    assert "Not authorized" in text
    assert show_alert is True, "rejection toast should be show_alert=True"


async def test_allowlist_fail_closed_on_missing_user() -> None:
    """When ALLOWED_USER_IDS is non-empty, updates without an
    effective_user (channel posts, my_chat_member, certain inline
    queries) must be REJECTED. Passing them through in a closed
    deployment would let bot-management updates reach handlers without
    attribution. This is the silent-failure mode the architect flagged."""
    from telegram.ext import ApplicationHandlerStop

    auth = _reload_auth_with_allowlist("42")
    update, _, _ = _make_update(user_id=None)
    try:
        await auth.authorize(update, context=None)
    except ApplicationHandlerStop:
        pass
    else:
        raise AssertionError(
            "fail-closed broken: effective_user=None passed through "
            "with non-empty allowlist"
        )
