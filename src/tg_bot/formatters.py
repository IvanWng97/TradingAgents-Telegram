"""Pure formatting helpers — no I/O, no globals."""

from datetime import datetime, timezone

from telegram.helpers import escape_markdown


# MarkdownV2-aware emoji prefix per decision verb.
_DECISION_EMOJI = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡"}


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


def format_short_message(
    ticker: str, signal: str, telegraph_url: str | None = None
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
        f"_Generated {timestamp}_",
        "",
    ]

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
