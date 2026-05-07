"""Pure formatting helpers — no I/O, no globals."""

import re
from datetime import datetime, timezone

from telegram.helpers import escape_markdown


_MD_V2_URL_ESCAPE = re.compile(r"([\\)])")


def escape_md_v2_url(url: str) -> str:
    """Escape `\\` and `)` for safe inclusion inside a MarkdownV2 link
    target `[text](url)`. Other characters don't need escaping per
    Telegram's spec, so we deliberately leave them alone."""
    return _MD_V2_URL_ESCAPE.sub(r"\\\1", url)


# MarkdownV2-aware emoji prefix per decision verb. Public — reused by the
# digest summary so the per-ticker rows match the manual-analysis caption.
DECISION_EMOJI = {
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
    """Word-boundary preview of the decision text — first `max_len` chars,
    clamped at the last space so the slice doesn't end mid-word.

    Agents emit `final_trade_decision` in inconsistent formats (sometimes
    leading with a markdown header, sometimes a field list, sometimes prose).
    Heuristic-based extraction (skip-headers, find-first-sentence) produced
    unpredictable output across runs. A flat slice is honest: caption shows
    the LLM's actual opening text, user clicks through to the Telegraph link
    for the full version.
    """
    if not decision_text:
        return ""
    text = decision_text.strip()
    if len(text) <= max_len:
        return text
    # Back up to the last space so we don't end mid-word. `rsplit(" ", 1)`
    # returns the whole slice when there's no space (single long token),
    # which falls through cleanly to a verbatim cut + ellipsis.
    return text[:max_len].rsplit(" ", 1)[0].rstrip() + "…"


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
    emoji = DECISION_EMOJI.get(signal.strip().upper(), "📊")
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
        lines.append(f"📄 [View Full Report]({escape_md_v2_url(telegraph_url)})")
    else:
        lines.append(
            "⚠️ Full report unavailable "
            + escape_markdown("(Telegraph publish failed).", version=2)
        )

    return "\n".join(lines)
