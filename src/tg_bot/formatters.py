"""Pure formatting helpers — no I/O, no globals."""

import html as _html_lib
import re
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import Optional

import markdown as _markdown_lib


_MD_V2_URL_ESCAPE = re.compile(r"([\\)])")


def escape_md_v2_url(url: str) -> str:
    """Escape `\\` and `)` for safe inclusion inside a MarkdownV2 link
    target `[text](url)`. Other characters don't need escaping per
    Telegram's spec, so we deliberately leave them alone."""
    return _MD_V2_URL_ESCAPE.sub(r"\\\1", url)


# ─── HTML sanitization for Telegram captions ─────────────────────────────
#
# The analysis-output captions (format_short_message, /history republish,
# digest summary) run LLM markdown through markdown.markdown(...) and then
# this sanitizer so the output stays within Telegram's HTML whitelist.
# Telegram silently rejects messages containing tags it doesn't recognize
# ("Bad Request: can't parse entities"), so the sanitizer is load-bearing.
#
# Telegram's allowed HTML tags (per core.telegram.org/bots/api#html-style):
#   <b>, <strong>, <i>, <em>, <u>, <ins>, <s>, <strike>, <del>,
#   <a href="…">, <code>, <pre>, <blockquote>, <blockquote expandable>,
#   <span class="tg-spoiler">, <tg-spoiler>, <tg-emoji emoji-id="…">
#
# Anything else (h1-h6, p, br, hr, ul, ol, li, table, …) must be either
# stripped or converted to allowed equivalents before sending.

_TELEGRAM_INLINE_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "code",
}
_TELEGRAM_BLOCK_TAGS = {"pre", "blockquote", "tg-spoiler"}
_TAG_REWRITE = {"strong": "b", "em": "i", "ins": "u", "strike": "s", "del": "s"}
# Tags whose content gets dropped entirely (not just the tag): tables blow
# the caption budget and don't render usefully inline; images aren't
# expressible in a text caption at all. Users see these via Telegraph.
_DROP_WITH_CONTENT = {"table", "img"}


class _TelegramHtmlSanitizer(HTMLParser):
    """Walk HTML via stdlib html.parser and rebuild a Telegram-safe string.

    Strategy:
      - Allowed inline tags pass through (`strong`→`b`, etc.).
      - Headers `<h1>`-`<h6>` become `<b>…</b>` + blank line.
      - `<p>`/`<br>` drop the tag but emit appropriate newlines.
      - `<hr>` emits a blank line.
      - `<ul>`/`<ol>`/`<li>` flatten to bulleted lines (`• `).
      - `<table>` and `<img>` are dropped with their content (see
        `_DROP_WITH_CONTENT`). Captions have a 1024-char budget and
        tables don't render usefully inline; users get them in Telegraph.
      - `<a href="…">` passes through with the href re-escaped via html.escape.
      - `<blockquote>` and `<blockquote expandable>` pass through (the
        latter is what powers the collapsible analysis summary).
      - Unknown tags: tag dropped, inner text preserved (never silently
        lose content).
      - Inner text is re-escaped via html.escape (the parser decodes entities
        as it walks, so emitting raw would corrupt `<`/`&`/etc).
    """

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        # Increment when we enter a tag whose entire subtree is dropped
        # (tables, images). All data + nested tags are suppressed until
        # the matching close decrements back to 0.
        self._skip_depth = 0

    def _emit_block_break(self, count: int = 2) -> None:
        # Don't stack multiple blank lines on top of each other.
        existing = "".join(self._parts[-3:])
        trailing_nl = len(existing) - len(existing.rstrip("\n"))
        needed = max(0, count - trailing_nl)
        if needed:
            self._parts.append("\n" * needed)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in _DROP_WITH_CONTENT or self._skip_depth > 0:
            if tag in _DROP_WITH_CONTENT:
                self._skip_depth += 1
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("<b>")
            return
        if tag == "p":
            return
        if tag == "br":
            self._emit_block_break(1)
            return
        if tag == "hr":
            self._emit_block_break(2)
            return
        if tag in {"ul", "ol"}:
            self._emit_block_break(1)
            return
        if tag == "li":
            self._parts.append("• ")
            return
        if tag == "a":
            href = next((v for k, v in attrs if k == "href" and v), None)
            if not href:
                # bare <a>; drop the tag, keep inner text
                return
            self._parts.append(f'<a href="{_html_lib.escape(href, quote=True)}">')
            return
        if tag == "blockquote":
            expandable = any(k == "expandable" for k, _ in attrs)
            self._parts.append(
                "<blockquote expandable>" if expandable else "<blockquote>"
            )
            return
        out = _TAG_REWRITE.get(tag, tag)
        if out in _TELEGRAM_INLINE_TAGS or out in _TELEGRAM_BLOCK_TAGS:
            self._parts.append(f"<{out}>")
            return
        # Unknown tag — drop, preserve children.

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth > 0:
            if tag in _DROP_WITH_CONTENT:
                self._skip_depth -= 1
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("</b>")
            self._emit_block_break(2)
            return
        if tag == "p":
            self._emit_block_break(2)
            return
        if tag in {"br", "hr"}:
            return
        if tag in {"ul", "ol"}:
            self._emit_block_break(1)
            return
        if tag == "li":
            self._parts.append("\n")
            return
        if tag == "a":
            # Close only if we actually opened a tag (skipped bare <a>).
            if self._parts and self._parts[-1].startswith('<a href="'):
                # nothing was emitted inside — drop the unclosed open tag
                self._parts.pop()
                return
            self._parts.append("</a>")
            return
        if tag == "blockquote":
            self._parts.append("</blockquote>")
            return
        out = _TAG_REWRITE.get(tag, tag)
        if out in _TELEGRAM_INLINE_TAGS or out in _TELEGRAM_BLOCK_TAGS:
            self._parts.append(f"</{out}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        # `html.parser` decodes entities as it walks (so `&amp;` arrives
        # as `&`). We must re-escape on emit to avoid corrupting captions.
        self._parts.append(_html_lib.escape(data, quote=False))

    def result(self) -> str:
        # Collapse runs of >2 consecutive newlines that crept in across
        # block boundaries.
        joined = "".join(self._parts)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


def sanitize_html_for_telegram(html_in: str) -> str:
    """Strip / rewrite HTML so only Telegram's allowed tag set remains.

    Idempotent on already-clean input. Used by `markdown_to_telegram_html`
    to ensure LLM-produced markdown is renderable in Telegram captions
    (and by callers that already hold HTML)."""
    parser = _TelegramHtmlSanitizer()
    parser.feed(html_in)
    parser.close()
    return parser.result()


def markdown_to_telegram_html(text: str) -> str:
    """Convert markdown to Telegram-flavor HTML.

    Pipeline: `markdown.markdown(text, extensions=["tables"])` produces
    full HTML (with `<h1>`, `<table>`, `<p>`, `<ul>`, etc.); the
    sanitizer rewrites that to Telegram's allowed tag set."""
    if not text:
        return ""
    rendered = _markdown_lib.markdown(text, extensions=["tables"])
    return sanitize_html_for_telegram(rendered)


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
    rendered into the caption as `via <summary>`.

    Layout: `<provider> · <deep>/<quick> · r{n} · e={level}`. Provider
    and the deep/quick model pair are always shown so users can tell
    at a glance what produced the result; `r{n}` and `e={level}` are
    appended only when the user customized those knobs (rounds != 1
    or effort set), so the line stays tight for the common case."""
    # Late import to avoid a load-time cycle (analysis → formatters via
    # config-rendering helpers). EFFORT_KEY_BY_PROVIDER is the single
    # source of truth for the per-provider key vocabulary.
    from tg_bot.analysis import EFFORT_KEY_BY_PROVIDER

    provider = config.get("llm_provider", "unknown")
    deep = config.get("deep_think_llm", "default")
    quick = config.get("quick_think_llm", "default")
    parts = [provider, f"{deep}/{quick}"]
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


def format_analysis_result_markdown(
    ticker: str,
    final_state: dict,
    signal: str,
    config_summary: str | None = None,
    generated_at: date | datetime | None = None,
) -> str:
    """Markdown body for the Telegraph page.

    Telegraph URLs are public-by-default and frequently shared bare; the
    in-Telegram caption carries the `via <provider>...` config trace but
    the Telegraph page itself was contextless. When `config_summary` or
    `generated_at` is provided, prepend a blockquote header with the
    timestamp and config so readers of a shared link can tell which model
    + run produced the analysis.

    `generated_at` accepts `datetime` for fresh runs (full UTC stamp) and
    `date` for /history republishes (date only — historical logs don't
    record the analysis time of day). datetime is checked first because
    it's a subclass of date.

    Currently emits final_trade_decision + trader_investment_plan; other
    final_state sections (analyst reports, debate verdicts) can be
    appended later.
    """
    header = ""
    if config_summary or generated_at is not None:
        bits = []
        if isinstance(generated_at, datetime):
            bits.append(f"Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')}")
        elif isinstance(generated_at, date):
            bits.append(f"Generated {generated_at.isoformat()}")
        if config_summary:
            bits.append(config_summary)
        # `---` is a hard separator: without it, a body whose first line
        # starts with `> ` (LLM agents occasionally quote a thesis or risk
        # warning) gets pulled into the header blockquote across the
        # blank line, fusing them visually. The horizontal rule renders
        # as <hr/> in Telegraph and forces a fresh blockquote for the
        # body's `> ` lines.
        header = f"> {' · '.join(bits)}\n\n---\n\n"
    return (
        f"{header}"
        f"{final_state.get('final_trade_decision', 'N/A')}\n\n"
        f"{final_state.get('trader_investment_plan', '')}\n"
    )


def extract_summary(decision_text: str, max_len: int = 700) -> str:
    """Word-boundary preview of the decision text — first `max_len` chars,
    clamped at the last space so the slice doesn't end mid-word.

    Default 700 sized for HTML-mode captions: the summary is wrapped in a
    `<blockquote expandable>` inside a 1024-char-budget photo caption.
    After fixed overhead (signal line, timestamp, config trace, Telegraph
    link, blockquote tags) and ~10-15% HTML escaping bloat from
    `markdown_to_telegram_html`, 700 raw-markdown chars lands the caption
    comfortably under the limit.

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
    """HTML caption shown alongside the chart in the Telegram message.

    **HTML, not MarkdownV2** — call sites must pass `parse_mode="HTML"`.
    The caption used to be MarkdownV2 but LLM agents emit markdown
    (`**bold**`, `## headers`, `[links](url)`) that `escape_markdown`
    flattened to literal characters; users saw raw `**Bear Case**`
    instead of formatted text. HTML mode lets us convert the markdown
    via `markdown_to_telegram_html` so the inline formatting renders.

    The summary is wrapped in `<blockquote expandable>`: collapsed
    preview is ~3 lines on mobile, tap to expand to ~700 chars of
    formatted rationale (bold/italic/links/code).

    `generated_at` defaults to "now" for fresh runs. Cache-hit paths
    pass the original analysis time so users see when the cached
    decision was made, not the moment they tapped.

    `config_summary` (optional) renders a one-line trace of the LLM
    config — provider/deep-model plus rounds/effort suffix when
    customized via /config.

    All user-supplied strings are escaped via `html.escape`; LLM
    markdown is run through `markdown_to_telegram_html` which whitelists
    Telegram's allowed tag set and drops everything else.
    """
    emoji = DECISION_EMOJI.get(signal.strip().upper(), "📊")
    safe_ticker = _html_lib.escape(ticker)
    safe_signal = _html_lib.escape(signal)
    when = generated_at if generated_at else datetime.now(timezone.utc)
    timestamp = _html_lib.escape(when.strftime("%Y-%m-%d %H:%M UTC"))

    parts = [f"{emoji} <b>{safe_ticker}</b> — <b>{safe_signal}</b>", ""]
    if summary:
        # Run the (markdown) summary through markdown→HTML + Telegram-
        # whitelist sanitizer, then wrap in expandable blockquote.
        summary_html = markdown_to_telegram_html(summary)
        if summary_html:
            parts.append(f"<blockquote expandable>{summary_html}</blockquote>")
            parts.append("")
    parts.append(f"<i>Generated {timestamp}</i>")
    if config_summary:
        parts.append(f"<i>via {_html_lib.escape(config_summary)}</i>")
    parts.append("")

    if telegraph_url:
        href = _html_lib.escape(telegraph_url, quote=True)
        parts.append(f'📄 <a href="{href}">View Full Report</a>')
    else:
        parts.append("⚠️ Full report unavailable (Telegraph publish failed).")

    return "\n".join(parts)
