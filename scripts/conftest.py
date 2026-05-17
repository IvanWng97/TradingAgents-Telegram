"""Pytest config for the smoke suites.

Pytest discovers `scripts/smoke_*.py` files in place (see
`[tool.pytest.ini_options]` in `pyproject.toml`). The migration was
additive — `bash scripts/run_smoke.sh` still works for parity checks,
and the same 222 `async def test_*` functions run under both runners.

This conftest carries the cross-cutting setup that used to live in each
smoke file's `main()`:

1. `src/` on `sys.path` so `tg_bot.*` imports resolve. Each smoke file
   already does this at module top, but conftest fires earlier (during
   collection, before any smoke is imported) so the precheck-disable
   fixture below can import `tg_bot.handlers.*` cleanly.

2. Digest precheck disable. `smoke_digest.py`'s fan-out / cancel /
   progress tests assume `check_llm_configured` is a no-op so they
   don't have to arm a provider + matching env var. The original
   `main()` called `_disable_llm_precheck_globally()` once before
   iterating its SCENARIOS list. Pytest skips `main()`, so we replicate
   the call as an autouse session fixture here. Safe across all suites
   because `test_llm_precheck_*` tests import `check_llm_configured`
   directly from `tg_bot.pipeline.analysis` (the source) — not the
   patched-out module-level imports in `callbacks` / `analysis_runner`.
"""

from __future__ import annotations

import os
import sys

import pytest


_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_SRC = os.path.join(_REPO_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


@pytest.fixture(scope="session", autouse=True)
def _disable_digest_llm_precheck():
    """Patch the module-level `check_llm_configured` import in both
    handlers that bind it — `callbacks` (used by `_handle_digest`'s
    `digest:run` branch) and `analysis_runner` (used by
    `run_user_digest`'s entry guard). Mirrors `smoke_digest.py`'s
    original `_disable_llm_precheck_globally()` helper, hoisted here
    so it applies session-wide without each test needing to opt in.

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
