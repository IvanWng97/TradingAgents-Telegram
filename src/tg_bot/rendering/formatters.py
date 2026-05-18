"""Pure formatting helpers — no I/O, no globals."""

import html as _html_lib
import re
from datetime import date, datetime, UTC
from html.parser import HTMLParser

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
        # Track <a> tags that were opened without an href and silently
        # dropped. The matching </a> must also be dropped — otherwise a
        # bare `<a>foo</a>` from LLM-emitted raw HTML produces a dangling
        # `</a>` in the output and Telegram returns
        # `Bad Request: can't parse entities`.
        self._bare_anchor_depth = 0

    def _emit_block_break(self, count: int = 2) -> None:
        # Don't stack multiple blank lines on top of each other.
        existing = "".join(self._parts[-3:])
        trailing_nl = len(existing) - len(existing.rstrip("\n"))
        needed = max(0, count - trailing_nl)
        if needed:
            self._parts.append("\n" * needed)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
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
                # Bare <a>; drop the tag, keep inner text. Bump the depth
                # counter so the matching </a> also gets dropped — otherwise
                # we'd emit a dangling close tag that Telegram rejects.
                self._bare_anchor_depth += 1
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
            # If the matching open was a bare `<a>` that we silently
            # dropped, drop this close too. Without this, a bare
            # `<a>foo</a>` from LLM-emitted raw HTML produces `foo</a>`
            # and Telegram rejects it with "can't parse entities".
            if self._bare_anchor_depth > 0:
                self._bare_anchor_depth -= 1
                return
            # Empty link (no inner content between open + close): drop
            # the unclosed open tag rather than emitting `<a href="...">`
            # followed immediately by `</a>`.
            if self._parts and self._parts[-1].startswith('<a href="'):
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


# Report sections in decision-relevance order. The Telegraph packer adds
# sections from the top until the rendered HTML approaches the 40,000-char
# internal budget (see `_TELEGRAPH_HTML_BUDGET` below); the full .md
# attachment emits all of them unconditionally. Keys map to
# `final_state` fields populated by tradingagents.
_REPORT_SECTIONS: list[tuple[str, str]] = [
    ("Final Trading Decision", "final_trade_decision"),
    ("Trader's Recommendation", "trader_investment_plan"),
    ("Research Manager Synthesis", "investment_plan"),
    ("Market Analysis", "market_report"),
    ("Fundamentals", "fundamentals_report"),
    ("News", "news_report"),
    ("Sentiment", "sentiment_report"),
]

# Telegraph documents a 65,536 cap on submitted HTML — but the SDK
# internally parses HTML into a JSON Node tree (every element becomes
# `{"tag": "...", "children": [...]}`), and Telegraph's CONTENT_TOO_BIG
# is enforced on *that* serialized size, not the raw HTML chars we send.
# JSON wrapping + whitespace preservation + the embedded chart `<img>`
# typically inflate the cleaned HTML 25–40% on Telegraph's side, so the
# documented 65 KB cap maps to roughly 45–50 KB of our HTML chars.
#
# Empirical: INTU 2026-05-11 was rejected with CONTENT_TOO_BIG at
# 52,991 cleaned HTML chars under the previous 64 KB budget — comfortably
# under the documented cap but evidently over the practical one. The
# tightened budget below forces the packer to drop a section earlier
# rather than ship oversized content and get hard-rejected.
_TELEGRAPH_HTML_BUDGET = 40000  # ~25-40% headroom for Telegraph's Node-tree inflation


def _build_header(
    config_summary: str | None, generated_at: date | datetime | None
) -> str:
    """The `> Generated … · <config>` blockquote prepended to both the
    Telegraph body and the full .md attachment so a recipient sharing
    either format can tell who/when/what produced the analysis."""
    if not (config_summary or generated_at is not None):
        return ""
    bits = []
    if isinstance(generated_at, datetime):
        bits.append(f"Generated {generated_at.strftime('%Y-%m-%d %H:%M UTC')}")
    elif isinstance(generated_at, date):
        bits.append(f"Generated {generated_at.isoformat()}")
    if config_summary:
        bits.append(config_summary)
    # `---` is a hard separator: without it, a body whose first line
    # starts with `> ` (LLM agents occasionally quote a thesis or risk
    # warning) gets pulled into the header blockquote across the blank
    # line, fusing them visually. The horizontal rule renders as <hr/>
    # in Telegraph and forces a fresh blockquote for the body's `> ` lines.
    return f"> {' · '.join(bits)}\n\n---\n\n"


def _is_numbered_list_start(line: str) -> bool:
    """True if `line` begins with the `\\d+. ` marker that opens a markdown
    ordered-list item. Plain-Python (no regex) check: leading digits,
    one literal dot, one literal space."""
    i = 0
    while i < len(line) and line[i].isdigit():
        i += 1
    return i > 0 and i + 1 < len(line) and line[i] == "." and line[i + 1] == " "


def _is_unindented_bullet(line: str) -> bool:
    """True if `line` is `- foo` or `* foo` at column 0 — the LLM-emitted
    shape that needs 4-space padding to nest properly under an `<ol>`."""
    return len(line) >= 2 and line[0] in "-*" and line[1] == " "


def _normalize_nested_bullets(md: str) -> str:
    """Indent sub-bullets that the LLM emitted at column 0 directly under
    a numbered-list item so `markdown.markdown` produces `<ol><li><ul><li>…`
    instead of promoting the bullets to siblings of the parent `<ol>`.

    Python-Markdown needs **4-space** indent for proper nesting under an
    ordered list; 3 spaces flattens. The LLM very often emits the
    intuitive-but-wrong:

        4. Monitor for catalysts:
        - First catalyst
        - Second catalyst

    State machine: after a `\\d+. ` line, any column-0 `- `/`* ` lines get
    padded to 4 spaces. Blank lines preserve the state (loose list). A
    non-blank, non-bullet, non-numbered line resets — sub-list is over.

    Pattern-pinned to the high-confidence case (col-0 bullet directly
    after numbered context); won't touch bullets that were already
    correctly indented or top-level bullets in paragraphs that aren't
    preceded by an OL.
    """
    out: list[str] = []
    inside_ol = False
    for line in md.splitlines():
        if _is_numbered_list_start(line):
            inside_ol = True
            out.append(line)
        elif inside_ol and _is_unindented_bullet(line):
            out.append("    " + line)
        elif line.strip() == "":
            out.append(line)
        else:
            inside_ol = False
            out.append(line)
    return "\n".join(out)


def _iter_section_blocks(final_state: dict) -> list[tuple[str, str]]:
    """Yield (title, content) pairs from `_REPORT_SECTIONS`, skipping
    empty/missing fields. Used by both the Telegraph packer and the full
    .md report so section order/labels stay in lockstep.

    Each section's content runs through `_normalize_nested_bullets` so
    LLM-emitted unindented sub-bullets under numbered items render as
    proper nested `<ul>` instead of getting promoted to `<ol>` siblings.
    """
    blocks: list[tuple[str, str]] = []
    for title, key in _REPORT_SECTIONS:
        value = final_state.get(key) or ""
        value = value.strip()
        if value:
            blocks.append((title, _normalize_nested_bullets(value)))
    return blocks


def _assemble_report(header: str, blocks: list[tuple[str, str]]) -> str:
    """Concatenate the header + each (title, content) block as `## Title`
    sections separated by blank lines."""
    parts = []
    if header:
        parts.append(header)
    for title, content in blocks:
        parts.append(f"## {title}\n\n{content}\n")
    return "\n".join(parts)


def format_analysis_result_markdown(
    ticker: str,
    final_state: dict,
    signal: str,
    config_summary: str | None = None,
    generated_at: date | datetime | None = None,
) -> str:
    """Markdown body for the Telegraph page.

    Packs analyst sections in priority order (`_REPORT_SECTIONS`) until
    the rendered HTML approaches Telegraph's 65,536-char cap, then stops
    — *drops* the would-overflow section rather than truncating it (the
    full content is available in the .md document attachment, so partial
    sections in Telegraph just create the impression that something was
    cut off). The dropped sections are still in `final_state` and visible
    via `format_full_md_report`.

    Header is prepended when `config_summary` or `generated_at` is set so
    a shared Telegraph URL self-documents who/when/what (datetime gets a
    full UTC stamp; date gets day-only — historical logs don't record
    time of day).
    """
    header = _build_header(config_summary, generated_at)
    blocks = _iter_section_blocks(final_state)
    if not blocks:
        # Edge case: empty final_state. Preserve the header so the
        # Telegraph page isn't completely empty.
        return header or "_(no analysis content)_"

    # Pack from the top, dropping trailing sections that push the
    # rendered HTML over budget. Conversion isn't free but only runs
    # once per analysis; the typical case (all 7 sections + tables) is
    # 72k HTML, ~8k over — one drop iteration usually suffices.
    for limit in range(len(blocks), 0, -1):
        md = _assemble_report(header, blocks[:limit])
        rendered = _markdown_lib.markdown(md, extensions=["tables"])
        if len(rendered) <= _TELEGRAPH_HTML_BUDGET:
            return md
    # Even the first section alone exceeds the cap — extremely unlikely,
    # but fall through to first section as-is rather than returning an
    # empty body. Telegraph will reject if it's truly too large, and
    # `publish_to_telegraph` already logs/handles the failure.
    return _assemble_report(header, blocks[:1])


def format_full_md_report(
    ticker: str,
    final_state: dict,
    config_summary: str | None = None,
    generated_at: date | datetime | None = None,
) -> str:
    """Full markdown report for the `.md` document attachment.

    No size cap — every section in `_REPORT_SECTIONS` that's present and
    non-empty gets emitted. This is the archival "give me everything"
    artifact users get alongside the photo + caption + Telegraph link.
    Mirrors the priority order of the Telegraph packer so when users
    compare the two, the on-Telegraph subset is the leading prefix of
    the full .md.
    """
    header = _build_header(config_summary, generated_at)
    return _assemble_report(header, _iter_section_blocks(final_state))


def _strip_final_decision_header(text: str) -> str:
    """Drop the leading `**Final Trading Decision: <T>**` and
    `**Rating: <X>**` boilerplate from `final_trade_decision` — those
    lines duplicate the signal badge + ticker that the photo caption
    already shows above the expandable blockquote.

    Pattern-pinned (only the two known prefixes, only at the very top of
    the text, blank lines between them OK) — no regex, no clever "skip
    until first sentence" heuristics. Body content can't be silently
    swallowed because the loop breaks on the first non-matching line,
    so a later `**Rating: …**` mention inside prose stays put."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("**Final Trading Decision") or line.startswith("**Rating:"):
            i += 1
            continue
        break
    return "\n".join(lines[i:]).lstrip()


def _extract_executive_summary(text: str) -> str | None:
    """Pull the `**Executive Summary**` section body out of
    `final_trade_decision`, if present.

    Tradingagents' synthesis output is structured: `**Rating**` →
    `**Executive Summary**` → `**Investment Thesis**` →
    `**Why not Sell/Hold/...**` → `**Decisive evidence anchoring**` →
    `**Price Target**` → `**Time Horizon**`. The Executive Summary is
    the natural TL;DR — concrete actionable guidance (trim/add levels,
    stops, re-entry zones, price targets) that fits a 700-char caption
    natively without dragging in the multi-paragraph counterargument
    sections.

    Handles both header variants the LLM emits inconsistently:
      - `**Executive Summary**:` (colon outside the bold)
      - `**Executive Summary:**` (colon inside the bold)
    Case-insensitive on the title. Body terminates at the next
    `\\n\\n**` (next bold-headed paragraph) or end of text.

    Returns None when the header isn't present so callers can fall
    back to the generic strip-and-clip path — older analyses, custom
    prompts, and providers whose output doesn't conform all degrade
    gracefully."""
    lower = text.lower()
    # Two patterns the structured output uses, normalized to lowercase
    # so we match regardless of capitalization drift.
    for marker in ("**executive summary**:", "**executive summary:**"):
        idx = lower.find(marker)
        if idx == -1:
            continue
        body_start = idx + len(marker)
        # Section terminates at the next bold-headed paragraph
        # (preceded by a blank line) OR the end of text. The blank-line
        # guard avoids false-positive termination on inline `**word**`
        # bolding inside the summary prose.
        end = text.find("\n\n**", body_start)
        body = text[body_start:end] if end != -1 else text[body_start:]
        return body.strip()
    return None


def caption_summary(final_state: dict, max_len: int = 700) -> str:
    """Caption summary clip sourced from `final_trade_decision` (the
    post-risk-debate synthesis — matches the signal badge by
    construction).

    Two extraction paths, in priority order:
      1. **Executive Summary section** (`_extract_executive_summary`) —
         the structured TL;DR with actionable price levels + stops +
         entries. Natural caption content, ~700 chars by convention.
      2. **Strip-and-clip fallback** — drop the redundant
         ticker/rating header, then clip to `max_len`. Catches older
         analyses or custom-prompt outputs that don't conform to the
         structured layout.

    Earlier the source was `trader_investment_plan` (pre-risk-debate),
    which disagreed with the post-risk-debate badge when the risk
    panel tempered the trader's verdict. Switching the source +
    section-anchoring guarantees alignment AND a focused preview."""
    raw = final_state.get("final_trade_decision", "")
    section = _extract_executive_summary(raw)
    if section is not None:
        return extract_summary(section, max_len=max_len)
    return extract_summary(_strip_final_decision_header(raw), max_len=max_len)


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
    customized via `.env`.

    All user-supplied strings are escaped via `html.escape`; LLM
    markdown is run through `markdown_to_telegram_html` which whitelists
    Telegram's allowed tag set and drops everything else.
    """
    emoji = DECISION_EMOJI.get(signal.strip().upper(), "📊")
    safe_ticker = _html_lib.escape(ticker)
    safe_signal = _html_lib.escape(signal)
    when = generated_at if generated_at else datetime.now(UTC)
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

    # When Telegraph publishing fails, callers still set up the keyboard
    # (download-only — `_full_report_keyboard` drops the IV button if
    # url is None) but the user has no signal that the Telegraph route
    # is unavailable. Surface the warning in the caption so the missing
    # button isn't a silent failure. On success the caption stays clean
    # (no inline `<a href>` line) — the `📰 Instant View` keyboard
    # button is the canonical entry point.
    if not telegraph_url:
        parts.append("")
        parts.append("⚠️ Instant View unavailable (Telegraph publish failed).")

    return "\n".join(parts)
