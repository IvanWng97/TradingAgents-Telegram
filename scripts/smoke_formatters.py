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
    format_analysis_result_markdown,
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
    formatter must still accept legacy 3-arg invocations) get no header."""
    out = format_analysis_result_markdown("SOFI", _STATE, "BUY")
    assert not out.startswith(">"), out
    assert out.startswith("HOLD - sample."), out


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
