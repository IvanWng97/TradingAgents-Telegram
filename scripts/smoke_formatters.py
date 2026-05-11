"""Smoke tests for formatters that produce shareable artifacts.

Telegraph URLs are public-by-default and frequently shared without the
in-Telegram caption that carries the config trace. The
`format_analysis_result_markdown` header (added per user request: shared
links should self-document which model + run produced the analysis)
prepends a blockquote with `Generated <ts>` + optional config summary.
These scenarios pin that contract.

Run with: .venv/bin/python3 scripts/smoke_formatters.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_bot.formatters import (  # noqa: E402
    _normalize_nested_bullets,
    _strip_final_decision_header,
    caption_summary,
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
        generated_at=datetime(2026, 5, 10, 12, 34, tzinfo=timezone.utc),
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
    dt = datetime(2026, 5, 10, 9, 5, tzinfo=timezone.utc)
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
        generated_at=datetime(2026, 5, 10, 12, 34, tzinfo=timezone.utc),
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
        generated_at=datetime(2026, 5, 10, 12, 34, tzinfo=timezone.utc),
    )
    assert "<b>SOFI</b>" in out and "<b>SELL</b>" in out, out
    assert "<blockquote expandable>" in out and "</blockquote>" in out, out
    assert "<b>bold</b>" in out, out  # summary markdown was converted
    assert '<a href="https://telegra.ph/SOFI-Analysis-05-10">' in out, out
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
        generated_at=datetime(2026, 5, 10, 12, 34, tzinfo=timezone.utc),
    )
    assert len(out) <= 1024, f"caption {len(out)} chars > 1024 limit:\n{out}"


def test_format_short_message_no_summary_still_works() -> None:
    """Missing summary kwarg: caption omits the blockquote but the
    signal/timestamp/link still render."""
    out = format_short_message("SOFI", "BUY", telegraph_url="https://telegra.ph/x")
    assert "<b>SOFI</b>" in out and "<b>BUY</b>" in out
    assert "<blockquote" not in out, out
    assert '<a href="https://telegra.ph/x">' in out


def test_format_short_message_telegraph_failure_path() -> None:
    """When publish failed, render the warning instead of a broken link."""
    out = format_short_message("SOFI", "BUY", telegraph_url=None)
    assert "Telegraph publish failed" in out
    assert "<a href" not in out


def test_format_short_message_escapes_url_chars() -> None:
    """A URL containing `&` or `"` must be html-escaped in the href
    attribute or it'll break the tag."""
    url = 'https://example.com/path?a=1&b="2"'
    out = format_short_message("SOFI", "BUY", telegraph_url=url)
    assert "&amp;" in out, out
    assert "&quot;" in out, out
    assert 'a=1&b="2"' not in out, 'raw `&` or `"` leaked into href'


def test_format_short_message_uses_read_online_label() -> None:
    """Caption labels the Telegraph link as `📰 Read Online (preview)`
    to disambiguate it from the `📥 Download .md (all sections)` button
    that sits below the photo. Both said "Full Report" before — confusing
    when paired."""
    out = format_short_message("SOFI", "BUY", telegraph_url="https://telegra.ph/x")
    assert "📰" in out, out
    assert "Read Online (preview)" in out, out
    # Old label and its emoji must be gone.
    assert "View Full Report" not in out, out
    assert "📄" not in out, out


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


def test_full_report_keyboard_callback_data_shape() -> None:
    """`getmd:<TICKER>:<DATE>` payload + the `📥 Download .md (all
    sections)` button label is the wire contract — the handler dispatch
    in button_callback splits on `:` and the user-facing differentiation
    from the `📰 Read Online` link depends on this exact label."""
    # Defer the import: tg_bot.handlers.callbacks pulls in PTB on import.
    from tg_bot.handlers.callbacks import _full_report_keyboard  # noqa: E402

    kb = _full_report_keyboard("BRK-B", "2026-05-10")
    rows = kb.inline_keyboard
    assert len(rows) == 1 and len(rows[0]) == 1, rows
    btn = rows[0][0]
    assert btn.callback_data == "getmd:BRK-B:2026-05-10", btn.callback_data
    assert "📥" in btn.text and "Download .md" in btn.text, btn.text
    # callback_data must stay under Telegram's 64-byte cap.
    assert len(btn.callback_data.encode("utf-8")) <= 64


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
        generated_at=datetime(2026, 5, 10, 12, 34, tzinfo=timezone.utc),
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


SCENARIOS = [
    ("fresh run prepends full ts + config header", test_fresh_run_prepends_full_header),
    ("history prepends date-only header", test_history_prepends_date_only_header),
    (
        "config_summary alone produces header without ts",
        test_config_summary_only_no_timestamp,
    ),
    ("no args = no header (backward compat)", test_no_args_no_header),
    (
        "datetime checked before date in isinstance chain",
        test_datetime_subclass_check_picks_full_format,
    ),
    (
        "body blockquote lines don't merge with header",
        test_body_blockquote_lines_stay_separate_from_header,
    ),
    # HTML sanitizer
    ("sanitizer keeps inline allowed tags", test_sanitizer_keeps_inline_allowed_tags),
    ("sanitizer converts headers to bold", test_sanitizer_converts_headers_to_bold),
    ("sanitizer drops tables with content", test_sanitizer_drops_tables_with_content),
    ("sanitizer flattens lists to bullets", test_sanitizer_flattens_lists_to_bullets),
    (
        "sanitizer drops unknown tag, preserves inner text",
        test_sanitizer_drops_unknown_tag_preserves_inner_text,
    ),
    ("sanitizer re-escapes inner text", test_sanitizer_escapes_inner_text),
    (
        "sanitizer keeps blockquote expandable attribute",
        test_sanitizer_keeps_blockquote_expandable_attribute,
    ),
    (
        "markdown→html pipeline drops tables end-to-end",
        test_markdown_to_telegram_html_table_dropped,
    ),
    # format_short_message HTML output
    (
        "format_short_message emits HTML structure",
        test_format_short_message_emits_html_structure,
    ),
    (
        "caption fits under 1024-char Telegram limit",
        test_format_short_message_caption_fits_under_telegram_limit,
    ),
    (
        "no summary → no blockquote, link still renders",
        test_format_short_message_no_summary_still_works,
    ),
    (
        "Telegraph publish failure path",
        test_format_short_message_telegraph_failure_path,
    ),
    ("URL specials escaped in href", test_format_short_message_escapes_url_chars),
    (
        "caption Telegraph link uses 📰 Read Online (preview) label",
        test_format_short_message_uses_read_online_label,
    ),
    (
        "📥 Download .md button payload shape (getmd:<T>:<D>)",
        test_full_report_keyboard_callback_data_shape,
    ),
    # Markdown nested-bullet normalization
    (
        "nested bullets get 4-space indent under numbered items",
        test_normalize_indents_bullets_under_numbered_item,
    ),
    (
        "top-level bullets stay at col 0 (no false-positive indent)",
        test_normalize_leaves_top_level_bullets_alone,
    ),
    (
        "paragraph break between OL and bullets resets context",
        test_normalize_resets_on_paragraph_break,
    ),
    (
        "already-indented bullets pass through untouched",
        test_normalize_preserves_already_indented_bullets,
    ),
    # final_trade_decision header strip + caption_summary
    (
        "strip drops `**Final Trading Decision`+`**Rating:` boilerplate",
        test_strip_final_decision_default_shape,
    ),
    (
        "strip handles missing Rating line",
        test_strip_only_decision_line_no_rating,
    ),
    (
        "strip handles missing Final Trading Decision line",
        test_strip_only_rating_no_decision_line,
    ),
    (
        "strip passes through text without boilerplate",
        test_strip_passthrough_when_no_boilerplate,
    ),
    (
        "strip does not swallow body Rating mentions",
        test_strip_does_not_swallow_body_rating_mention,
    ),
    (
        "caption_summary aligns badge + expandable after strip",
        test_caption_summary_aligns_with_badge_after_strip,
    ),
    # Telegraph packer + full .md report
    (
        "full md report emits all 7 sections in priority order",
        test_full_md_report_emits_all_seven_sections,
    ),
    ("full md report skips empty sections", test_full_md_report_skips_empty_sections),
    (
        "full md report includes header when provided",
        test_full_md_report_includes_header_when_provided,
    ),
    (
        "Telegraph packer drops trailing section when over budget",
        test_telegraph_packer_drops_trailing_section_when_over_budget,
    ),
    (
        "Telegraph packer keeps all sections when under budget",
        test_telegraph_packer_keeps_all_sections_when_under_budget,
    ),
]


def main() -> int:
    failures = 0
    for label, fn in SCENARIOS:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"  {FAIL} {label}: {e}")
        except Exception as e:
            failures += 1
            print(f"  {FAIL} {label}: {type(e).__name__}: {e}")
        else:
            print(f"  {PASS} {label}")
    print()
    if failures:
        print(f"{FAIL} {failures} of {len(SCENARIOS)} failed")
        return 1
    print(f"{PASS} all {len(SCENARIOS)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
