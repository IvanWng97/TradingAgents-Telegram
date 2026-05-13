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
    Returns the live `commands` module so callers can use the rebuilt
    references."""
    import importlib

    for mod in [
        "tg_bot.storage",
        "tg_bot.storage.user_config",
        "tg_bot.storage.watchlist",
        "tg_bot.handlers.commands",
    ]:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    from tg_bot.handlers import commands

    return commands


# ─── picker rendering ──────────────────────────────────────────────────


async def test_watch_mode_header_and_done_label() -> None:
    _fresh_data_dir()
    _seed_storage(["NVDA", "AAPL"])
    commands = _reload_storage_singletons()
    text, kb = commands.build_watchlist_response(
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
    text, kb = commands.build_watchlist_response(
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
    _, kb_watch = commands.build_watchlist_response(
        "1", selected=set(), page=0, mode="watch"
    )
    _, kb_refresh = commands.build_watchlist_response(
        "1", selected=set(), page=0, mode="refresh"
    )
    # Same number of rows.
    assert len(kb_watch.inline_keyboard) == len(kb_refresh.inline_keyboard)
    # All ticker rows + pagination + bulk-select rows have identical
    # callback_data — only the last row's first button (Done) text differs.
    for row_w, row_r in zip(
        kb_watch.inline_keyboard[:-1], kb_refresh.inline_keyboard[:-1]
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
        text, kb = commands.build_watchlist_response(
            "1", selected=set(), page=0, mode=mode
        )
        assert kb is None, f"{mode}: expected None keyboard, got {kb}"
        assert "empty" in text.lower(), text


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
