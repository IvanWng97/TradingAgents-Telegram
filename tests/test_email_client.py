"""Tests for `rendering/email_client.py` — the digest email mirror.

Pins:
- `is_email_configured()` env-var gate semantics
- `_signal_tally()` per-signal counting + stable ordering
- `_build_subject()` shape
- `_build_html()` per-ticker section + skipped footnote + cancelled/failed rows
- `send_digest_email()` opt-in gate + attachment-build + send-failure log-and-continue

The Resend SDK is stubbed end-to-end — we never make a real network call. Tests
own their `os.environ` mutations (save/restore) so the autouse env-setup in
conftest is preserved across runs.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_bot.config import Config  # noqa: E402
from tg_bot.rendering.email_client import (  # noqa: E402
    EmailSendResult,
    _build_html,
    _build_subject,
    _signal_tally,
    is_email_configured,
    send_digest_email,
)


@pytest.fixture(autouse=True)
def _email_gating_defaults():
    """M3: the email mirror is allow-list-gated. Default every scenario to a
    locked-down bot (so the legacy send/command tests exercise the *enabled*
    path) and a clean per-user test-send cooldown bucket. Open-mode tests
    clear `Config.ALLOWED_USER_IDS` themselves inside the test body; the
    fixture restores it afterward."""
    from tg_bot.handlers import commands

    saved = Config.ALLOWED_USER_IDS
    Config.ALLOWED_USER_IDS = [42]
    commands._email_test_last_sent.clear()
    try:
        yield
    finally:
        Config.ALLOWED_USER_IDS = saved
        commands._email_test_last_sent.clear()


# ─── is_email_configured ────────────────────────────────────────────────


async def test_is_email_configured_requires_both() -> None:
    """Both env vars must be present — either alone is incomplete and the
    Resend API call would fail anyway. Pinning both halves prevents a
    future refactor from silently accepting a half-configured state."""
    saved_key = os.environ.pop("RESEND_API_KEY", None)
    saved_from = os.environ.pop("RESEND_FROM", None)
    try:
        # Neither set → False.
        assert is_email_configured() is False

        # Only key set → still False.
        os.environ["RESEND_API_KEY"] = "re_fake"
        assert is_email_configured() is False

        # Only from set → still False.
        os.environ.pop("RESEND_API_KEY", None)
        os.environ["RESEND_FROM"] = "bot@example.com"
        assert is_email_configured() is False

        # Both set → True.
        os.environ["RESEND_API_KEY"] = "re_fake"
        assert is_email_configured() is True
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        if saved_key is not None:
            os.environ["RESEND_API_KEY"] = saved_key
        if saved_from is not None:
            os.environ["RESEND_FROM"] = saved_from


async def test_is_email_configured_rejects_empty_string() -> None:
    """Empty-string env vars (`RESEND_API_KEY=`) are treated as unset —
    matches the "falsy means missing" convention used elsewhere in the bot."""
    saved_key = os.environ.pop("RESEND_API_KEY", None)
    saved_from = os.environ.pop("RESEND_FROM", None)
    try:
        os.environ["RESEND_API_KEY"] = ""
        os.environ["RESEND_FROM"] = ""
        assert is_email_configured() is False
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        if saved_key is not None:
            os.environ["RESEND_API_KEY"] = saved_key
        if saved_from is not None:
            os.environ["RESEND_FROM"] = saved_from


# ─── _signal_tally ──────────────────────────────────────────────────────


async def test_signal_tally_orders_by_count_desc_then_alpha() -> None:
    """Most common signal first; ties broken alphabetically. Pinning the
    sort order keeps subject lines stable across runs (otherwise the
    same digest could produce `2 BUY · 2 HOLD` and `2 HOLD · 2 BUY`
    on alternating fires — confusing for inbox scanning)."""
    status = {
        "NVDA": {"signal": "BUY"},
        "AAPL": {"signal": "BUY"},
        "TSLA": {"signal": "HOLD"},
        "GME": {"signal": "BUY"},
        "AMD": {"signal": "HOLD"},
    }
    out = _signal_tally(status)
    # 3 BUY (most), then 2 HOLD.
    assert out == "3 BUY · 2 HOLD", out


async def test_signal_tally_skips_non_dict_status() -> None:
    """Cancelled/failed tickers shouldn't contribute to the tally —
    only completed (dict) results count."""
    status = {
        "NVDA": {"signal": "BUY"},
        "AAPL": "cancelled",
        "TSLA": None,
        "GME": ("analyzing", "market analyst", 1),
    }
    assert _signal_tally(status) == "1 BUY"


async def test_signal_tally_empty_when_nothing_completed() -> None:
    """A digest where every ticker failed/cancelled — keep the subject
    rendering safe rather than producing an empty parenthetical."""
    status = {"NVDA": None, "AAPL": "cancelled"}
    assert _signal_tally(status) == "no completed tickers"


# ─── _build_subject ─────────────────────────────────────────────────────


async def test_build_subject_includes_tally() -> None:
    status = {"NVDA": {"signal": "BUY"}, "AAPL": {"signal": "HOLD"}}
    subj = _build_subject("2026-05-18", status)
    assert subj == "🌙 Daily Digest — 2026-05-18 (1 BUY · 1 HOLD)", subj


# ─── _build_html ────────────────────────────────────────────────────────


async def test_build_html_renders_each_completed_ticker_section() -> None:
    """Per-ticker section must have: signal emoji, ticker name, signal verb,
    inlined finviz chart `<img>`, Telegraph link. This is the bulk of what
    the user sees in their inbox; any missing element silently degrades
    the email."""
    status = {
        "NVDA": {
            "ticker": "NVDA",
            "signal": "BUY",
            "telegraph_url": "https://telegra.ph/NVDA-Analysis-05-18",
        },
    }
    html = _build_html(["NVDA"], status, "2026-05-18")
    assert "<b>NVDA</b>" in html, "ticker name missing from section header"
    assert "BUY" in html, "signal verb missing"
    assert "🟢" in html, "BUY signal emoji missing"
    assert "<img" in html and "NVDA" in html, "finviz chart img missing"
    assert "telegra.ph/NVDA-Analysis-05-18" in html, "Telegraph link missing"
    assert "Full analysis on Telegraph" in html, "Telegraph link text missing"


async def test_build_html_omits_telegraph_link_when_publish_failed() -> None:
    """`telegraph_url=None` means the Telegraph publish failed during the
    fan-out. The email should still render the section (chart + signal)
    but skip the broken link entirely — better no link than a `href="None"`."""
    status = {"NVDA": {"signal": "BUY", "telegraph_url": None}}
    html = _build_html(["NVDA"], status, "2026-05-18")
    assert "Full analysis on Telegraph" not in html
    assert 'href="None"' not in html


async def test_build_html_renders_cancelled_and_failed_rows() -> None:
    """Cancelled (⛔) and failed (❓) tickers get short status rows, not
    full sections (no chart, no link). Mirrors the Telegram summary
    format so users see the same shape across both channels."""
    status = {
        "CANCEL": "cancelled",
        "FAIL": None,
    }
    html = _build_html(["CANCEL", "FAIL"], status, "2026-05-18")
    assert "⛔" in html and "CANCEL" in html and "cancelled" in html
    assert "❓" in html and "FAIL" in html and "error" in html


async def test_build_html_renders_skipped_footnote_when_set() -> None:
    """The market-calendar gate's skipped tickers must appear as a
    footnote — same content as the Telegram digest's footnote so the
    email tells the same story."""
    status = {"NVDA": {"signal": "BUY", "telegraph_url": None}}
    html = _build_html(
        ["NVDA"], status, "2026-05-18", skipped_closed=["0700.HK", "601318.SS"]
    )
    assert "Skipped (markets closed)" in html
    assert "0700.HK" in html and "601318.SS" in html


async def test_build_html_html_escapes_ticker_with_special_chars() -> None:
    """Defensive: a future ticker with HTML metacharacters (currently
    impossible given `TICKER_RE` but storage could be hand-edited) must
    not produce XSS-risky output even in an internal email."""
    status = {"<script>": {"signal": "BUY", "telegraph_url": None}}
    html = _build_html(["<script>"], status, "2026-05-18")
    assert "<script>" not in html, "raw ticker bypassed HTML escape"
    assert "&lt;script&gt;" in html, "ticker not properly escaped"


# ─── send_digest_email ──────────────────────────────────────────────────


async def test_send_digest_email_short_circuits_when_env_missing() -> None:
    """Defensive second-line gate inside `send_digest_email`: if
    `RESEND_API_KEY` was hot-removed between caller-side check and the
    send, the function must return an `ok=False` result tagged
    `not_configured`, log a warning, and skip the Resend SDK call
    entirely (no ImportError or KeyError leaks)."""
    saved_key = os.environ.pop("RESEND_API_KEY", None)
    saved_from = os.environ.pop("RESEND_FROM", None)
    try:
        result = await send_digest_email(
            to_addr="user@example.com",
            watchlist=["NVDA"],
            status={"NVDA": {"signal": "BUY", "telegraph_url": None}},
            safe_date="2026-05-18",
            date_iso="2026-05-18",
        )
        assert isinstance(result, EmailSendResult)
        assert result.ok is False
        assert result.recipient == "user@example.com"
        assert result.error == "not_configured"
        assert result.message_id is None
    finally:
        if saved_key is not None:
            os.environ["RESEND_API_KEY"] = saved_key
        if saved_from is not None:
            os.environ["RESEND_FROM"] = saved_from


async def test_send_digest_email_calls_resend_and_returns_ok_on_success() -> None:
    """Happy path: env configured + opt-in addr set → `resend.Emails.send`
    is called with the right payload shape, returns `ok=True` carrying the
    Resend message id for downstream log correlation. Stubs the SDK
    entirely so no network call fires."""
    os.environ["RESEND_API_KEY"] = "re_fake"
    os.environ["RESEND_FROM"] = "bot@example.com"

    captured_payload: dict = {}

    def fake_send(payload):
        captured_payload.update(payload)
        return {"id": "re_message_abc123"}

    try:
        with patch("resend.Emails.send", side_effect=fake_send):
            result = await send_digest_email(
                to_addr="user@example.com",
                watchlist=["NVDA"],
                status={"NVDA": {"signal": "BUY", "telegraph_url": None}},
                safe_date="2026-05-18",
                date_iso="2026-05-18",
            )
        assert result.ok is True, "expected ok=True from resend.Emails.send id"
        assert result.recipient == "user@example.com"
        assert result.message_id == "re_message_abc123", (
            "message id must round-trip — used for log correlation"
        )
        assert result.error is None
        assert captured_payload["from"] == "bot@example.com"
        assert captured_payload["to"] == ["user@example.com"]
        assert "🌙 Daily Digest" in captured_payload["subject"]
        assert "<html>" in captured_payload["html"]
        assert "NVDA" in captured_payload["html"]
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)


async def test_send_digest_email_swallows_exception_and_returns_error() -> None:
    """Resend API outage / network error: function must log + return an
    `ok=False` result with `error` set to the exception class name, never
    propagate the exception. The whole point of the email-as-mirror pattern
    is that email failures don't break the Telegram digest. The exception
    class name on the result is what drives the summary footer's "Email
    failed" line — pin it here so a refactor can't drop the breadcrumb."""
    os.environ["RESEND_API_KEY"] = "re_fake"
    os.environ["RESEND_FROM"] = "bot@example.com"
    try:
        with patch(
            "resend.Emails.send",
            side_effect=RuntimeError("Resend is down"),
        ):
            result = await send_digest_email(
                to_addr="user@example.com",
                watchlist=["NVDA"],
                status={"NVDA": {"signal": "BUY", "telegraph_url": None}},
                safe_date="2026-05-18",
                date_iso="2026-05-18",
            )
        assert result.ok is False
        assert result.recipient == "user@example.com"
        assert result.error == "RuntimeError"
        assert result.message_id is None
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)


async def test_send_digest_email_returns_error_on_malformed_response() -> None:
    """If Resend's response doesn't include an `id` field (defensive against
    a future SDK shape change or partial network response), we log + return
    an `ok=False` result tagged `malformed_response` rather than treating
    it as success — keeps observability honest."""
    os.environ["RESEND_API_KEY"] = "re_fake"
    os.environ["RESEND_FROM"] = "bot@example.com"
    try:
        with patch("resend.Emails.send", return_value={"unexpected": "shape"}):
            result = await send_digest_email(
                to_addr="user@example.com",
                watchlist=["NVDA"],
                status={"NVDA": {"signal": "BUY", "telegraph_url": None}},
                safe_date="2026-05-18",
                date_iso="2026-05-18",
            )
        assert result.ok is False
        assert result.recipient == "user@example.com"
        assert result.error == "malformed_response"
        assert result.message_id is None
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)


# ─── EmailSendResult __post_init__ guard ────────────────────────────────


async def test_email_send_result_rejects_ok_with_error() -> None:
    """Mutual exclusivity: `ok=True` with `error` set must raise at
    construction. Prevents the latent trap where a misconstructed result
    silently renders the wrong footer line. Architect's "cheap hardening"
    on PR #75."""
    import pytest

    with pytest.raises(ValueError, match="ok=True must have error=None"):
        EmailSendResult(ok=True, recipient="x@y.com", error="wat")


async def test_email_send_result_rejects_failure_with_message_id() -> None:
    """Mirror of the prior guard: `ok=False` with `message_id` set is a
    contradiction (no successful send produces a message id when ok=False).
    Raise at construction."""
    import pytest

    with pytest.raises(ValueError, match="ok=False must have message_id=None"):
        EmailSendResult(ok=False, recipient="x@y.com", message_id="re_abc")


async def test_email_send_result_accepts_valid_shapes() -> None:
    """Both legitimate shapes — success (ok + message_id) and failure
    (not ok + error) — construct without raising. Pinning the happy
    path so the guard above can't drift into rejecting valid results."""
    # Success path.
    ok_result = EmailSendResult(ok=True, recipient="x@y.com", message_id="re_x")
    assert ok_result.message_id == "re_x"

    # Failure path with error string.
    err_result = EmailSendResult(ok=False, recipient="x@y.com", error="RuntimeError")
    assert err_result.error == "RuntimeError"

    # Failure path with no error (defensive — error is optional).
    bare_failure = EmailSendResult(ok=False, recipient="x@y.com")
    assert bare_failure.ok is False


# ─── /email test failure-path regression guard ──────────────────────────


async def test_email_test_command_reports_failure_when_send_fails() -> None:
    """Reviewer caught on PR #75: `commands.py:631` was `if ok:` which
    tested truthiness of the EmailSendResult dataclass (always True since
    non-None). The fix is `if result.ok:`. This test pins the failure
    branch so a future refactor can't regress it back to the always-success
    shape.

    Stubs `send_digest_email` to return `ok=False`; asserts the user
    receives the ❌ failure message, NOT the ✅ success message.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    # Minimal Update + Context fakes — email_cmd only reaches for
    # `update.effective_user.id`, `update.message.reply_text`, and
    # `context.args`.
    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(args=["test"])

    os.environ["RESEND_API_KEY"] = "re_fake"
    os.environ["RESEND_FROM"] = "bot@example.com"

    class _StubUserConfig:
        def get_digest(self, _uid):
            return {"email": "user@example.com"}

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()

    async def fake_send(**kwargs):
        return EmailSendResult(
            ok=False,
            recipient=kwargs["to_addr"],
            error="RuntimeError",
        )

    try:
        with patch.object(commands, "send_digest_email", side_effect=fake_send):
            await commands.email_cmd(update, context)

        reply_mock.assert_called_once()
        sent_text = reply_mock.call_args[0][0]
        assert "❌ Test email failed" in sent_text, (
            f"expected failure message; got: {sent_text!r}"
        )
        # The success message must NOT appear — guards against the
        # always-truthy `if ok:` regression.
        assert "✅ Test email sent" not in sent_text
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        commands.user_config_storage = orig_uc


async def test_email_test_command_reports_success_when_send_succeeds() -> None:
    """Counterpart to the failure-path test: `ok=True` should produce
    the ✅ success message. Together they pin the `if result.ok` boolean
    branching in `commands.py:email_cmd`."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(args=["test"])

    os.environ["RESEND_API_KEY"] = "re_fake"
    os.environ["RESEND_FROM"] = "bot@example.com"

    class _StubUserConfig:
        def get_digest(self, _uid):
            return {"email": "user@example.com"}

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()

    async def fake_send(**kwargs):
        return EmailSendResult(
            ok=True,
            recipient=kwargs["to_addr"],
            message_id="re_abc",
        )

    try:
        with patch.object(commands, "send_digest_email", side_effect=fake_send):
            await commands.email_cmd(update, context)

        sent_text = reply_mock.call_args[0][0]
        assert "✅ Test email sent" in sent_text, (
            f"expected success message; got: {sent_text!r}"
        )
        # MarkdownV2 escapes the `.` in the address — assert on the
        # escaped form, not raw.
        assert "user@example\\.com" in sent_text, (
            f"recipient missing/wrong-escaped in success message: {sent_text!r}"
        )
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        commands.user_config_storage = orig_uc


# ─── /email ForceReply UX ────────────────────────────────────────────────


async def test_email_cmd_no_args_sends_forcereply_prompt() -> None:
    """Bare `/email` must open a ForceReply prompt (UX parity with bare
    `/add`) instead of showing the current setting. The current setting
    moved to `/status` in this PR — pin both halves so a future revert
    can't split the surfaces."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from telegram import ForceReply

    from tg_bot.handlers import commands

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(args=[])

    class _StubUserConfig:
        def get_digest(self, _uid):
            return {"email": "user@example.com"}  # already set

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()
    try:
        await commands.email_cmd(update, context)
        reply_mock.assert_called_once()
        # First positional is the prompt text.
        sent_text = reply_mock.call_args[0][0]
        assert sent_text == commands.EMAIL_PROMPT, (
            f"expected EMAIL_PROMPT verbatim; got: {sent_text!r}"
        )
        # The reply_markup must be a ForceReply — that's what makes the
        # Telegram client pop the reply box.
        kwargs = reply_mock.call_args.kwargs
        assert isinstance(kwargs.get("reply_markup"), ForceReply), (
            f"expected ForceReply, got: {kwargs.get('reply_markup')!r}"
        )
        # Crucially, the current email address must NOT appear in the
        # prompt — it moved to /status.
        assert "user@example.com" not in sent_text
    finally:
        commands.user_config_storage = orig_uc


async def test_email_via_reply_sets_address_and_confirms() -> None:
    """Reply to the EMAIL_PROMPT message → set the email + send a
    confirmation. Mirrors `add_via_reply`'s confirm-after-set UX."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    os.environ["RESEND_API_KEY"] = "re_fake"
    os.environ["RESEND_FROM"] = "bot@example.com"

    reply_mock = AsyncMock()
    bot_user = SimpleNamespace(is_bot=True)
    # Simulate the user replying to our EMAIL_PROMPT with their address.
    replied = SimpleNamespace(text=commands.EMAIL_PROMPT, from_user=bot_user)
    msg = SimpleNamespace(
        text="user@example.com",
        reply_to_message=replied,
        reply_text=reply_mock,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=msg,
    )
    context = SimpleNamespace(args=[])

    saved: list[tuple[str, str]] = []

    class _StubUserConfig:
        async def set_digest_email(self, uid, addr):
            saved.append((uid, addr))
            return True  # valid

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()
    try:
        await commands.email_via_reply(update, context)
        assert saved == [("42", "user@example.com")], (
            f"expected set_digest_email to fire; got {saved}"
        )
        reply_mock.assert_called_once()
        confirmation = reply_mock.call_args[0][0]
        assert "✅ Email mirror set to" in confirmation, (
            f"expected confirmation text; got: {confirmation!r}"
        )
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        commands.user_config_storage = orig_uc


async def test_email_via_reply_refused_in_open_mode() -> None:
    """M3: the reply save-path honors the open-mode gate too. Replying to
    EMAIL_PROMPT with an address while `ALLOWED_USER_IDS` is empty must reply
    with the disabled notice and NOT persist the address (this path is only
    reachable if the allowlist was cleared after the prompt was shown, since
    the bare-`/email` prompt is itself gated)."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    Config.ALLOWED_USER_IDS = []  # open mode (fixture restores)

    reply_mock = AsyncMock()
    bot_user = SimpleNamespace(is_bot=True)
    replied = SimpleNamespace(text=commands.EMAIL_PROMPT, from_user=bot_user)
    msg = SimpleNamespace(
        text="victim@example.com",
        reply_to_message=replied,
        reply_text=reply_mock,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=msg,
    )
    context = SimpleNamespace(args=[])

    saved: list = []

    class _StubUserConfig:
        async def set_digest_email(self, uid, addr):
            saved.append((uid, addr))
            return True

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()
    try:
        await commands.email_via_reply(update, context)
        assert saved == [], "address must NOT be saved via reply in open mode"
        reply_mock.assert_called_once()
        notice = reply_mock.call_args[0][0]
        assert "ALLOWED_USER_IDS" in notice, (
            f"expected open-mode notice; got: {notice!r}"
        )
    finally:
        commands.user_config_storage = orig_uc


async def test_email_via_reply_rejects_invalid_address() -> None:
    """A non-email reply must surface the same `_EMAIL_RE` rejection that
    `/email <addr>` shows, NOT silently no-op. Pinning so the regression
    'reply did nothing' never lands."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    reply_mock = AsyncMock()
    bot_user = SimpleNamespace(is_bot=True)
    replied = SimpleNamespace(text=commands.EMAIL_PROMPT, from_user=bot_user)
    msg = SimpleNamespace(
        text="not-an-email",
        reply_to_message=replied,
        reply_text=reply_mock,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=msg,
    )
    context = SimpleNamespace(args=[])

    class _StubUserConfig:
        async def set_digest_email(self, _uid, _addr):
            return False  # rejected by _EMAIL_RE

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()
    try:
        await commands.email_via_reply(update, context)
        reply_mock.assert_called_once()
        reply_text = reply_mock.call_args[0][0]
        assert "doesn't look like a valid" in reply_text, (
            f"expected rejection message; got: {reply_text!r}"
        )
    finally:
        commands.user_config_storage = orig_uc


async def test_email_via_reply_ignores_replies_to_other_prompts() -> None:
    """If a user replies to the bot's ADD_PROMPT (or any other bot message),
    email_via_reply must early-return without touching storage. The strict
    EMAIL_PROMPT match is what allows both add_via_reply and email_via_reply
    to coexist as MessageHandlers on the same filter."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    reply_mock = AsyncMock()
    bot_user = SimpleNamespace(is_bot=True)
    # Replying to ADD_PROMPT, not EMAIL_PROMPT.
    replied = SimpleNamespace(text=commands.ADD_PROMPT, from_user=bot_user)
    msg = SimpleNamespace(
        text="NVDA",
        reply_to_message=replied,
        reply_text=reply_mock,
    )
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=msg,
    )
    context = SimpleNamespace(args=[])

    fired: list[str] = []

    class _StubUserConfig:
        async def set_digest_email(self, _uid, addr):
            fired.append(addr)
            return True

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()
    try:
        await commands.email_via_reply(update, context)
        assert fired == [], "email_via_reply must NOT fire on ADD_PROMPT replies"
        reply_mock.assert_not_called()
    finally:
        commands.user_config_storage = orig_uc


# ─── /email diagnose ───────────────────────────────────────────────────


async def test_email_diagnose_reports_all_green_on_happy_path() -> None:
    """End-to-end happy path: env vars set, domain verified in Resend,
    test send succeeds → message shows ✅ for all four checks. Pins
    the operator-facing one-shot diagnose UX so a future refactor
    doesn't drop one of the four surfaces."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    os.environ["RESEND_API_KEY"] = "re_fake"
    os.environ["RESEND_FROM"] = "bot@yifwang.com"

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(args=["diagnose"])

    class _StubUserConfig:
        def get_digest(self, _uid):
            return {"email": "user@example.com"}

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()

    # Stub resend.Domains.list to return our verified domain.
    fake_resend_module = SimpleNamespace()

    class _FakeDomains:
        @staticmethod
        def list():
            return {"data": [{"name": "yifwang.com", "status": "verified"}]}

    fake_resend_module.Domains = _FakeDomains
    fake_resend_module.api_key = ""

    async def fake_send(**kwargs):
        return EmailSendResult(
            ok=True, recipient=kwargs["to_addr"], message_id="re_msg_abc"
        )

    try:
        with patch.dict("sys.modules", {"resend": fake_resend_module}):
            with patch.object(commands, "send_digest_email", side_effect=fake_send):
                await commands.email_cmd(update, context)

        sent = reply_mock.call_args[0][0]
        # All four checks must show ✅ on the happy path.
        assert sent.count("✅") >= 4, (
            f"expected 4+ ✅ markers (api key, from, domain, test send); got: {sent!r}"
        )
        # MarkdownV2 escapes `.` and `_` — assert on the wire form.
        assert "yifwang\\.com` verified" in sent, (
            f"verified-domain line missing/wrong-escaped: {sent!r}"
        )
        assert "re\\_msg\\_abc" in sent, (
            f"Resend message id missing from test-send line: {sent!r}"
        )
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        commands.user_config_storage = orig_uc


async def test_email_diagnose_flags_unverified_domain() -> None:
    """Resend reports the domain as 'pending' → diagnose shows ⏳ on
    the domain line with the status string included. Pins that a partial
    setup (env wired, domain added, DNS not propagated) surfaces clearly
    instead of falsely reporting all-green."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    os.environ["RESEND_API_KEY"] = "re_fake"
    os.environ["RESEND_FROM"] = "bot@yifwang.com"

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(args=["diagnose"])

    class _StubUserConfig:
        def get_digest(self, _uid):
            return {"email": "user@example.com"}

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()

    fake_resend_module = SimpleNamespace()

    class _FakeDomains:
        @staticmethod
        def list():
            return {"data": [{"name": "yifwang.com", "status": "pending"}]}

    fake_resend_module.Domains = _FakeDomains
    fake_resend_module.api_key = ""

    async def fake_send(**kwargs):
        # Send is attempted even when domain pending — Resend rejects it.
        return EmailSendResult(
            ok=False, recipient=kwargs["to_addr"], error="DomainNotVerified"
        )

    try:
        with patch.dict("sys.modules", {"resend": fake_resend_module}):
            with patch.object(commands, "send_digest_email", side_effect=fake_send):
                await commands.email_cmd(update, context)

        sent = reply_mock.call_args[0][0]
        assert "⏳" in sent, f"expected ⏳ for pending domain; got: {sent!r}"
        assert "pending" in sent
        assert "❌" in sent, "expected ❌ on test-send line for unverified domain"
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        commands.user_config_storage = orig_uc


async def test_email_diagnose_handles_resend_api_error_gracefully() -> None:
    """Resend API key invalid / Resend down → Domains.list raises. The
    diagnose surface must catch + render ❌, NOT crash the command.
    Pins that a bad API key still produces a useful response."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    os.environ["RESEND_API_KEY"] = "re_bogus"
    os.environ["RESEND_FROM"] = "bot@yifwang.com"

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(args=["diagnose"])

    class _StubUserConfig:
        def get_digest(self, _uid):
            return {"email": "user@example.com"}

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()

    fake_resend_module = SimpleNamespace()

    class _FakeDomains:
        @staticmethod
        def list():
            raise RuntimeError("Unauthorized — bad API key")

    fake_resend_module.Domains = _FakeDomains
    fake_resend_module.api_key = ""

    async def fake_send(**kwargs):
        return EmailSendResult(
            ok=False, recipient=kwargs["to_addr"], error="AuthenticationError"
        )

    try:
        with patch.dict("sys.modules", {"resend": fake_resend_module}):
            with patch.object(commands, "send_digest_email", side_effect=fake_send):
                # Must NOT raise.
                await commands.email_cmd(update, context)

        sent = reply_mock.call_args[0][0]
        assert "❌" in sent
        assert "RuntimeError" in sent, (
            f"expected RuntimeError class name in API-error message; got: {sent!r}"
        )
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        commands.user_config_storage = orig_uc


# ─── M3: email-mirror abuse gating ───────────────────────────────────────
# The mirror relays through the operator's verified Resend domain to a
# user-supplied recipient. Two guards: (1) refuse all sends when the bot is
# open to everyone (empty ALLOWED_USER_IDS); (2) per-user cooldown on the
# immediate `/email test` + `/email diagnose` test sends.


async def test_send_digest_email_refuses_in_open_mode() -> None:
    """Backstop gate: even with env configured + a recipient set,
    `send_digest_email` must refuse (ok=False, error='open_mode') and never
    touch the Resend SDK when ALLOWED_USER_IDS is empty. This is the layer
    that protects the *daily* mirror too, not just the command surface."""
    from types import SimpleNamespace

    Config.ALLOWED_USER_IDS = []  # open mode (fixture restores)
    os.environ["RESEND_API_KEY"] = "re_fake"
    os.environ["RESEND_FROM"] = "bot@example.com"

    sent_calls: list = []

    def fake_send(payload):
        sent_calls.append(payload)
        return {"id": "SHOULD_NOT_SEND"}

    fake_resend = SimpleNamespace(
        api_key="",
        Emails=SimpleNamespace(send=fake_send),
    )
    try:
        with patch.dict("sys.modules", {"resend": fake_resend}):
            result = await send_digest_email(
                to_addr="victim@example.com",
                watchlist=["NVDA"],
                status={"NVDA": {"signal": "BUY", "telegraph_url": None}},
                safe_date="2026-06-26",
                date_iso="2026-06-26",
            )
        assert result.ok is False
        assert result.error == "open_mode", (
            f"expected open_mode refusal; got error={result.error!r}"
        )
        assert result.recipient == "victim@example.com"
        assert result.message_id is None
        assert sent_calls == [], "Resend.Emails.send must NOT be called in open mode"
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)


async def test_email_cmd_refuses_set_in_open_mode() -> None:
    """`/email <addr>` in open mode must reply with the disabled notice and
    NOT persist the address — otherwise a stranger could register a victim's
    address as a relay target."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    Config.ALLOWED_USER_IDS = []  # open mode

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(args=["victim@example.com"])

    set_calls: list = []

    class _StubUserConfig:
        def get_digest(self, _uid):
            return {}

        async def set_digest_email(self, uid, addr):
            set_calls.append((uid, addr))
            return True

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()
    try:
        await commands.email_cmd(update, context)
        sent = reply_mock.call_args[0][0]
        assert "ALLOWED_USER_IDS" in sent, f"expected open-mode notice; got: {sent!r}"
        assert set_calls == [], "address must NOT be saved in open mode"
    finally:
        commands.user_config_storage = orig_uc


async def test_email_cmd_off_allowed_in_open_mode() -> None:
    """`/email off` is the one carve-out: it only clears a (possibly stale)
    address, so it stays allowed even in open mode."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    Config.ALLOWED_USER_IDS = []  # open mode

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(args=["off"])

    cleared: list = []

    class _StubUserConfig:
        def get_digest(self, _uid):
            return {"email": "user@example.com"}

        async def clear_digest_email(self, uid):
            cleared.append(uid)
            return True

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()
    try:
        await commands.email_cmd(update, context)
        sent = reply_mock.call_args[0][0]
        assert "ALLOWED_USER_IDS" not in sent, (
            f"`off` must not hit the open-mode notice; got: {sent!r}"
        )
        assert cleared == ["42"], "off must clear the address even in open mode"
    finally:
        commands.user_config_storage = orig_uc


async def test_email_test_cooldown_blocks_rapid_second_send() -> None:
    """Two `/email test` in a row: the first sends, the second is throttled
    (no send). Pins the per-user cooldown so an allow-listed user can't hammer
    Resend on the operator's domain."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tg_bot.handlers import commands

    os.environ["RESEND_API_KEY"] = "re_fake"
    os.environ["RESEND_FROM"] = "bot@example.com"

    reply_mock = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=SimpleNamespace(reply_text=reply_mock),
    )
    context = SimpleNamespace(args=["test"])

    class _StubUserConfig:
        def get_digest(self, _uid):
            return {"email": "user@example.com"}

    orig_uc = commands.user_config_storage
    commands.user_config_storage = _StubUserConfig()

    send_count = {"n": 0}

    async def fake_send(**kwargs):
        send_count["n"] += 1
        return EmailSendResult(ok=True, recipient=kwargs["to_addr"], message_id="re_ok")

    try:
        with patch.object(commands, "send_digest_email", side_effect=fake_send):
            await commands.email_cmd(update, context)
            first = reply_mock.call_args[0][0]
            await commands.email_cmd(update, context)
            second = reply_mock.call_args[0][0]
        assert "✅ Test email sent" in first, f"first send should succeed: {first!r}"
        assert "Slow down" in second, (
            f"second send should be throttled; got: {second!r}"
        )
        assert send_count["n"] == 1, "only ONE email should actually be sent"
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        commands.user_config_storage = orig_uc


async def test_email_test_cooldown_remaining_helper() -> None:
    """Unit-pin the cooldown arithmetic: first call records + returns None,
    an immediate repeat returns a positive remaining, and once the window has
    elapsed it returns None again (resetting the bucket)."""
    from tg_bot.handlers import commands

    uid = 777
    assert commands._email_test_cooldown_remaining(uid) is None
    remaining = commands._email_test_cooldown_remaining(uid)
    assert remaining is not None and 0 < remaining <= commands._EMAIL_TEST_COOLDOWN_S
    # Backdate past the window — next call is a clean miss again.
    commands._email_test_last_sent[uid] = (
        time.monotonic() - commands._EMAIL_TEST_COOLDOWN_S - 1
    )
    assert commands._email_test_cooldown_remaining(uid) is None
