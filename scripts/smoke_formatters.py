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

from tg_bot.formatters import format_analysis_result_markdown  # noqa: E402


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
        "> Generated 2026-05-10 12:34 UTC · openai · gpt-4o/o4-mini\n\n"
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
    assert out.startswith("> Generated 2026-04-15\n\n"), out
    assert "12:34" not in out  # no time fragment leaked through


def test_config_summary_only_no_timestamp() -> None:
    out = format_analysis_result_markdown(
        "SOFI",
        _STATE,
        "HOLD",
        config_summary="anthropic · claude-sonnet-4 · r2",
    )
    assert out.startswith("> anthropic · claude-sonnet-4 · r2\n\n"), out


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
