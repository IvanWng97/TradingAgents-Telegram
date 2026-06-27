"""Smoke tests for the unified /watch + /refresh picker.

Both commands render the same paginated multi-select keyboard via
`build_watchlist_response`. The picker's `mode` param toggles only the
header copy and the Done-button label; the keyboard's structure and
callback prefixes are identical so paging/selection works the same in
both. The Done handler reads `chat_data["watch_mode"]` to decide
whether to invalidate the cache before running.

Run with: pytest tests/test_watchlist.py
"""

from __future__ import annotations

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


# ─── storage layer (WatchlistStorage prune behavior) ───────────────────


async def test_remove_last_ticker_drops_user_key() -> None:
    """Pins the documented prune behavior in `watchlist.py:43-44`: when
    `remove_ticker` empties a user's list, the user key is dropped from
    `_data` entirely (not left as `[]`). `get_watchlist` still returns
    `[]` either way so the user-facing surface is unchanged — but
    storage/CLAUDE.md flags this as a subtle gotcha for any future code
    that keys "has the user ever used the bot" on watchlist presence."""
    _fresh_data_dir()
    from tg_bot.storage.watchlist import WatchlistStorage

    path = Path(os.environ["TG_BOT_DATA_DIR"]) / "watchlist.json"
    storage = WatchlistStorage(path)

    await storage.add_ticker("1", "AAPL")
    assert "1" in storage._data and storage._data["1"] == ["AAPL"]

    removed = await storage.remove_ticker("1", "AAPL")
    assert removed is True
    # User key entirely pruned, not left as [].
    assert "1" not in storage._data, (
        f"expected '1' to be removed from _data, got {storage._data!r}"
    )
    # Public surface still returns [] — caller never sees the prune.
    assert storage.get_watchlist("1") == []


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


# ─── Invariant #7: watch_mode → force_refresh translation + state pop ─────
#
# Invariant #7 (root CLAUDE.md) says the picker's `chat_data["watch_mode"]`
# is the single carrier of the watch-vs-refresh choice, and that the
# Done/Cancel handlers in `handlers/callbacks.py` are the ones that
# translate "refresh" → `force_refresh=True` and pop the three picker keys
# (`watch_mode` / `watch_selection` / `watch_page`). The rest of the suite
# only ever pins `build_watchlist_response` rendering or passes
# `force_refresh` straight into `_run_analysis_for_ticker`, so the
# mode→kwarg translation and the state-pop contract in `_handle_done` /
# `_handle_cancel` had NO test that would fail if either broke. These pin it.
#
# NOTE: `callbacks.check_llm_configured` is no-op'd session-wide by the
# autouse `_disable_digest_llm_precheck` conftest fixture, so `_handle_done`
# clears the LLM precheck and reaches the run/pop path without arming a
# provider + env key.


class _FakeQuery:
    """Minimal callback-query stand-in for the Done/Cancel handlers.

    Only the attributes those two handlers touch are populated; the
    single-ticker `_handle_done` path reads `message.chat_id` and calls
    `delete_message()`, the cancel path may fall back to
    `edit_message_text()`. Everything is recorded so misroutes surface."""

    def __init__(self, chat_id: int = 555) -> None:
        self.message = SimpleNamespace(chat_id=chat_id, message_id=999)
        self.answers: list[tuple] = []
        self.deleted = False
        self.edits: list[tuple] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def delete_message(self):
        self.deleted = True

    async def edit_message_text(self, text, **kw):
        self.edits.append((text, kw))


async def test_handle_done_refresh_threads_force_refresh_true_and_pops_state() -> None:
    """`_handle_done` reads `chat_data["watch_mode"]`, translates
    "refresh" → `force_refresh=True`, and pops all three picker keys
    before dispatching. Pins the mode→kwarg translation that the rest of
    the suite bypasses by passing `force_refresh` straight into
    `_run_analysis_for_ticker`."""
    import tg_bot.handlers.callbacks as callbacks

    recorded: dict = {}

    async def fake_run(context, chat_id, user_id, ticker, force_refresh=False):
        recorded["args"] = (chat_id, user_id, ticker)
        recorded["force_refresh"] = force_refresh
        return "completed"

    orig = callbacks._run_analysis_for_ticker
    callbacks._run_analysis_for_ticker = fake_run
    try:
        query = _FakeQuery(chat_id=555)
        context = SimpleNamespace(
            chat_data={
                "watch_mode": "refresh",
                # Single ticker → direct-call path (no asyncio.gather).
                "watch_selection": {"NVDA"},
                "watch_page": 2,
            }
        )
        await callbacks._handle_done(query, context, 1)
    finally:
        callbacks._run_analysis_for_ticker = orig

    # refresh mode → force_refresh=True threaded into the runner.
    assert recorded["force_refresh"] is True, recorded
    assert recorded["args"] == (555, 1, "NVDA"), recorded
    # All three picker keys popped after the precheck commits to running.
    assert "watch_mode" not in context.chat_data, context.chat_data
    assert "watch_selection" not in context.chat_data, context.chat_data
    assert "watch_page" not in context.chat_data, context.chat_data


async def test_handle_done_watch_threads_force_refresh_false() -> None:
    """The watch-mode half of the translation: absent/`"watch"` mode →
    `force_refresh=False`. Same picker, same dispatch — only the flag
    flips."""
    import tg_bot.handlers.callbacks as callbacks

    recorded: dict = {}

    async def fake_run(context, chat_id, user_id, ticker, force_refresh=False):
        recorded["force_refresh"] = force_refresh
        return "completed"

    orig = callbacks._run_analysis_for_ticker
    callbacks._run_analysis_for_ticker = fake_run
    try:
        query = _FakeQuery()
        context = SimpleNamespace(
            chat_data={"watch_mode": "watch", "watch_selection": {"AAPL"}}
        )
        await callbacks._handle_done(query, context, 1)
    finally:
        callbacks._run_analysis_for_ticker = orig

    assert recorded["force_refresh"] is False, recorded
    assert "watch_mode" not in context.chat_data, context.chat_data
    assert "watch_selection" not in context.chat_data, context.chat_data


async def test_handle_cancel_pops_watch_state_only_for_watch_what() -> None:
    """`_handle_cancel` re-pops the same three picker keys, but ONLY when
    `what == "watch"` (the ❌ Cancel inside the picker). Any other flow
    (e.g. `del`) must leave the picker state untouched — picker cleanup
    is mode-scoped, never global. Pins both sides of that branch."""
    import tg_bot.handlers.callbacks as callbacks

    # what == "watch" → all three popped (mirrors _handle_done's cleanup).
    query = _FakeQuery()
    context = SimpleNamespace(
        chat_data={
            "watch_mode": "refresh",
            "watch_selection": {"NVDA"},
            "watch_page": 1,
        }
    )
    await callbacks._handle_cancel(context, query, 1, "watch")
    assert "watch_mode" not in context.chat_data, context.chat_data
    assert "watch_selection" not in context.chat_data, context.chat_data
    assert "watch_page" not in context.chat_data, context.chat_data

    # what != "watch" (e.g. "del") → the three keys SURVIVE untouched.
    query2 = _FakeQuery()
    context2 = SimpleNamespace(
        chat_data={
            "watch_mode": "watch",
            "watch_selection": {"AAPL"},
            "watch_page": 3,
        }
    )
    await callbacks._handle_cancel(context2, query2, 1, "del")
    assert context2.chat_data["watch_mode"] == "watch", context2.chat_data
    assert context2.chat_data["watch_selection"] == {"AAPL"}, context2.chat_data
    assert context2.chat_data["watch_page"] == 3, context2.chat_data
