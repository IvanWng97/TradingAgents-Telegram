"""Send the daily digest summary as an HTML email via Resend.

Mirrors the Telegram summary at digest completion when the user has opted
in via `/email set <addr>`. Email is a MIRROR, never a REPLACEMENT —
Telegram delivery happens first and is the authoritative output channel.
Email send failures log + swallow so a Resend outage cannot break the
Telegram path.

Module placement parallels `telegraph_client.py`: a single file owning
both the HTML template build AND the network send (in `to_thread`),
since the two halves are tightly coupled to the destination service's
API shape.

What the email looks like:

    🌙 Daily Digest — 2026-05-18  (3 BUY · 2 HOLD · 1 SELL)

    [per-ticker section, repeated]
    🟢 NVDA — BUY
    <finviz chart inlined as <img src="...">>
    📰 Full analysis on Telegraph: <link>

    [footer if applicable]
    ⚫️ Skipped (markets closed): 0700.HK, 601318.SS

Per-ticker `.md` files (the full tradingagents report) are attached so
the operator has an archive-quality artifact in their inbox alongside
the live Telegraph link.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from html import escape as _html_escape

from tg_bot.history import load_historical_state
from tg_bot.rendering.chart import finviz_chart_url
from tg_bot.rendering.formatters import DECISION_EMOJI, format_full_md_report


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailSendResult:
    """Outcome of one `send_digest_email` call.

    `ok=True` only when Resend accepted the send and returned a message
    id; every other path (env hot-removed, SDK exception, malformed
    response) is `ok=False` with `error` set to a short failure category
    for log correlation + summary-line rendering. `recipient` is always
    populated (caller passes the address in) so the Telegram summary
    appender doesn't have to re-read storage to render "📧 Sent to
    <addr>".

    `__post_init__` enforces mutual exclusivity (`ok and not error`,
    `not ok and not message_id`) — the fields are nominally independent
    by type, but a misconstruction would silently render the wrong
    footer line. Raising at construction time prevents the trap from
    propagating to the renderer.
    """

    ok: bool
    recipient: str
    message_id: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.ok and self.error is not None:
            raise ValueError(
                f"EmailSendResult: ok=True must have error=None, got {self.error!r}"
            )
        if not self.ok and self.message_id is not None:
            raise ValueError(
                f"EmailSendResult: ok=False must have message_id=None, "
                f"got {self.message_id!r}"
            )


# Inline-style constants. Gmail/Outlook strip <style> blocks; everything
# must live on the element. Defined once here to keep the template
# functions readable + ensure consistency across rows.
_FONT_FAMILY = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
_STYLE_H2 = f"margin:0 0 8px 0;color:#222;font-family:{_FONT_FAMILY};font-weight:600;"
_STYLE_TALLY = (
    f"margin:0 0 24px 0;color:#666;font-family:{_FONT_FAMILY};font-size:14px;"
)
_STYLE_ROW_DIM = (
    f"padding:12px 0;border-top:1px solid #eee;color:#999;"
    f"font-family:{_FONT_FAMILY};font-size:14px;"
)
_STYLE_SECTION = f"padding:16px 0;border-top:1px solid #eee;font-family:{_FONT_FAMILY};"
_STYLE_H3 = "margin:0 0 12px 0;color:#222;font-weight:600;font-size:18px;"
_STYLE_IMG = "max-width:100%;height:auto;border-radius:4px;"
_STYLE_LINK_P = "margin:12px 0 0 0;font-size:14px;"
_STYLE_LINK = "color:#1a73e8;"
_STYLE_FOOTNOTE = (
    f"margin:24px 0 0 0;padding-top:16px;border-top:1px solid #eee;"
    f"color:#999;font-family:{_FONT_FAMILY};font-size:12px;"
)
_STYLE_FOOTER = (
    f"margin:24px 0 0 0;color:#bbb;font-family:{_FONT_FAMILY};font-size:11px;"
)


def is_email_configured() -> bool:
    """True iff both `RESEND_API_KEY` and `RESEND_FROM` are present in env.

    Used by callers to short-circuit before building attachments or
    rendering the template — both are expensive enough to skip when
    the send would fail anyway. Also drives the startup WARNING in
    `_post_init` when any user has email opt-in but the env is missing.
    """
    return bool(os.environ.get("RESEND_API_KEY")) and bool(
        os.environ.get("RESEND_FROM")
    )


def _signal_tally(status: dict) -> str:
    """Render a one-line "3 BUY · 2 HOLD · 1 SELL" tally for the subject
    line. Counts only completed (dict) status entries — cancelled and
    failed don't contribute. Empty result returns "no completed tickers"
    so the subject is never blank."""
    counts: dict[str, int] = {}
    for s in status.values():
        if isinstance(s, dict):
            sig = (s.get("signal") or "").upper().strip()
            if sig:
                counts[sig] = counts.get(sig, 0) + 1
    if not counts:
        return "no completed tickers"
    # Stable order: most common first, then alphabetical for ties.
    parts = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return " · ".join(f"{n} {sig}" for sig, n in parts)


def _build_subject(safe_date: str, status: dict) -> str:
    """Subject line: `🌙 Daily Digest — 2026-05-18 (3 BUY · 2 HOLD)`."""
    return f"🌙 Daily Digest — {safe_date} ({_signal_tally(status)})"


def _build_html(
    watchlist: list[str],
    status: dict,
    safe_date: str,
    skipped_closed: list[str] | None = None,
) -> str:
    """Render the HTML email body. Inline styles only — Gmail/Outlook
    strip `<style>` blocks and `<link rel=stylesheet>` is ignored by
    every major client.

    Per-ticker section: signal-emoji header + inlined finviz chart +
    Telegraph link (or "no link" if publish failed). Cancelled/failed
    tickers get a short row instead of a full section.

    `safe_date` is pre-escaped by the caller.
    """
    sections: list[str] = []
    sections.append(f'<h2 style="{_STYLE_H2}">🌙 Daily Digest — {safe_date}</h2>')
    sections.append(
        f'<p style="{_STYLE_TALLY}">{_html_escape(_signal_tally(status))}</p>'
    )

    for ticker in watchlist:
        s = status.get(ticker)
        ticker_h = _html_escape(ticker)
        if s == "cancelled":
            sections.append(
                f'<div style="{_STYLE_ROW_DIM}">⛔ <b>{ticker_h}</b> — cancelled</div>'
            )
            continue
        if not isinstance(s, dict):
            sections.append(
                f'<div style="{_STYLE_ROW_DIM}">❓ <b>{ticker_h}</b> — error</div>'
            )
            continue

        signal = (s.get("signal") or "").upper().strip()
        emoji = DECISION_EMOJI.get(signal, "•")
        signal_h = _html_escape(signal or "—")
        chart_url = finviz_chart_url(ticker)
        chart_url_h = _html_escape(chart_url, quote=True)
        telegraph_url = s.get("telegraph_url")

        section_parts = [
            f'<div style="{_STYLE_SECTION}">',
            f'<h3 style="{_STYLE_H3}">{emoji} <b>{ticker_h}</b> — {signal_h}</h3>',
            f'<img src="{chart_url_h}" alt="{ticker_h} chart" style="{_STYLE_IMG}" />',
        ]
        if telegraph_url:
            url_h = _html_escape(telegraph_url, quote=True)
            section_parts.append(
                f'<p style="{_STYLE_LINK_P}">'
                f'<a href="{url_h}" style="{_STYLE_LINK}">'
                f"📰 Full analysis on Telegraph</a></p>"
            )
        section_parts.append("</div>")
        sections.append("".join(section_parts))

    if skipped_closed:
        skipped_h = ", ".join(_html_escape(t) for t in skipped_closed)
        sections.append(
            f'<p style="{_STYLE_FOOTNOTE}">⚫️ Skipped (markets closed): {skipped_h}</p>'
        )

    sections.append(
        f'<p style="{_STYLE_FOOTER}">Sent by your TradingAgents-Telegram bot.</p>'
    )

    body = "".join(sections)
    return (
        '<html><body style="margin:0;padding:16px;max-width:680px;">'
        f"{body}</body></html>"
    )


def _build_attachments(watchlist: list[str], status: dict, date_iso: str) -> list[dict]:
    """Build base64-encoded `.md` attachments for each completed ticker.

    Reads `final_state` from tradingagents' on-disk JSON logs via
    `load_historical_state` — same source `/history` and the
    `📥 Download Full Report` button use. This decouples email
    attachment-building from the digest's `status` dict (which only
    carries the signal + Telegraph URL, not the full state).

    A missing log file (ephemeral Docker FS without persistent
    `TRADINGAGENTS_RESULTS_DIR`) skips that ticker's attachment with a
    log line; the email still sends with the rest of the attachments.
    """
    attachments: list[dict] = []
    for ticker in watchlist:
        s = status.get(ticker)
        if not isinstance(s, dict):
            continue  # skip cancelled/failed
        try:
            state = load_historical_state(ticker, date_iso)
            if state is None:
                logger.info(
                    "email: no log file for %s on %s, skipping attachment",
                    ticker,
                    date_iso,
                )
                continue
            from datetime import date as _date

            try:
                gen_date = _date.fromisoformat(date_iso)
            except ValueError:
                gen_date = None
            md = format_full_md_report(ticker, state, generated_at=gen_date)
            attachments.append(
                {
                    "filename": f"{ticker}_{date_iso}.md",
                    "content": base64.b64encode(md.encode("utf-8")).decode("ascii"),
                }
            )
        except Exception as e:
            logger.warning("email: failed to build attachment for %s: %s", ticker, e)
    return attachments


async def send_digest_email(
    to_addr: str,
    watchlist: list[str],
    status: dict,
    safe_date: str,
    date_iso: str,
    skipped_closed: list[str] | None = None,
) -> EmailSendResult:
    """Send the digest summary email. Returns an `EmailSendResult` —
    never raises to the caller, since email is a mirror and must NEVER
    break the Telegram path.

    Caller is responsible for the opt-in gate (`digest.email` is set)
    AND the env-configured gate (`is_email_configured()`); this function
    assumes both are checked. We re-check the env here as a defensive
    second line so a hot-removed env var doesn't crash on the Resend
    SDK's internal validation.
    """
    if not is_email_configured():
        logger.warning(
            "send_digest_email: RESEND_API_KEY/RESEND_FROM not set, skipping send to %s",
            to_addr,
        )
        return EmailSendResult(ok=False, recipient=to_addr, error="not_configured")

    # Cheap pure-Python work stays on the loop thread.
    subject = _build_subject(safe_date, status)
    html = _build_html(watchlist, status, safe_date, skipped_closed)

    # Lazy import — keep the dep import cost out of cold-start when the
    # operator never uses email. The dep is in pyproject.toml as a hard
    # requirement, so the import won't ImportError in practice.
    import resend

    api_key = os.environ["RESEND_API_KEY"]
    from_addr = os.environ["RESEND_FROM"]

    def _send_blocking() -> tuple[object, int]:
        # Everything blocking happens here:
        #   1. Disk I/O for `.md` attachment build (per-ticker
        #      `load_historical_state` reads the on-disk JSON log) —
        #      ~5-50ms per ticker, O(N) on the fan-out.
        #   2. Network I/O via `resend.Emails.send` — HTTPS POST to
        #      Resend's API, typically ~50-300ms.
        # Both are kept in the same `to_thread` call so the digest
        # fan-out can't stall concurrent /watch handlers, cancel
        # taps, or progress-edit coroutines on the event loop while
        # the email is building + going on the wire. Setting
        # `resend.api_key` (a module-level global) inside this thread
        # is safe because we run one send at a time (one per digest
        # per user, serialized through this coroutine).
        # Returns (resend_result, attachment_count) so the caller can
        # log the attachment count without re-running _build_attachments
        # back on the loop thread.
        resend.api_key = api_key
        attachments = _build_attachments(watchlist, status, date_iso)
        payload = {
            "from": from_addr,
            "to": [to_addr],
            "subject": subject,
            "html": html,
        }
        if attachments:
            payload["attachments"] = attachments
        return resend.Emails.send(payload), len(attachments)

    try:
        result, n_attachments = await asyncio.to_thread(_send_blocking)
        # Resend SDK returns a dict like {"id": "re_..."} on success.
        if isinstance(result, dict) and result.get("id"):
            logger.info(
                "email: sent digest to %s (id=%s, %d attachments)",
                to_addr,
                result["id"],
                n_attachments,
            )
            return EmailSendResult(ok=True, recipient=to_addr, message_id=result["id"])
        logger.warning(
            "email: unexpected Resend response shape for %s: %r", to_addr, result
        )
        return EmailSendResult(ok=False, recipient=to_addr, error="malformed_response")
    except Exception as e:
        logger.warning(
            "email: send to %s failed (%s) — Telegram digest unaffected",
            to_addr,
            type(e).__name__,
        )
        logger.debug("email send full traceback:", exc_info=True)
        return EmailSendResult(ok=False, recipient=to_addr, error=type(e).__name__)
