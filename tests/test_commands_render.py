"""MarkdownV2 render-safety pins for `commands.py` handlers (Invariant #4).

Both scenarios here are CI-invisible-by-construction bugs the whole-codebase
review surfaced: a reserved MarkdownV2 char emitted OUTSIDE a code span makes
Telegram reject the entire message (`Bad Request: can't parse entities`), and
neither handler wraps its `reply_text` in try/except — so the command silently
returns nothing. The existing happy-path tests never render the broken
branches:

  - M1 (`/status`): the token-cost figure `~$12.34` carries a bare `.` and
    only renders when `estimate_token_cost_usd` is non-None (a priced model
    with ≥1 analysis) — the example `.env`'s deepseek models aren't priced,
    so out-of-the-box config masks it.
  - M2 (`/email diagnose`): the "skipped" domain-status line carries a bare
    `+`, and that branch fires exactly when Resend is NOT fully wired — the
    common reason to run diagnose — while the 3 existing diagnose tests all
    wire Resend fully and take the other branch.

Both pins drive the REAL handler and validate the captured MarkdownV2.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Fresh data dir so status_cmd's user_config read starts empty (digest line
# absent) regardless of collection order — see tests/CLAUDE.md gotcha.
os.environ["TG_BOT_DATA_DIR"] = tempfile.mkdtemp(prefix="smoke_cmd_render_")

from tg_bot.handlers import commands  # noqa: E402


def _inside_code_span(line: str, needle: str) -> bool:
    """True iff `needle` falls inside a backtick code span in `line`.

    Splitting on backtick yields alternating outside/inside segments
    (index 0 is outside); odd indices are inside spans. None of the values
    rendered here contain literal backticks, so the parity is unambiguous.
    """
    parts = line.split("`")
    return any(needle in parts[i] for i in range(1, len(parts), 2))


def _line_with(text: str, marker: str) -> str:
    for ln in text.split("\n"):
        if marker in ln:
            return ln
    raise AssertionError(f"no line containing {marker!r} in:\n{text}")


# ─── M1: /status token-cost line ────────────────────────────────────────


async def test_status_token_cost_is_inside_code_span(monkeypatch) -> None:
    """The `~$12.34` cost figure must render INSIDE a code span so its `.`
    (and `$`/`~`/parens) are literal. Pre-fix it sat in `\\(\\~$12.34\\)`
    with the `.` bare and reserved → Telegram rejects the whole `/status`
    message and the user sees nothing. Forces the cost branch by stubbing
    the token totals + the price estimator so the test is deterministic
    regardless of the active model's presence in the price table."""
    monkeypatch.setattr(commands, "get_token_totals", lambda: (1_200_000, 567_000))
    monkeypatch.setattr(commands, "estimate_token_cost_usd", lambda *a, **k: 12.34)

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(bot_data={"start_time": time.time(), "analysis_count": 3})

    await commands.status_cmd(update, context)

    reply_mock.assert_called_once()
    text, kwargs = reply_mock.call_args[0][0], reply_mock.call_args[1]
    assert kwargs.get("parse_mode") == "MarkdownV2"
    tokens_line = _line_with(text, "Tokens since boot")
    assert _inside_code_span(tokens_line, "12.34"), (
        "cost figure rendered outside a code span — bare '.' breaks "
        f"MarkdownV2 parsing. Line: {tokens_line!r}"
    )


async def test_status_token_cost_zero_path_renders(monkeypatch) -> None:
    """Control: the no-analyses-yet branch (totals == 0) renders the
    tokens-only line with no cost figure, so the cost-span fix can't
    accidentally break the zero path."""
    monkeypatch.setattr(commands, "get_token_totals", lambda: (0, 0))

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(bot_data={"start_time": time.time()})

    await commands.status_cmd(update, context)

    text = reply_mock.call_args[0][0]
    tokens_line = _line_with(text, "Tokens since boot")
    assert "0 in / 0 out" in tokens_line
    assert "$" not in tokens_line


# ─── M2: /email diagnose "skipped" domain-status line ───────────────────


async def test_email_diagnose_skipped_line_escapes_plus() -> None:
    """When Resend is NOT fully wired, `_email_diagnose` takes the domain
    "skipped" branch whose copy is `...needs both env vars + valid FROM`.
    The `+` is MarkdownV2-reserved and must be escaped (`\\+`), else the
    whole diagnose message fails to parse — precisely when an operator runs
    diagnose to figure out why Resend isn't working. Drives the real
    handler with the env cleared + no recipient so it stays fully offline
    (no `check_resend_domain` / `send_digest_email` call)."""
    saved_key = os.environ.pop("RESEND_API_KEY", None)
    saved_from = os.environ.pop("RESEND_FROM", None)

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    try:
        await commands._email_diagnose(update, current=None)
    finally:
        if saved_key is not None:
            os.environ["RESEND_API_KEY"] = saved_key
        if saved_from is not None:
            os.environ["RESEND_FROM"] = saved_from

    reply_mock.assert_called_once()
    text, kwargs = reply_mock.call_args[0][0], reply_mock.call_args[1]
    assert kwargs.get("parse_mode") == "MarkdownV2"
    assert "Domain status: ⏭ skipped" in text, f"wrong branch rendered:\n{text}"
    assert "env vars \\+ valid FROM" in text, "plus must be escaped (\\+)"
    assert "env vars + valid" not in text, "bare unescaped '+' would break parse"
