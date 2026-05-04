"""Pure formatting helpers — no I/O, no globals."""

from datetime import datetime, timezone

from telegram.helpers import escape_markdown


# MarkdownV2-aware emoji prefix per decision verb.
_DECISION_EMOJI = {
    "BUY": "🟢",
    "OVERWEIGHT": "🟩",
    "HOLD": "🟡",
    "UNDERWEIGHT": "🟥",
    "SELL": "🔴",
}


def format_analysis_result_markdown(ticker: str, final_state: dict, signal: str) -> str:
    """Markdown body for the Telegraph page.

    Currently emits final_trade_decision + trader_investment_plan; other
    final_state sections (analyst reports, debate verdicts) can be appended
    later.
    """
    return (
        f"{final_state.get('final_trade_decision', 'N/A')}\n\n"
        f"{final_state.get('trader_investment_plan', '')}\n"
    )


def extract_summary(decision_text: str, max_len: int = 280) -> str:
    """Pull the lead paragraph out of `final_trade_decision` for the caption.

    Agents reliably lead with the verdict-rationale paragraph; clipping the
    first paragraph to ~280 chars captures the substance without bloating the
    caption (Telegram caps photo captions at 1024 chars). Returns "" if the
    input is empty.
    """
    if not decision_text:
        return ""
    paragraphs = [p.strip() for p in decision_text.split("\n\n") if p.strip()]
    first = paragraphs[0] if paragraphs else decision_text.strip()
    if len(first) <= max_len:
        return first
    return first[:max_len].rstrip() + "…"


def format_short_message(
    ticker: str,
    signal: str,
    telegraph_url: str | None = None,
    summary: str | None = None,
) -> str:
    """MarkdownV2 caption shown alongside the chart in the Telegram message.

    All user-supplied text is escaped via `telegram.helpers.escape_markdown`
    so a stray `.` `-` `!` `(` doesn't break parsing.
    """
    emoji = _DECISION_EMOJI.get(signal.strip().upper(), "📊")
    safe_ticker = escape_markdown(ticker, version=2)
    safe_signal = escape_markdown(signal, version=2)
    timestamp = escape_markdown(
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), version=2
    )

    lines = [
        f"{emoji} *{safe_ticker}* — *{safe_signal}*",
        "",
    ]
    if summary:
        lines.append(escape_markdown(summary, version=2))
        lines.append("")
    lines.append(f"_Generated {timestamp}_")
    lines.append("")

    if telegraph_url:
        # In MarkdownV2 link URLs only ')' and '\' need escaping; Telegraph
        # paths are alphanumeric, so the replace is normally a no-op.
        safe_url = telegraph_url.replace("\\", "\\\\").replace(")", "\\)")
        lines.append(f"📄 [View Full Report]({safe_url})")
    else:
        lines.append(
            "⚠️ Full report unavailable "
            + escape_markdown("(Telegraph publish failed).", version=2)
        )

    return "\n".join(lines)
