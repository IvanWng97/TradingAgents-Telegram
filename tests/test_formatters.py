"""Smoke tests for formatters that produce shareable artifacts.

Telegraph URLs are public-by-default and frequently shared without the
in-Telegram caption that carries the config trace. The
`format_analysis_result_markdown` header (added per user request: shared
links should self-document which model + run produced the analysis)
prepends a blockquote with `Generated <ts>` + optional config summary.
These scenarios pin that contract.

Run with: pytest tests/test_formatters.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, UTC
from pathlib import Path


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_bot.rendering.formatters import (  # noqa: E402
    _normalize_nested_bullets,
    _strip_final_decision_header,
    caption_summary,
    escape_md_v2_url,
    format_analysis_result_markdown,
    format_full_md_report,
    format_short_message,
    markdown_to_telegram_html,
    sanitize_html_for_telegram,
)


_STATE = {"final_trade_decision": "HOLD - sample.", "trader_investment_plan": "plan"}


def test_fresh_run_prepends_full_header() -> None:
    out = format_analysis_result_markdown(
        "SOFI",
        _STATE,
        "HOLD",
        config_summary="openai · gpt-4o/o4-mini",
        generated_at=datetime(2026, 5, 10, 12, 34, tzinfo=UTC),
    )
    assert out.startswith(
        "> Generated 2026-05-10 12:34 UTC · openai · gpt-4o/o4-mini\n\n---\n\n"
    ), out
    assert "HOLD - sample." in out


def test_history_prepends_date_only_header() -> None:
    """date arg (no time) gets a date-only header — historical logs
    don't record analysis time of day."""
    out = format_analysis_result_markdown(
        "SOFI",
        _STATE,
        "historical",
        generated_at=date(2026, 4, 15),
    )
    assert out.startswith("> Generated 2026-04-15\n\n---\n\n"), out
    assert "12:34" not in out  # no time fragment leaked through


def test_config_summary_only_no_timestamp() -> None:
    out = format_analysis_result_markdown(
        "SOFI",
        _STATE,
        "HOLD",
        config_summary="anthropic · claude-sonnet-4 · r2",
    )
    assert out.startswith("> anthropic · claude-sonnet-4 · r2\n\n---\n\n"), out


def test_no_args_no_header() -> None:
    """Backward compat: bare callers (none currently in-tree, but the
    formatter must still accept legacy 3-arg invocations) get no
    blockquote header. Section dividers (`## Title`) are unconditional
    now — they help readers parse a multi-section Telegraph page even
    when no config/timestamp is available."""
    out = format_analysis_result_markdown("SOFI", _STATE, "BUY")
    assert not out.startswith(">"), out  # no blockquote header
    assert out.startswith("## Final Trading Decision\n\nHOLD - sample."), out


def test_datetime_subclass_check_picks_full_format() -> None:
    """datetime is a subclass of date, so isinstance order matters: we
    must check datetime first to get the full HH:MM stamp instead of
    falling through to the date-only branch."""
    dt = datetime(2026, 5, 10, 9, 5, tzinfo=UTC)
    out = format_analysis_result_markdown("SOFI", _STATE, "BUY", generated_at=dt)
    assert "09:05 UTC" in out, out


def test_body_blockquote_lines_stay_separate_from_header() -> None:
    """Hard separator between header and body must prevent body lines
    starting with `> ` (LLM agents occasionally quote a thesis/risk
    warning) from being absorbed into the header blockquote across the
    blank line. Without `---`, markdown.markdown lazy-continues the
    blockquote and fuses the two visually."""
    import markdown

    state = {
        "final_trade_decision": "> Per the Buffett doctrine: hold quality.",
        "trader_investment_plan": "details",
    }
    out = format_analysis_result_markdown(
        "SOFI",
        state,
        "HOLD",
        config_summary="openai · gpt-4o/o4-mini",
        generated_at=datetime(2026, 5, 10, 12, 34, tzinfo=UTC),
    )
    rendered = markdown.markdown(out, extensions=["tables"])
    # Two separate blockquotes (header + body), divided by an <hr/>.
    # If the separator were missing, rendered would have a single
    # <blockquote> containing both header and body paragraphs.
    assert rendered.count("<blockquote>") == 2, rendered
    assert "<hr />" in rendered, rendered


# ─── HTML sanitizer + caption pipeline ─────────────────────────────────


def test_sanitizer_keeps_inline_allowed_tags() -> None:
    """Allowed inline tags pass through (with rewriting of synonyms)."""
    out = sanitize_html_for_telegram(
        "<strong>bold</strong> <em>italic</em> <ins>under</ins> <del>strike</del> <code>c</code>"
    )
    assert "<b>bold</b>" in out, out
    assert "<i>italic</i>" in out, out
    assert "<u>under</u>" in out, out
    assert "<s>strike</s>" in out, out
    assert "<code>c</code>" in out, out


def test_sanitizer_converts_headers_to_bold() -> None:
    out = sanitize_html_for_telegram("<h1>Title</h1><h3>Sub</h3><p>body</p>")
    assert "<b>Title</b>" in out and "<b>Sub</b>" in out, out
    assert "<h1>" not in out and "<h3>" not in out, out


def test_sanitizer_drops_tables_with_content() -> None:
    """Tables blow the caption budget and don't render inline — drop the
    whole subtree, not just the tags."""
    out = sanitize_html_for_telegram(
        "<p>before</p>"
        "<table><thead><tr><th>A</th><th>B</th></tr></thead>"
        "<tbody><tr><td>1</td><td>2</td></tr></tbody></table>"
        "<p>after</p>"
    )
    assert "<table>" not in out and "<tr>" not in out and "<td>" not in out, out
    # Cell contents must NOT leak through (would create orphan text).
    for token in ("A", "B", "1", "2"):
        assert token not in out, f"table content leaked: {token!r} in {out!r}"
    assert "before" in out and "after" in out


def test_sanitizer_flattens_lists_to_bullets() -> None:
    out = sanitize_html_for_telegram("<ul><li>first</li><li>second</li></ul>")
    assert "<ul>" not in out and "<li>" not in out, out
    assert "• first" in out and "• second" in out, out


def test_sanitizer_drops_unknown_tag_preserves_inner_text() -> None:
    """Unknown tags lose the markup but keep their text content — never
    silently drop content that the LLM wrote."""
    out = sanitize_html_for_telegram("Some <unknown>important</unknown> text")
    assert "important" in out, out
    assert "<unknown>" not in out, out


def test_sanitizer_anchor_with_whitespace_child() -> None:
    """`<a href="x"> </a>` (whitespace-only child) must produce a
    well-formed result. A prior review flagged this case as a potential
    dangling-open-tag bug; this scenario pins the actual behavior so a
    future refactor of `handle_endtag` can't regress it. Trace: starttag
    appends `<a href=...>`, handle_data appends `' '`, endtag sees the
    space (not the open tag) at `_parts[-1]` and emits `</a>` via the
    else branch — closed pair, valid HTML."""
    out = sanitize_html_for_telegram('<a href="https://x.test/"> </a>')
    # `<a>` open and `</a>` close present, exactly once each.
    assert out.count('<a href="') == 1, out
    assert out.count("</a>") == 1, out
    # And the open is positioned before the close.
    assert out.index('<a href="') < out.index("</a>"), out


def test_sanitizer_anchor_empty_drops_open() -> None:
    """`<a href="x"></a>` (zero-child) drops the open tag instead of
    leaving a dangling empty `<a></a>` pair. This is the intended
    behavior of the `_parts[-1].startswith('<a href="')` check."""
    out = sanitize_html_for_telegram('<a href="https://x.test/"></a>')
    assert '<a href="' not in out, out
    assert "</a>" not in out, out


def test_sanitizer_bare_anchor_drops_both_tags() -> None:
    """Bare `<a>foo</a>` (no href) — open is silently dropped via the
    `bare_anchor_depth` counter; the matching close must also be dropped.
    Without the counter, `</a>` would leak through and Telegram would
    reject the caption with "Bad Request: can't parse entities."
    Real-world trigger: LLM emits raw HTML in its prose."""
    out = sanitize_html_for_telegram("<a>important</a>")
    assert "<a" not in out, f"bare anchor open leaked: {out!r}"
    assert "</a>" not in out, f"dangling close leaked: {out!r}"
    assert "important" in out, f"inner text dropped: {out!r}"


def test_sanitizer_nested_bare_anchors_balanced() -> None:
    """Two bare `<a>` opens nested with two `</a>` closes should drop
    every anchor tag and keep the inner text. Depth-counter test —
    a single-flag implementation would mis-handle this case."""
    out = sanitize_html_for_telegram("<a><a>x</a>y</a>")
    assert "<a" not in out and "</a>" not in out, out
    assert "x" in out and "y" in out, out


def test_sanitizer_mixed_bare_and_href_anchors() -> None:
    """Mixed: one bare `<a>`, one anchor with href. Counter must distinguish
    so the href-anchor's `</a>` is correctly preserved."""
    out = sanitize_html_for_telegram(
        '<a>bare</a> and <a href="https://x.test/">linked</a>'
    )
    # Bare anchor: dropped open + dropped close, inner text kept.
    # Href anchor: open + close preserved.
    assert out.count("<a href=") == 1, f"expected exactly one href anchor: {out!r}"
    assert out.count("</a>") == 1, f"expected exactly one close: {out!r}"
    assert "bare" in out and "linked" in out, out


def test_sanitizer_escapes_inner_text() -> None:
    """The parser decodes entities; emit MUST re-escape so the resulting
    HTML stays valid for Telegram."""
    out = sanitize_html_for_telegram("<p>X &amp; Y &lt; Z</p>")
    assert "&amp;" in out and "&lt;" in out, out
    # No bare `&` or `<` snuck through (would be parse-rejected by Telegram).
    assert " & " not in out and " < " not in out, out


def test_sanitizer_keeps_blockquote_expandable_attribute() -> None:
    out = sanitize_html_for_telegram("<blockquote expandable><p>x</p></blockquote>")
    assert "<blockquote expandable>" in out, out
    assert "</blockquote>" in out, out


def test_markdown_to_telegram_html_table_dropped() -> None:
    """End-to-end: markdown table → dropped from output; surrounding prose
    survives. This is the realistic LLM-output shape we care about."""
    md = (
        "## Phase 1\n\nLead **prose**.\n\n"
        "| Indicator | Value |\n|---|---|\n| RSI | 38.5 |\n| MACD | -0.47 |\n\n"
        "Trailing prose with a [link](https://example.com)."
    )
    out = markdown_to_telegram_html(md)
    assert "<b>Phase 1</b>" in out, out
    assert "<b>prose</b>" in out, out
    assert '<a href="https://example.com">link</a>' in out, out
    # Table is gone — neither headers nor cell contents leak.
    for forbidden in ("RSI", "MACD", "38.5", "Indicator"):
        assert forbidden not in out, f"table cell leaked: {forbidden!r}"


# ─── format_short_message HTML output ──────────────────────────────────


def test_format_short_message_emits_html_structure() -> None:
    """Caption must use HTML tags (not MarkdownV2) since callers pass
    parse_mode='HTML'."""
    out = format_short_message(
        "SOFI",
        "SELL",
        telegraph_url="https://telegra.ph/SOFI-Analysis-05-10",
        summary="Lead **bold** prose.",
        config_summary="openai · gpt-4o/o4-mini",
        generated_at=datetime(2026, 5, 10, 12, 34, tzinfo=UTC),
    )
    assert "<b>SOFI</b>" in out and "<b>SELL</b>" in out, out
    assert "<blockquote expandable>" in out and "</blockquote>" in out, out
    assert "<b>bold</b>" in out, out  # summary markdown was converted
    # Inline Telegraph link removed — `📰 Instant View` button owns the action now.
    assert "<a href=" not in out, out
    # No MarkdownV2 escape sequences leaked through.
    for marker in ("\\.", "\\!", "\\("):
        assert marker not in out, f"MarkdownV2 escape leaked: {marker!r} in {out!r}"


def test_format_short_message_caption_fits_under_telegram_limit() -> None:
    """Photo captions are capped at 1024 chars in Telegram. extract_summary
    defaults to 700 raw-markdown chars so the HTML-rendered caption stays
    comfortably under the limit even after tag bloat. This pins the
    contract: a realistic worst-case input must produce a caption ≤ 1024."""
    # Realistic-ish LLM body: headers, multiple paragraphs, list, bold.
    summary = (
        "## Verdict\n\n**STRONG SELL** based on the confluence of bearish "
        "indicators. The 50 SMA at $17.32 is well above the current $15.75, "
        "the MACD is sharply negative, and the April 29 distribution day "
        "set a clear institutional-exit tone.\n\n"
        "- Technical breakdown below all key MAs\n"
        "- RSI in bearish territory (not yet oversold)\n"
        "- VWMA confirms heavy selling on the recent volume spikes\n\n"
        "Risk: a positive earnings catalyst could trigger a short-squeeze, "
        "but the technicals strongly favor the bear case."
    )
    out = format_short_message(
        "SOFI",
        "SELL",
        telegraph_url="https://telegra.ph/SOFI-Analysis-05-10-3",
        summary=summary[:700],
        config_summary="openai · gpt-4o/o4-mini · r2 · e=high",
        generated_at=datetime(2026, 5, 10, 12, 34, tzinfo=UTC),
    )
    assert len(out) <= 1024, f"caption {len(out)} chars > 1024 limit:\n{out}"


def test_format_short_message_no_summary_still_works() -> None:
    """Missing summary kwarg: caption omits the blockquote but the
    signal badge + timestamp still render. The Telegraph link is now
    surfaced via the inline-keyboard button, not the caption body."""
    out = format_short_message("SOFI", "BUY", telegraph_url="https://telegra.ph/x")
    assert "<b>SOFI</b>" in out and "<b>BUY</b>" in out
    assert "<blockquote" not in out, out
    assert "Generated" in out


def test_format_short_message_telegraph_failure_path() -> None:
    """When publish failed, render the warning instead of a broken link."""
    out = format_short_message("SOFI", "BUY", telegraph_url=None)
    assert "Telegraph publish failed" in out
    assert "<a href" not in out


def test_full_report_keyboard_passes_url_verbatim() -> None:
    """URL buttons receive the URL verbatim — PTB and Telegram handle
    encoding internally for the Bot API call. The earlier escaping test
    targeted the inline `<a href>` in the caption (no longer there)."""
    from tg_bot.handlers.analysis_runner import _full_report_keyboard  # noqa: E402

    url = "https://example.com/path?a=1&b=2"
    kb = _full_report_keyboard("SOFI", "2026-05-10", telegraph_url=url)
    assert kb.inline_keyboard[0][0].url == url


def test_format_short_message_omits_inline_telegraph_link_when_button_owns_it() -> None:
    """Two-button keyboard owns both actions (`📰 Instant View` URL +
    `📥 Download .md` callback). The inline `<a href>` Telegraph link
    inside the caption was removed — it duplicated the button and read
    as documentation rather than a primary action."""
    out = format_short_message("SOFI", "BUY", telegraph_url="https://telegra.ph/x")
    # No inline anchor + no old labels in the caption.
    assert "<a href" not in out, out
    assert "Read Online" not in out, out
    assert "View Full Report" not in out, out
    # Header still renders the signal badge.
    assert "<b>SOFI</b>" in out and "<b>BUY</b>" in out, out


def test_format_short_message_warns_when_telegraph_publish_fails() -> None:
    """When `telegraph_url` is None the keyboard drops the IV button; the
    caption surfaces a warning so the missing button isn't a silent failure."""
    out = format_short_message("SOFI", "BUY", telegraph_url=None)
    assert "⚠️" in out and "Telegraph publish failed" in out, out
    assert "Instant View unavailable" in out, out


def test_escape_md_v2_url_only_escapes_backslash_and_paren() -> None:
    """`escape_md_v2_url` is NARROWER than `escape_markdown(version=2)` —
    it only handles the two characters that break a `[text](url)` link
    target: `\\` and `)`. Other MarkdownV2 specials (`_`, `*`, `?`, `=`,
    `.`) are LEGAL inside the parens and must NOT be escaped (the full
    escaper would double-escape them and produce a broken URL). Pin the
    carve-out so a refactor that "unifies" the two escapers breaks loudly."""
    # Common URL chars stay verbatim: `_`, `?`, `=`, `.`, `-`, `/`, `:`
    plain = "https://example.com/a_b?x=1"
    assert escape_md_v2_url(plain) == plain, (
        f"common URL chars escaped unexpectedly: {escape_md_v2_url(plain)!r}"
    )

    # Only `)` and `\` get a leading backslash. Input includes `(` (legal)
    # so we can also assert open-paren stays unescaped.
    escaped = escape_md_v2_url("https://example.com/foo(bar)\\baz")
    assert escaped == "https://example.com/foo(bar\\)\\\\baz", repr(escaped)


def test_normalize_indents_bullets_under_numbered_item() -> None:
    """The buggy LLM-emitted shape (col-0 bullets directly under a
    numbered item) gets the 4-space pad required for proper <ol><li><ul>
    nesting by `markdown.markdown`."""
    md = (
        "1. First numbered\n\n"
        "2. **Monitor for:**\n"
        "- Sub bullet A\n"
        "- Sub bullet B\n\n"
        "3. Third numbered"
    )
    out = _normalize_nested_bullets(md)
    assert "    - Sub bullet A" in out, out
    assert "    - Sub bullet B" in out, out

    import markdown

    html = markdown.markdown(out, extensions=["tables"])
    # Sub-bullets must end up inside a <ul> nested in <li>2, not as
    # promoted siblings of the <ol>.
    assert "<ul>" in html and "</ul>" in html, html
    # 3 top-level <li> (not 5, which is what the bug produces)
    # Counting requires excluding the nested ones — quick proxy: the
    # outer <ol> contains exactly 3 child <li>s before </ol>.
    ol_block = html[html.find("<ol>") : html.find("</ol>")]
    # Top-level <li> children are those NOT inside <ul>...</ul>.
    # Strip the <ul> block and count remaining <li>.
    while "<ul>" in ol_block:
        start = ol_block.find("<ul>")
        end = ol_block.find("</ul>") + len("</ul>")
        ol_block = ol_block[:start] + ol_block[end:]
    assert ol_block.count("<li>") == 3, ol_block


def test_normalize_leaves_top_level_bullets_alone() -> None:
    """Top-level bullets not preceded by a numbered list must NOT be
    indented — they were already at the correct level."""
    md = "Section header\n\n- Bullet one\n- Bullet two"
    out = _normalize_nested_bullets(md)
    assert out == md, out


def test_normalize_resets_on_paragraph_break() -> None:
    """After a numbered list ends (paragraph between), subsequent col-0
    bullets should NOT be indented — they belong to a new context."""
    md = (
        "1. Numbered item\n\n"
        "Some paragraph that isn't a list item.\n\n"
        "- Independent bullet\n"
        "- Another independent bullet"
    )
    out = _normalize_nested_bullets(md)
    # Bullets stay at column 0
    assert "\n- Independent bullet" in out, out
    assert "\n- Another independent bullet" in out, out
    # No false-positive indentation of the bullets
    assert "    - Independent bullet" not in out, out


def test_normalize_preserves_already_indented_bullets() -> None:
    """Bullets that the LLM already indented correctly must pass through
    untouched — no double-indent."""
    md = "1. **Monitor for:**\n    - Already indented A\n    - Already indented B"
    out = _normalize_nested_bullets(md)
    # No extra 4 spaces stacked on top of the existing 4-space indent
    assert "        - Already indented" not in out, out
    assert "    - Already indented A" in out, out


def test_normalize_mixed_indent_stops_padding_after_preindented_bullet() -> None:
    """Pins the documented state-machine flip in `_normalize_nested_bullets`
    (rendering/CLAUDE.md): `_is_unindented_bullet` matches col-0 bullets
    only, so a pre-indented `  - ` line falls through to the `else` branch
    which flips `inside_ol = False`. Any subsequent col-0 `- ` bullets
    then render as flat siblings on Telegraph IV instead of nesting under
    the numbered item. Documenting the regression so a future re-write
    of the state machine notices."""
    md = (
        "1. **Monitor for:**\n"
        "  - Pre-indented A (3 spaces — not col-0, not 4-space)\n"
        "- Col-0 bullet that SHOULD nest but won't"
    )
    out = _normalize_nested_bullets(md)
    # State machine flips on the 3-space line → col-0 bullet is NOT padded.
    assert "    - Col-0 bullet" not in out, (
        "regression — col-0 bullet got auto-padded; state machine no longer "
        "flips on pre-indented bullets. Update rendering/CLAUDE.md."
    )
    # The col-0 bullet stays at column 0 in the output.
    assert "\n- Col-0 bullet" in out, out


def test_strip_final_decision_default_shape() -> None:
    """Canonical `**Final Trading Decision: <T>**` + `**Rating: <X>**`
    boilerplate at the top of final_trade_decision gets stripped so
    the 700-char caption clip doesn't waste budget duplicating the
    signal badge that's already above the expandable."""
    raw = (
        "**Final Trading Decision: BRK-B**\n\n"
        "**Rating: HOLD**\n\n"
        "The debate has been vigorous, but it sharpens rather than "
        "overturns the Research Manager's verdict."
    )
    out = _strip_final_decision_header(raw)
    assert "**Final Trading Decision" not in out, out
    assert "**Rating:" not in out, out
    assert out.startswith("The debate has been vigorous"), out


def test_strip_only_decision_line_no_rating() -> None:
    """Some agents emit Final Trading Decision without a separate Rating
    line — drop just the first, pass body through."""
    raw = "**Final Trading Decision: BRK-B**\n\nBody starts here immediately."
    out = _strip_final_decision_header(raw)
    assert out == "Body starts here immediately.", out


def test_strip_only_rating_no_decision_line() -> None:
    """Or just Rating without the Final Trading Decision line — drop it."""
    raw = "**Rating: HOLD**\n\nBody starts here."
    out = _strip_final_decision_header(raw)
    assert out == "Body starts here.", out


def test_strip_passthrough_when_no_boilerplate() -> None:
    """Text without the known leading patterns is returned untouched —
    no false-positive stripping of real content."""
    raw = "The trader recommends HOLD because the market is balanced."
    assert _strip_final_decision_header(raw) == raw


def test_strip_does_not_swallow_body_rating_mention() -> None:
    """A `**Rating:` line that appears deeper in the body (e.g. inside a
    list of considerations) must NOT be stripped — only the leading
    boilerplate, terminated at first non-matching content line."""
    raw = (
        "**Final Trading Decision: SPOT**\n\n"
        "**Rating: UNDERWEIGHT**\n\n"
        "The risk panel tempered the trader's Sell to Underweight.\n\n"
        "**Rating considerations:**\n\n- Position size\n- Stop loss"
    )
    out = _strip_final_decision_header(raw)
    assert out.startswith("The risk panel tempered"), out
    # The body `**Rating considerations:**` line must survive.
    assert "**Rating considerations:**" in out, out


def test_caption_summary_aligns_with_badge_after_strip() -> None:
    """End-to-end: caption_summary(final_state) yields stripped + clipped
    text. For a HOLD case, the clip starts on real synthesis content
    (not the redundant rating header) so the expandable prose lines up
    with the badge shown above it."""
    final_state = {
        "final_trade_decision": (
            "**Final Trading Decision: SPOT**\n\n"
            "**Rating: UNDERWEIGHT**\n\n"
            "After the risk debate, the trader's Sell recommendation is "
            "tempered to Underweight. Maintain a partial position while "
            "monitoring the $480 resistance for confirmation."
        ),
    }
    out = caption_summary(final_state)
    assert out.startswith("After the risk debate"), out
    # No duplicated badge/ticker line in the 700-char clip.
    assert "Final Trading Decision" not in out, out
    assert "Rating: UNDERWEIGHT" not in out, out


# ─── Caption summary: Executive Summary section extraction ──────────────


_STRUCTURED_FINAL_DECISION = (
    "**Final Trading Decision: CLS**\n\n"
    "**Rating**: Underweight\n\n"
    "**Executive Summary**: Trim 25-33% of existing CLS positions into any "
    "strength above $395-$400. Hard stop at $355, re-entry $330-$340.\n\n"
    "**Investment Thesis**: The Research Manager's Underweight recommendation "
    "is adopted as the final decision after the risk debate.\n\n"
    "**Why not Sell (Aggressive Analyst's position):** The aggressive analyst "
    "correctly identifies the asymmetric risk/reward but selling entirely "
    "ignores the structural growth story.\n\n"
    "**Price Target**: 460.0\n\n"
    "**Time Horizon**: 3-6 months"
)


def test_caption_extracts_executive_summary_when_present() -> None:
    """Structured final_trade_decision → caption pulls JUST the Executive
    Summary section. Investment Thesis, Why-not-Sell, Price Target, and
    Time Horizon are excluded from the 700-char preview — those live in
    the Telegraph body / .md download where they belong."""
    final_state = {"final_trade_decision": _STRUCTURED_FINAL_DECISION}
    out = caption_summary(final_state)
    assert "Trim 25-33%" in out, out
    assert "Hard stop at $355" in out, out
    # Sections after Executive Summary must NOT leak into the caption.
    assert "Investment Thesis" not in out, out
    assert "Why not Sell" not in out, out
    assert "Price Target" not in out, out


def test_caption_handles_colon_inside_bold_marker() -> None:
    """LLM emits both `**Executive Summary**:` and `**Executive Summary:**`
    inconsistently. The colon-inside-bold variant must be recognized too."""
    final_state = {
        "final_trade_decision": (
            "**Rating:** HOLD\n\n"
            "**Executive Summary:** Maintain partial position. Monitor "
            "the $480 resistance level.\n\n"
            "**Investment Thesis:** Balanced risk profile."
        )
    }
    out = caption_summary(final_state)
    assert "Maintain partial position" in out, out
    assert "Investment Thesis" not in out, out
    assert "Balanced risk profile" not in out, out


def test_caption_falls_back_when_no_executive_summary_header() -> None:
    """Older analyses or custom-prompt outputs that don't have the
    structured Executive Summary section degrade to the existing
    strip-and-clip behavior — no caption regression for those callers."""
    final_state = {
        "final_trade_decision": (
            "**Final Trading Decision: NVDA**\n\n"
            "**Rating: BUY**\n\n"
            "After thorough debate the trader's Buy stands. Strong "
            "fundamentals + macro tailwinds support continued upside."
        )
    }
    out = caption_summary(final_state)
    # Strip path: the rating/decision lines are gone, body starts here.
    assert out.startswith("After thorough debate"), out
    assert "Final Trading Decision" not in out, out
    assert "Rating: BUY" not in out, out


def test_caption_executive_summary_terminates_at_next_section() -> None:
    """The section extractor must stop at the next `\\n\\n**` paragraph
    header — not run on through subsequent sections. Pins the
    blank-line-required guard against inline `**word**` bolding inside
    the summary prose."""
    final_state = {
        "final_trade_decision": (
            "**Executive Summary**: First section content with a "
            "**bold inline phrase** that must NOT terminate the section.\n\n"
            "**Investment Thesis**: This should be excluded entirely."
        )
    }
    out = caption_summary(final_state)
    assert "bold inline phrase" in out, out
    assert "Investment Thesis" not in out, out
    assert "should be excluded" not in out, out


def test_caption_executive_summary_clipped_when_overlong() -> None:
    """A 2,000-char Executive Summary still gets clipped to the 700-char
    caption budget. The clip is word-boundary safe (existing behavior)."""
    long_body = "Trim positions above $395. " * 100  # ~2,700 chars
    final_state = {
        "final_trade_decision": (
            f"**Executive Summary**: {long_body}\n\n**Investment Thesis**: ignored"
        )
    }
    out = caption_summary(final_state)
    assert len(out) <= 700 + 5, f"clip overran: {len(out)}"  # +5 for ellipsis/marker
    assert out.startswith("Trim positions above"), out


def test_full_report_keyboard_two_button_shape_with_url() -> None:
    """When `telegraph_url` is set, the keyboard exposes both actions
    side-by-side: a URL button (`📰 Instant View`) opening Telegraph IV
    + a callback button (`📥 Download .md`) firing the `getmd:` flow."""
    from tg_bot.handlers.analysis_runner import _full_report_keyboard  # noqa: E402

    kb = _full_report_keyboard(
        "BRK-B", "2026-05-10", telegraph_url="https://telegra.ph/BRK-B-Analysis-05-10"
    )
    rows = kb.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 2, rows
    iv_btn, md_btn = rows[0]
    # URL button: no callback_data, has url set.
    assert iv_btn.url == "https://telegra.ph/BRK-B-Analysis-05-10", iv_btn.url
    assert iv_btn.callback_data is None
    assert "📰" in iv_btn.text and "Instant View" in iv_btn.text, iv_btn.text
    # Callback button: no url, has callback_data.
    assert md_btn.url is None
    assert md_btn.callback_data == "getmd:BRK-B:2026-05-10", md_btn.callback_data
    # Label says "Full Report" (the .md is unbounded — all 7 sections),
    # distinct from the Telegraph IV button which can truncate sections
    # under the 40K HTML budget. Label asymmetry communicates the
    # completeness guarantee to the user.
    assert "📥" in md_btn.text and "Download Full Report" in md_btn.text, md_btn.text
    assert len(md_btn.callback_data.encode("utf-8")) <= 64


def test_full_report_keyboard_drops_iv_button_when_url_missing() -> None:
    """Telegraph publish failed (no url) → IV button omitted, only the
    download-callback button remains. Caller surfaces a warning in the
    caption so users know the IV route is unavailable."""
    from tg_bot.handlers.analysis_runner import _full_report_keyboard  # noqa: E402

    kb = _full_report_keyboard("BRK-B", "2026-05-10", telegraph_url=None)
    rows = kb.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1, rows
    assert rows[0][0].callback_data == "getmd:BRK-B:2026-05-10"
    assert rows[0][0].url is None


# ─── Telegraph packer + full .md report ─────────────────────────────────


_FULL_STATE = {
    "final_trade_decision": "**Final Trading Decision: SOFI**\n\n**Rating: HOLD**\n\nLong synthesis...",
    "trader_investment_plan": "I recommend **HOLD** for SOFI. Reasoning: ...",
    "investment_plan": "### Research Manager's Evaluation\n\nBull/bear synthesis...",
    "market_report": "# Technical Analysis\n\nMA, RSI, MACD discussion...",
    "fundamentals_report": "# Fundamental Analysis\n\nValuation, balance sheet...",
    "news_report": "# News Roundup\n\nWeekly events...",
    "sentiment_report": "# Sentiment Analysis\n\nSocial + news sentiment...",
}


def test_full_md_report_emits_all_seven_sections() -> None:
    """The .md attachment carries every populated section in priority
    order — no budget cap, no truncation."""
    out = format_full_md_report("SOFI", _FULL_STATE)
    for title in (
        "## Final Trading Decision",
        "## Trader's Recommendation",
        "## Research Manager Synthesis",
        "## Market Analysis",
        "## Fundamentals",
        "## News",
        "## Sentiment",
    ):
        assert title in out, (
            f"section {title!r} missing from full md report:\n{out[:200]}"
        )
    # Section order matches the priority list.
    indices = [
        out.index(t)
        for t in (
            "## Final Trading Decision",
            "## Trader's Recommendation",
            "## Research Manager Synthesis",
            "## Market Analysis",
            "## Fundamentals",
            "## News",
            "## Sentiment",
        )
    ]
    assert indices == sorted(indices), "sections out of priority order"


def test_full_md_report_skips_empty_sections() -> None:
    """Missing or empty fields must drop out cleanly — no `## Sentiment\\n\\n\\n`
    artifacts that look like a stub section to a reader."""
    partial = {
        "final_trade_decision": "x",
        "trader_investment_plan": "y",
        "sentiment_report": "",  # explicitly empty
        # other keys absent
    }
    out = format_full_md_report("SOFI", partial)
    assert "## Final Trading Decision" in out
    assert "## Trader's Recommendation" in out
    assert "## Sentiment" not in out
    assert "## Market Analysis" not in out


def test_full_md_report_includes_header_when_provided() -> None:
    out = format_full_md_report(
        "SOFI",
        _FULL_STATE,
        config_summary="openai · gpt-4o/o4-mini",
        generated_at=datetime(2026, 5, 10, 12, 34, tzinfo=UTC),
    )
    assert out.startswith(
        "> Generated 2026-05-10 12:34 UTC · openai · gpt-4o/o4-mini\n\n---\n\n"
    ), out[:200]


def test_telegraph_packer_drops_trailing_section_when_over_budget() -> None:
    """A realistic full final_state with all 7 sections renders to ~73k
    HTML — over Telegraph's 65k cap. The packer must drop the
    lowest-priority section (sentiment_report) and bring HTML under the
    64k internal budget."""
    import markdown

    # Each section sized so the assembled HTML lands above the 65k cap
    # with all 7 included but under it after dropping the last.
    big_section = "Lorem ipsum dolor sit amet. " * 400  # ~11 KB
    state = {
        "final_trade_decision": big_section,
        "trader_investment_plan": big_section,
        "investment_plan": big_section,
        "market_report": big_section,
        "fundamentals_report": big_section,
        "news_report": big_section,
        "sentiment_report": big_section,
    }
    out = format_analysis_result_markdown("SOFI", state, "HOLD")
    rendered = markdown.markdown(out, extensions=["tables"])
    assert len(rendered) <= 65536, f"packer let HTML exceed limit: {len(rendered)}"
    # At least one section should be missing — packer dropped to fit.
    sections_present = sum(
        out.count(f"## {t}")
        for t in (
            "Final Trading Decision",
            "Trader's Recommendation",
            "Research Manager Synthesis",
            "Market Analysis",
            "Fundamentals",
            "News",
            "Sentiment",
        )
    )
    assert sections_present < 7, "packer should have dropped at least one section"
    # The highest-priority sections must still be present.
    assert "## Final Trading Decision" in out
    assert "## Trader's Recommendation" in out


def test_telegraph_packer_keeps_all_sections_when_under_budget() -> None:
    """Small content → all 7 sections fit, no dropping."""
    state = {
        k: f"section content {k}"
        for k, _ in [
            ("final_trade_decision", None),
            ("trader_investment_plan", None),
            ("investment_plan", None),
            ("market_report", None),
            ("fundamentals_report", None),
            ("news_report", None),
            ("sentiment_report", None),
        ]
    }
    # Rename keys correctly
    state = {
        "final_trade_decision": "tiny",
        "trader_investment_plan": "tiny",
        "investment_plan": "tiny",
        "market_report": "tiny",
        "fundamentals_report": "tiny",
        "news_report": "tiny",
        "sentiment_report": "tiny",
    }
    out = format_analysis_result_markdown("SOFI", state, "HOLD")
    for title in (
        "## Final Trading Decision",
        "## Trader's Recommendation",
        "## Research Manager Synthesis",
        "## Market Analysis",
        "## Fundamentals",
        "## News",
        "## Sentiment",
    ):
        assert title in out, (
            f"under-budget run should keep all sections; missing {title!r}"
        )
