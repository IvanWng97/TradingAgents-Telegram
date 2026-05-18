"""Pytest config for the `tests/` suite.

Pytest discovers `tests/test_*.py` files via the default rule (no
`python_files` override needed; see `[tool.pytest.ini_options]` in
`pyproject.toml`). `async def test_*` / `def test_*` scenarios — run
`.venv/bin/python -m pytest tests/ --collect-only -q | tail -1` for the
live count.

Cross-cutting setup carried here:

1. `src/` on `sys.path` so `tg_bot.*` imports resolve. Each test file
   already does this at module top, but conftest fires earlier (during
   collection, before any test file is imported) so the precheck-disable
   fixture below can import `tg_bot.handlers.*` cleanly.

2. Digest precheck disable. `test_digest.py`'s fan-out / cancel /
   progress tests assume `check_llm_configured` is a no-op so they
   don't have to arm a provider + matching env var. We replicate the
   patch as an autouse session fixture here. Safe across all suites
   because `test_llm_precheck_*` tests import `check_llm_configured`
   directly from `tg_bot.pipeline.analysis` (the source) — not the
   patched-out module-level imports in `callbacks` / `analysis_runner`.
"""

from __future__ import annotations

import os
import sys

import pytest


# Bot-wide concurrency cap for tests. Set BEFORE any test module loads
# `Config` so the lazy semaphore picks up the right value. Was previously
# at the top of test_runner.py, but pytest's collection order varies with
# the set of files collected — when test_pipeline_analysis.py was added,
# Config could end up cached at the default (3) before test_runner.py's
# module-top assignment ran, breaking test_basic_queue's "5 analyzing +
# 5 queued" assertion in non-deterministic ways. conftest is the only
# place guaranteed to fire before any test_*.py module loads.
os.environ.setdefault("TG_BOT_MAX_CONCURRENT_ANALYSES", "5")


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture(scope="session", autouse=True)
def _disable_digest_llm_precheck():
    """Patch the module-level `check_llm_configured` import in both
    handlers that bind it — `callbacks` (used by `_handle_digest`'s
    `digest:run` branch) and `analysis_runner` (used by
    `run_user_digest`'s entry guard). Applied session-wide so each test
    doesn't need to opt in.

    The source function at `tg_bot.pipeline.analysis.check_llm_configured`
    is NOT touched — `test_llm_precheck_*` tests call it directly and
    expect real behavior. Only the rebound module-level references in
    the two handler modules are no-op'd.
    """
    from tg_bot.handlers import analysis_runner, callbacks

    def _noop(*_args, **_kwargs):
        return None

    orig_callbacks = callbacks.check_llm_configured
    orig_runner = analysis_runner.check_llm_configured
    callbacks.check_llm_configured = _noop
    analysis_runner.check_llm_configured = _noop
    yield
    callbacks.check_llm_configured = orig_callbacks
    analysis_runner.check_llm_configured = orig_runner


@pytest.fixture(scope="session", autouse=True)
def _force_markets_open_for_digest_tests():
    """Patch `is_market_open_for` to unconditionally return True for the
    digest test suite. Without this, every digest fan-out test would
    silently take the "all markets closed" branch when pytest happens
    to run on Sat/Sun or a US holiday — the test asserts a fan-out
    flow that the calendar gate skips entirely.

    The source function at `tg_bot.market_calendar.is_market_open_for`
    is NOT touched — `test_market_calendar.py` calls it directly to
    verify real behavior. Only the module-level reference in
    `analysis_runner` (used inside `run_user_digest`) is no-op'd.

    Tests that exercise the calendar gate's effect on the fan-out
    (closed markets, partial closures) re-patch this attribute themselves
    inside a try/finally — see `test_digest.py:test_fanout_*_market*`.
    """
    from tg_bot.handlers import analysis_runner

    orig = analysis_runner.is_market_open_for
    analysis_runner.is_market_open_for = lambda *_args, **_kwargs: True
    yield
    analysis_runner.is_market_open_for = orig
