"""Smoke tests for the unified /watch + /refresh picker.

Both commands render the same paginated multi-select keyboard via
`build_watchlist_response`. The picker's `mode` param toggles only the
header copy and the Done-button label; the keyboard's structure and
callback prefixes are identical so paging/selection works the same in
both. The Done handler reads `chat_data["watch_mode"]` to decide
whether to invalidate the cache before running.

Run with: .venv/bin/python3 scripts/smoke_watchlist.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _fresh_data_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="tg_bot_watchlist_smoke_"))
    os.environ["TG_BOT_DATA_DIR"] = str(d)
    return d


def _seed_storage(tickers: list[str]) -> None:
    """Write a watchlist + user_config so build_watchlist_response has
    real data to render."""
    d = Path(os.environ["TG_BOT_DATA_DIR"])
    (d / "watchlist.json").write_text(json.dumps({"1": tickers}))
    (d / "user_config.json").write_text(
        json.dumps(
            {
                "1": {
                    "llm_provider": "openai",
                    "deep_think_llm": "gpt-4o",
                    "quick_think_llm": "o4-mini",
                }
            }
        )
    )


def _reload_storage_singletons():
    """The storage singletons are constructed at import time against the
    env var; for tests that swap data dirs we need to drop and re-import.
    Returns a SimpleNamespace exposing both `commands` and `pickers`
    modules — the picker builders moved out of `commands.py` in the
    handlers/ split, so tests need both."""
    import importlib

    for mod in [
        "tg_bot.storage",
        "tg_bot.storage.user_config",
        "tg_bot.storage.watchlist",
        "tg_bot.handlers.pickers",
        "tg_bot.handlers.commands",
    ]:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from tg_bot.handlers import commands, pickers

    # Expose `pickers` as an attribute on the returned `commands` module
    # so tests that do `commands = _reload_storage_singletons()` can keep
    # the existing single-name pattern; new builder calls write
    # `commands.pickers.build_X(...)` (or unpack manually).
    commands.pickers = pickers
    return commands


# ─── picker rendering ──────────────────────────────────────────────────


async def test_watch_mode_header_and_done_label() -> None:
    _fresh_data_dir()
    _seed_storage(["NVDA", "AAPL"])
    commands = _reload_storage_singletons()
    text, kb = commands.pickers.build_watchlist_response(
        "1", selected={"NVDA"}, page=0, mode="watch"
    )
    assert "Your Watchlist" in text and "Force Refresh" not in text, text
    # Done is the first button on the last row; "✅ Done (N)" is the watch label.
    done_label = kb.inline_keyboard[-1][0].text
    assert done_label == "✅ Done (1)", done_label
    cb = kb.inline_keyboard[-1][0].callback_data
    assert cb == "runall:go", cb


async def test_refresh_mode_header_and_done_label() -> None:
    _fresh_data_dir()
    _seed_storage(["NVDA", "AAPL"])
    commands = _reload_storage_singletons()
    text, kb = commands.pickers.build_watchlist_response(
        "1", selected={"NVDA"}, page=0, mode="refresh"
    )
    assert "Force Refresh" in text and "Drops today's cached result" in text, text
    done_label = kb.inline_keyboard[-1][0].text
    assert done_label == "🔄 Refresh (1)", done_label
    # Same callback data — dispatcher routes both to _handle_done; the
    # watch_mode flag in chat_data drives the behavior split.
    cb = kb.inline_keyboard[-1][0].callback_data
    assert cb == "runall:go", cb


async def test_keyboard_structure_identical_across_modes() -> None:
    """The two modes must keep the same ticker buttons, pagination, and
    bulk-select rows so users can page/toggle in refresh mode exactly
    like in watch mode."""
    _fresh_data_dir()
    _seed_storage(
        ["NVDA", "AAPL", "TSLA", "MSFT", "GOOGL", "AMZN", "META", "NFLX", "ORCL", "CRM"]
    )
    commands = _reload_storage_singletons()
    _, kb_watch = commands.pickers.build_watchlist_response(
        "1", selected=set(), page=0, mode="watch"
    )
    _, kb_refresh = commands.pickers.build_watchlist_response(
        "1", selected=set(), page=0, mode="refresh"
    )
    # Same number of rows.
    assert len(kb_watch.inline_keyboard) == len(kb_refresh.inline_keyboard)
    # All ticker rows + pagination + bulk-select rows have identical
    # callback_data — only the last row's first button (Done) text differs.
    for row_w, row_r in zip(
        kb_watch.inline_keyboard[:-1], kb_refresh.inline_keyboard[:-1], strict=True
    ):
        assert [b.callback_data for b in row_w] == [b.callback_data for b in row_r]


# ─── /add via ForceReply ───────────────────────────────────────────────


def _build_reply_update(reply_text: str, user_text: str, *, from_bot: bool = True):
    """Construct a minimal Update shape that `add_via_reply` reads from.

    Only the attributes touched in the handler are populated; everything
    else is left as a SimpleNamespace so AttributeError surfaces a real
    coverage gap rather than masking with a MagicMock."""
    replies: list[str] = []

    async def _reply_text(text):
        replies.append(text)

    msg = SimpleNamespace(
        text=user_text,
        reply_to_message=SimpleNamespace(
            text=reply_text,
            from_user=SimpleNamespace(is_bot=from_bot),
        ),
        reply_text=_reply_text,
    )
    update = SimpleNamespace(
        message=msg,
        effective_user=SimpleNamespace(id=1),
    )
    return update, replies


async def test_add_via_reply_fires_only_on_exact_prompt_match() -> None:
    """Force-reply `/add` is dispatched by a `MessageHandler(REPLY)`
    filter that fires on *any* reply to a bot message. `add_via_reply`
    then guards by comparing `reply_to_message.text` verbatim against
    `ADD_PROMPT` — without that guard, every reply to any of the bot's
    messages (e.g. tapping reply on a `/help` response and typing
    something) would silently mutate the watchlist."""
    _fresh_data_dir()
    _seed_storage([])
    commands = _reload_storage_singletons()

    # Stub _apply_add so the handler doesn't touch yfinance or storage.
    apply_calls: list[tuple[int, list[str]]] = []

    async def fake_apply_add(user_id: int, tokens: list[str]) -> str:
        apply_calls.append((user_id, tokens))
        return f"✅ Added: {', '.join(tokens)}"

    commands._apply_add = fake_apply_add

    # Negative: reply to a different bot message (e.g. /help output).
    update, replies = _build_reply_update(
        reply_text="Some other bot message (not the add prompt)",
        user_text="NVDA AAPL",
    )
    await commands.add_via_reply(update, SimpleNamespace())
    assert apply_calls == [], f"add fired on mismatched reply: {apply_calls!r}"
    assert replies == [], f"reply emitted on mismatch: {replies!r}"

    # Negative: reply to a user (not the bot) — defensive check against
    # spoofed reply_to_message envelopes.
    update, _ = _build_reply_update(
        reply_text=commands.ADD_PROMPT,
        user_text="NVDA",
        from_bot=False,
    )
    await commands.add_via_reply(update, SimpleNamespace())
    assert apply_calls == [], f"add fired on reply to non-bot user: {apply_calls!r}"

    # Positive: reply to the exact ADD_PROMPT from the bot → handler fires.
    update, replies = _build_reply_update(
        reply_text=commands.ADD_PROMPT,
        user_text="NVDA AAPL",
    )
    await commands.add_via_reply(update, SimpleNamespace())
    assert apply_calls == [(1, ["NVDA", "AAPL"])], apply_calls
    assert replies == ["✅ Added: NVDA, AAPL"], replies


async def test_empty_watchlist_no_keyboard() -> None:
    """Both modes must short-circuit cleanly on an empty watchlist."""
    _fresh_data_dir()
    _seed_storage([])
    commands = _reload_storage_singletons()
    for mode in ("watch", "refresh"):
        text, kb = commands.pickers.build_watchlist_response(
            "1", selected=set(), page=0, mode=mode
        )
        assert kb is None, f"{mode}: expected None keyboard, got {kb}"
        assert "empty" in text.lower(), text


# ─── /list (text view) ─────────────────────────────────────────────────


async def test_list_empty_watchlist() -> None:
    """Handler-level: empty watchlist short-circuits with a /add nudge,
    no digest header rendered."""
    _fresh_data_dir()
    _seed_storage([])
    commands = _reload_storage_singletons()

    sent: list[dict] = []

    async def _reply(text, **kw):
        sent.append({"text": text, **kw})

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=SimpleNamespace(reply_text=_reply),
    )
    await commands.list_cmd(update, SimpleNamespace(chat_data={}))
    assert len(sent) == 1, sent
    assert "empty" in sent[0]["text"].lower(), sent[0]["text"]
    assert "/add" in sent[0]["text"], sent[0]["text"]
    assert sent[0]["parse_mode"] == "MarkdownV2"


async def test_list_format_no_digest() -> None:
    """`_format_list_view`: watchlist + None digest → no digest header,
    grid in pre block, footer says 'off'. Pure formatter test — no
    storage involvement."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA"])
    commands = _reload_storage_singletons()

    text = commands._format_list_view(["AAPL", "NVDA"], None, None)
    # Header
    assert "Watchlist" in text and "2 tickers" in text, text
    assert "Digest" not in text, "no digest header should render"
    # Grid in pre block
    assert "```\n" in text and "\n```" in text, "grid must be in pre block"
    # Footer
    assert "Daily digest off" in text, text
    # No bell markers anywhere
    assert "🔔" not in text, "no bell markers when digest off"


async def test_list_format_digest_all_watchlist() -> None:
    """`_format_list_view`: enrolled == set(watchlist) (legacy save) →
    digest header reads 'all N fire daily', no per-ticker bell markers
    needed (every ticker is enrolled), grid stays clean."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA"])
    commands = _reload_storage_singletons()

    digest = {
        "enabled": True,
        "hour_local": 9,
        "tz": "America/Los_Angeles",
        "chat_id": 999,
    }
    enrolled = {"AAPL", "NVDA"}

    text = commands._format_list_view(["AAPL", "NVDA"], digest, enrolled)
    # Digest header line present
    assert "Digest" in text and "09:00" in text, text
    assert "all 2 fire daily" in text, text
    # Grid in pre block
    assert "```\n" in text and "\n```" in text, "grid must be in pre block"
    # No per-ticker bell in the grid block — the digest header has its own bell
    # icon, but rows in the grid must not.
    grid_start = text.index("```\n") + 4
    grid_end = text.index("\n```", grid_start)
    grid_body = text[grid_start:grid_end]
    assert "🔔" not in grid_body, f"grid must not contain bell markers: {grid_body!r}"
    # No "→" enrolled-tickers list either (all-enrolled case skips it)
    assert "→" not in text, text


async def test_list_format_digest_with_filter() -> None:
    """`_format_list_view`: enrolled is a proper subset of watchlist →
    header has the digest line AND an indented `→ T1, T2` line naming
    the enrolled tickers. Grid stays clean (no inline markers)."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA", "TSLA", "MSFT"])
    commands = _reload_storage_singletons()

    digest = {
        "enabled": True,
        "hour_local": 8,
        "tz": "America/New_York",
        "chat_id": 999,
        "tickers": ["AAPL", "TSLA"],
    }
    enrolled = {"AAPL", "TSLA"}

    text = commands._format_list_view(
        ["AAPL", "NVDA", "TSLA", "MSFT"], digest, enrolled
    )
    # Header
    assert "Digest" in text and "08:00" in text, text
    # Pin the exact → line so a regression that drops the
    # `if t in enrolled` filter (which would name every watchlist
    # ticker) is caught — checking just `"AAPL" in text` is trivially
    # true since AAPL also appears in the grid below.
    arrow_line = next(
        (line for line in text.splitlines() if "→" in line),
        None,
    )
    assert arrow_line is not None, f"expected '→' line in: {text!r}"
    assert arrow_line == "   → `AAPL`, `TSLA`", repr(arrow_line)
    # Grid in pre block
    assert "```\n" in text and "\n```" in text, "grid must be in pre block"
    # Grid body has no bell markers
    grid_start = text.index("```\n") + 4
    grid_end = text.index("\n```", grid_start)
    grid_body = text[grid_start:grid_end]
    assert "🔔" not in grid_body, (
        f"per-ticker bell markers must not appear in grid: {grid_body!r}"
    )


async def test_list_format_digest_zero_enrolled() -> None:
    """`_format_list_view`: digest enabled but no tickers enrolled
    (empty filter set, K=0) → digest header line present (so user sees
    the schedule), no `→` line (nothing to list), footer reminds the
    user to fix their filter."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA"])
    commands = _reload_storage_singletons()

    digest = {
        "enabled": True,
        "hour_local": 9,
        "tz": "UTC",
        "chat_id": 999,
        "tickers": [],
    }
    enrolled: set[str] = set()

    text = commands._format_list_view(["AAPL", "NVDA"], digest, enrolled)
    assert "Digest" in text and "09:00" in text, text
    assert "→" not in text, "no '→' line when enrolled set is empty"
    assert "Digest enabled but no tickers enrolled" in text, text


async def test_list_format_no_inline_backticks_per_ticker() -> None:
    """The new grid uses a single triple-backtick pre block, NOT per-
    ticker inline backticks. Verifies the layout regression doesn't
    accidentally revert to inline code-span styling, which is what
    caused the original alignment wobble."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA", "TSLA"])
    commands = _reload_storage_singletons()

    text = commands._format_list_view(["AAPL", "NVDA", "TSLA"], None, None)
    grid_start = text.index("```\n") + 4
    grid_end = text.index("\n```", grid_start)
    grid_body = text[grid_start:grid_end]
    # No backticks inside the grid body (which would imply nested
    # inline code spans — wrong format).
    assert "`" not in grid_body, f"no inline backticks in grid: {grid_body!r}"


# ─── _format_ticker_grid helper ─────────────────────────────────────────


async def test_grid_renders_inside_pre_block() -> None:
    """Grid output is wrapped in MarkdownV2 triple-backtick fences so the
    entire block is one monospace context. Without this, spaces BETWEEN
    inline code-spans render in the proportional message font and rows
    wobble."""
    _fresh_data_dir()
    _seed_storage(["AAPL"])
    commands = _reload_storage_singletons()

    text = commands._format_ticker_grid(["AAPL"])
    assert text.startswith("```\n"), repr(text[:10])
    assert text.endswith("\n```"), repr(text[-10:])


async def test_grid_short_tickers_4_cols() -> None:
    """Short US tickers (≤5 chars) → 4-column grid, cells padded to
    max_len + _GRID_GUTTER. With 4 tickers `AAPL NVDA TSLA MSFT`,
    cell_width = 4 + 2 = 6, ncols = min(4, 36//6) = 4 → 1 row."""
    _fresh_data_dir()
    _seed_storage(["AAPL"])
    commands = _reload_storage_singletons()

    text = commands._format_ticker_grid(["AAPL", "NVDA", "TSLA", "MSFT"])
    # Strip pre fences: "```\n" prefix (4 chars), "\n```" suffix (4 chars).
    body = text[4:-4]
    lines = body.split("\n")
    assert len(lines) == 1, lines
    # Each cell = "AAPL  ", joined → "AAPL  NVDA  TSLA  MSFT  ", rstripped
    # to "AAPL  NVDA  TSLA  MSFT".
    assert lines[0] == "AAPL  NVDA  TSLA  MSFT", repr(lines[0])


async def test_grid_8_tickers_wraps_to_2_rows() -> None:
    """8 short tickers at 4 cols → exactly 2 rows."""
    _fresh_data_dir()
    _seed_storage(["AAPL"])
    commands = _reload_storage_singletons()

    text = commands._format_ticker_grid(
        ["AAPL", "NVDA", "TSLA", "MSFT", "GOOG", "AMZN", "META", "NFLX"]
    )
    body = text[4:-4]
    lines = body.split("\n")
    assert len(lines) == 2, lines


async def test_grid_long_ticker_drops_to_2_cols() -> None:
    """Indian NSE-style ticker `RELIANCE.NS` (11 chars) forces
    cell_width = 13, ncols = min(4, 36//13) = 2. All rows pad to 13
    so alignment is uniform regardless of which row holds the long
    ticker."""
    _fresh_data_dir()
    _seed_storage(["AAPL"])
    commands = _reload_storage_singletons()

    text = commands._format_ticker_grid(["AAPL", "NVDA", "RELIANCE.NS", "MSFT"])
    body = text[4:-4]
    lines = body.split("\n")
    assert len(lines) == 2, lines
    # Row 1: "AAPL         NVDA         " → rstrip → "AAPL         NVDA"
    assert lines[0] == "AAPL         NVDA", repr(lines[0])
    # Row 2: "RELIANCE.NS  MSFT         " → rstrip → "RELIANCE.NS  MSFT"
    assert lines[1] == "RELIANCE.NS  MSFT", repr(lines[1])


async def test_grid_extreme_ticker_drops_to_1_col() -> None:
    """Pathological 20-char ticker → cell_width = 22, ncols = max(1, 36//22)
    = 1 → vertical list. The guard `max(1, ...)` prevents ncols=0 on
    impossibly long tickers."""
    _fresh_data_dir()
    _seed_storage(["AAPL"])
    commands = _reload_storage_singletons()

    text = commands._format_ticker_grid(["AAPL", "X" * 20])
    body = text[4:-4]
    lines = body.split("\n")
    assert len(lines) == 2, lines
    # cell_width = 22, both rows pad to 22 then rstrip
    assert lines[0].rstrip() == "AAPL"
    assert lines[1].rstrip() == "X" * 20


async def test_list_handler_non_empty_path_sets_parse_mode() -> None:
    """The non-empty `_format_list_view` output is MarkdownV2; the
    handler must pass `parse_mode="MarkdownV2"` on the reply for the
    formatting to render. The empty-watchlist path is covered above by
    `test_list_empty_watchlist`; this pins the populated path so a
    refactor can't silently drop parse_mode from `list_cmd`."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA"])
    commands = _reload_storage_singletons()

    sent: list[dict] = []

    async def _reply(text, **kw):
        sent.append({"text": text, **kw})

    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1),
        message=SimpleNamespace(reply_text=_reply),
    )
    await commands.list_cmd(update, SimpleNamespace(chat_data={}))
    assert len(sent) == 1, sent
    assert sent[0]["parse_mode"] == "MarkdownV2", sent[0]
    # Body sanity: header + ticker grid present. The grid now lives in
    # a triple-backtick pre block instead of per-ticker inline code
    # spans, so check inside the pre block for the tickers.
    body = sent[0]["text"]
    assert "Watchlist" in body, body
    grid_start = body.index("```\n") + 4
    grid_end = body.index("\n```", grid_start)
    grid_body = body[grid_start:grid_end]
    assert "AAPL" in grid_body and "NVDA" in grid_body, grid_body


SCENARIOS = [
    ("watch mode header + Done label", test_watch_mode_header_and_done_label),
    ("refresh mode header + Done label", test_refresh_mode_header_and_done_label),
    (
        "keyboard structure identical across modes",
        test_keyboard_structure_identical_across_modes,
    ),
    ("empty watchlist short-circuits in both modes", test_empty_watchlist_no_keyboard),
    (
        "force-reply /add fires only on exact ADD_PROMPT match",
        test_add_via_reply_fires_only_on_exact_prompt_match,
    ),
    ("/list empty watchlist → nudge", test_list_empty_watchlist),
    ("_format_list_view: no digest → footer 'off'", test_list_format_no_digest),
    (
        "_format_list_view: all watchlist fires → 'all N fire daily' + no markers",
        test_list_format_digest_all_watchlist,
    ),
    (
        "_format_list_view: filter → '→ T1, T2' header + no bell markers in grid",
        test_list_format_digest_with_filter,
    ),
    (
        "/list handler sets parse_mode=MarkdownV2 on non-empty path",
        test_list_handler_non_empty_path_sets_parse_mode,
    ),
    # --- _format_ticker_grid helper ---
    ("_format_ticker_grid: wrapped in pre block", test_grid_renders_inside_pre_block),
    (
        "_format_ticker_grid: 4 short tickers → 4 cols, 1 row",
        test_grid_short_tickers_4_cols,
    ),
    (
        "_format_ticker_grid: 8 short tickers → 2 rows",
        test_grid_8_tickers_wraps_to_2_rows,
    ),
    (
        "_format_ticker_grid: RELIANCE.NS → drops to 2 cols",
        test_grid_long_ticker_drops_to_2_cols,
    ),
    (
        "_format_ticker_grid: 20-char ticker → 1 col",
        test_grid_extreme_ticker_drops_to_1_col,
    ),
    (
        "_format_list_view: digest on, zero enrolled → reminder footer",
        test_list_format_digest_zero_enrolled,
    ),
    (
        "_format_list_view: grid uses pre block, no inline backticks",
        test_list_format_no_inline_backticks_per_ticker,
    ),
]


async def main() -> int:
    failures = 0
    for label, fn in SCENARIOS:
        try:
            await fn()
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
    sys.exit(asyncio.run(main()))
