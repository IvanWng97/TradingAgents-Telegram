"""Tests for `app.py` Application wiring.

Pins Invariant #2: `app.py` builds the PTB Application with
`.concurrent_updates(True)`. That flag is load-bearing for cancellation —
with the default single-worker dispatcher, a Cancel-button update queues
behind the in-flight analysis handler (awaiting `to_thread`) and only runs
after the analysis returns, exactly when it's useless. Nothing else in the
suite asserts the flag, so deleting it from `_build_application` would
otherwise leave the whole suite green.

PTB (22.x) exposes the setting as `Application.concurrent_updates`, an
`int` max-concurrent-updates count: `concurrent_updates(True)` resolves to
a large default (256) while the un-set default / `concurrent_updates(False)`
resolve to 1 (single worker). The invariant is therefore "more than one
update processed concurrently" → `> 1`.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from telegram.ext import ApplicationBuilder  # noqa: E402

from tg_bot import app  # noqa: E402
from tg_bot.config import Config  # noqa: E402


def test_build_application_enables_concurrent_updates():
    """The real `_build_application()` path must enable concurrent update
    processing. `Config.TELEGRAM_BOT_TOKEN` is read at import time, so patch
    the resolved class attribute (not just os.environ) with a dummy token —
    `.token("...")` / `.build()` do not hit the network.
    """
    saved = Config.TELEGRAM_BOT_TOKEN
    Config.TELEGRAM_BOT_TOKEN = "123:dummy-token-for-build-only"
    try:
        application = app._build_application()
    finally:
        Config.TELEGRAM_BOT_TOKEN = saved

    # > 1 == parallel dispatch. Drops to 1 if `.concurrent_updates(True)`
    # is removed from `_build_application` — which is exactly the regression
    # this test exists to catch.
    assert application.concurrent_updates > 1


def test_default_builder_is_single_worker_control():
    """Control: a builder WITHOUT `.concurrent_updates(True)` resolves to
    the single-worker default (1). This documents the failure mode the
    assertion above guards against — if `app.py` ever drops the flag, the
    real-path test falls to this value and goes red.
    """
    plain = ApplicationBuilder().token("123:dummy-token-for-build-only").build()
    assert plain.concurrent_updates == 1


def test_build_application_registers_error_handler():
    """H1 defense-in-depth: `_build_application` must register a top-level
    error handler. Without one, an unhandled exception in any handler makes
    PTB's `process_error` return False; for the group=-1 auth gate that is
    the fail-open path (the update continues into later groups). A registered
    handler guarantees unhandled exceptions are logged and never alter
    dispatch flow.
    """
    saved = Config.TELEGRAM_BOT_TOKEN
    Config.TELEGRAM_BOT_TOKEN = "123:dummy-token-for-build-only"
    try:
        application = app._build_application()
    finally:
        Config.TELEGRAM_BOT_TOKEN = saved

    # PTB stores registered error handlers in `application.error_handlers`
    # (a dict keyed by callback). A non-empty dict == at least one handler.
    assert application.error_handlers, (
        "no error handler registered — unhandled handler exceptions would "
        "silently fall through (H1 fail-open backstop missing)"
    )
    assert app._on_error in application.error_handlers


async def test_on_error_logs_the_exception(caplog):
    """`_on_error` surfaces the unhandled exception at ERROR level so it is
    never silently swallowed (the codebase otherwise has no structured error
    logging)."""
    import logging
    from types import SimpleNamespace

    context = SimpleNamespace(error=RuntimeError("boom in a handler"))
    with caplog.at_level(logging.ERROR, logger="tg_bot.app"):
        await app._on_error(update=object(), context=context)
    assert any(
        "boom in a handler" in r.getMessage() or r.exc_info for r in caplog.records
    )
