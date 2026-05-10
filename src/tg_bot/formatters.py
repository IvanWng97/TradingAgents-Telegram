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


def build_config_summary(config: dict) -> str:
    """One-liner describing the LLM config that produced an analysis —
    rendered into the caption as `via <summary>`. Returns the bare
    `provider/deep_model` for default runs; appends `· r{n}` only when
    rounds != 1 and `· e={level}` only when an effort is set, so the
    line stays tight for the common case."""
    # Late import to avoid a load-time cycle (analysis → formatters via
    # config-rendering helpers). EFFORT_KEY_BY_PROVIDER is the single
    # source of truth for the per-provider key vocabulary.
    from tg_bot.analysis import EFFORT_KEY_BY_PROVIDER

    parts = [
        f"{config.get('llm_provider', 'unknown')}/"
        f"{config.get('deep_think_llm', 'default')}"
    ]
    rounds = config.get("max_debate_rounds", 1)
    if rounds != 1:
        parts.append(f"r{rounds}")
    # Effort lives under different keys per provider; pick whichever is set.
    for key in EFFORT_KEY_BY_PROVIDER.values():
        v = config.get(key)
        if v:
            parts.append(f"e={v}")
            break
    return " · ".join(parts)


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
    config_summary: str | None = None,
    generated_at: datetime | None = None,
) -> str:
    """MarkdownV2 caption shown alongside the chart in the Telegram message.

    `generated_at` defaults to "now" for fresh runs. Cache-hit paths pass
    the original analysis time so users see when the cached decision was
    actually made, not the moment they tapped the button.

    `config_summary` (optional) renders a one-line trace of the LLM
    config that produced this analysis — provider/deep-model plus
    rounds/effort suffix when the user customized them via /config.

    All user-supplied text is escaped via `telegram.helpers.escape_markdown`
    so a stray `.` `-` `!` `(` doesn't break parsing.
    """
    emoji = DECISION_EMOJI.get(signal.strip().upper(), "📊")
    safe_ticker = escape_markdown(ticker, version=2)
    safe_signal = escape_markdown(signal, version=2)
    when = generated_at if generated_at else datetime.now(timezone.utc)
    timestamp = escape_markdown(when.strftime("%Y-%m-%d %H:%M UTC"), version=2)

    lines = [
        f"{emoji} *{safe_ticker}* — *{safe_signal}*",
        "",
    ]
    if summary:
        lines.append(escape_markdown(summary, version=2))
        lines.append("")
    lines.append(f"_Generated {timestamp}_")
    if config_summary:
        lines.append(f"_via {escape_markdown(config_summary, version=2)}_")
    lines.append("")

    if telegraph_url:
        lines.append(f"📄 [View Full Report]({escape_md_v2_url(telegraph_url)})")
    else:
        lines.append(
            "⚠️ Full report unavailable "
            + escape_markdown("(Telegraph publish failed).", version=2)
        )

    return "\n".join(lines)
