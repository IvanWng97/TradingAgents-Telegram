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
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_bot.rendering.email_client import (  # noqa: E402
    _build_html,
    _build_subject,
    _signal_tally,
    is_email_configured,
    send_digest_email,
)


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
    send, the function must return False, log a warning, and skip the
    Resend SDK call entirely (no ImportError or KeyError leaks)."""
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
        assert result is False
    finally:
        if saved_key is not None:
            os.environ["RESEND_API_KEY"] = saved_key
        if saved_from is not None:
            os.environ["RESEND_FROM"] = saved_from


async def test_send_digest_email_calls_resend_and_returns_true_on_success() -> None:
    """Happy path: env configured + opt-in addr set → `resend.Emails.send`
    is called with the right payload shape, returns True on success.
    Stubs the SDK entirely so no network call fires."""
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
        assert result is True, "expected True from resend.Emails.send id"
        assert captured_payload["from"] == "bot@example.com"
        assert captured_payload["to"] == ["user@example.com"]
        assert "🌙 Daily Digest" in captured_payload["subject"]
        assert "<html>" in captured_payload["html"]
        assert "NVDA" in captured_payload["html"]
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)


async def test_send_digest_email_swallows_exception_and_returns_false() -> None:
    """Resend API outage / network error: function must log + return False,
    never propagate the exception. The whole point of the email-as-mirror
    pattern is that email failures don't break the Telegram digest."""
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
        assert result is False
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)


async def test_send_digest_email_returns_false_on_malformed_response() -> None:
    """If Resend's response doesn't include an `id` field (defensive against
    a future SDK shape change or partial network response), we log + return
    False rather than treating it as success — keeps observability honest."""
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
        assert result is False
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
