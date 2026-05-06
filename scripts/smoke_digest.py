"""Smoke tests for the daily-digest flow.

Each scenario uses a fresh temporary `user_config.json` so storage state
is isolated. Tests grow with the feature — currently covers the storage
layer; picker rendering, JobQueue registration, and fan-out execution
get appended as those land on the branch.

Storage scenarios:
  - Empty state: get_digest returns None for an unknown user.
  - Bad inputs: hour=24 and unknown IANA tz are both rejected.
  - First-time flow: set_digest_tz alone leaves the digest disabled.
  - Hour tap: set_digest_hour enables and captures chat_id.
  - iter_enabled_digests filters partial / disabled rows.
  - Active digest survives a tz change (enabled stays True).
  - disable_digest preserves hour + tz for one-tap re-enable.
  - Re-enable via hour tap restores enabled=True.
  - clear() (LLM rollback) preserves the digest block.
  - clear() drops the user entry only when nothing else is left.
  - Persistence round-trip: data survives a re-instantiation.

Picker rendering scenarios:
  - First-time render → tz picker (no Back button, has all 10 zones).
  - After tz only → hour picker, all hours unmarked, no 🔕 Off button.
  - After hour set → hour picker with ✅ on the chosen hour, 🔕 Off shown.
  - Mode override "tz" on a fully-configured user shows tz picker with
    ✅ on the active zone and a Back button.
  - Status line variants: pre-tz, post-tz pre-hour, OFF, ON.
  - Time math: next_fire wraps to tomorrow when target already passed today.

Callback dispatch scenarios (storage-only — no Telegram side effects asserted):
  - `digest:hour:10` enables digest, captures chat_id.
  - `digest:tz:<IANA>` sets tz; doesn't touch enabled.
  - `digest:tzpick` / `digest:hourpick` are pure mode swaps (no storage write).
  - `digest:off` flips enabled to False without losing hour/tz.
  - Bad inputs (non-int hour, garbage tz) are no-ops.

Run with: .venv/bin/python3 scripts/smoke_digest.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from tg_bot.digest import (  # noqa: E402
    TZ_OPTIONS,
    build_digest_response,
    humanize_delta,
    next_fire,
)
from tg_bot.storage.user_config import UserConfigStorage  # noqa: E402


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


# --- Helpers ---------------------------------------------------------------


def fresh_storage() -> tuple[UserConfigStorage, Path]:
    """Return a UserConfigStorage backed by a fresh temp file. Caller is
    responsible for cleaning up via the returned dir's tempfile context, or
    just ignoring it (process exit cleans /tmp eventually)."""
    tmp = tempfile.mkdtemp(prefix="smoke-digest-")
    path = Path(tmp) / "user_config.json"
    return UserConfigStorage(path), path


# --- Storage scenarios -----------------------------------------------------


async def test_empty_state() -> None:
    s, _ = fresh_storage()
    assert s.get_digest("42") is None
    assert s.iter_enabled_digests() == []


async def test_bad_inputs_rejected() -> None:
    s, _ = fresh_storage()
    assert not await s.set_digest_hour("42", -1, 999)
    assert not await s.set_digest_hour("42", 24, 999)
    assert not await s.set_digest_tz("42", "Mars/Phobos")
    assert not await s.set_digest_tz("42", "Not/A/Real/Zone")
    # No partial state should have been written.
    assert s.get_digest("42") is None


async def test_first_time_tz_then_hour() -> None:
    """Mirrors the first-time picker flow: user picks tz first, then hour."""
    s, _ = fresh_storage()
    assert await s.set_digest_tz("42", "America/Los_Angeles")
    d = s.get_digest("42")
    assert d == {
        "enabled": False,
        "hour_local": None,
        "tz": "America/Los_Angeles",
        "chat_id": None,
    }, d
    # Tz set but not yet enabled — partial config, doesn't fire.
    assert s.iter_enabled_digests() == []
    # Hour tap completes the setup.
    assert await s.set_digest_hour("42", 10, 999)
    d = s.get_digest("42")
    assert d["enabled"] and d["hour_local"] == 10 and d["chat_id"] == 999
    assert s.iter_enabled_digests() == [("42", d)]


async def test_iter_enabled_filters_partial() -> None:
    s, _ = fresh_storage()
    # Fully configured + enabled.
    assert await s.set_digest_tz("aaa", "America/New_York")
    assert await s.set_digest_hour("aaa", 9, 100)
    # Partial: tz only.
    assert await s.set_digest_tz("bbb", "America/New_York")
    # Partial: disabled (had been enabled, then turned off).
    assert await s.set_digest_tz("ccc", "America/New_York")
    assert await s.set_digest_hour("ccc", 12, 300)
    assert await s.disable_digest("ccc")
    enabled = s.iter_enabled_digests()
    user_ids = [uid for uid, _ in enabled]
    assert user_ids == ["aaa"], f"only fully-configured + enabled; got {user_ids}"


async def test_tz_change_keeps_active() -> None:
    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)
    assert s.get_digest("42")["enabled"]
    # Changing tz on an active digest must not flip enabled.
    assert await s.set_digest_tz("42", "Europe/London")
    d = s.get_digest("42")
    assert d["enabled"] and d["tz"] == "Europe/London" and d["hour_local"] == 10


async def test_disable_preserves_hour_tz() -> None:
    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)
    await s.disable_digest("42")
    d = s.get_digest("42")
    assert d["enabled"] is False
    assert d["hour_local"] == 10
    assert d["tz"] == "America/Los_Angeles"
    assert d["chat_id"] == 999  # not cleared either
    assert s.iter_enabled_digests() == []


async def test_reenable_via_hour_tap() -> None:
    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)
    await s.disable_digest("42")
    # User taps a different hour to re-enable.
    assert await s.set_digest_hour("42", 7, 999)
    d = s.get_digest("42")
    assert d["enabled"] and d["hour_local"] == 7
    assert s.iter_enabled_digests() == [("42", d)]


async def test_clear_preserves_digest() -> None:
    """The /config Cancel rollback must not wipe an unrelated digest."""
    s, _ = fresh_storage()
    await s.set_llm_provider("42", "deepseek")
    await s.set_llm_model("42", "deep", "deepseek-v4-pro")
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)
    await s.clear("42")
    bucket = s._data.get("42", {})
    assert "llm_provider" not in bucket
    assert "deep_think_llm" not in bucket
    assert "digest" in bucket, "clear() must preserve the digest block"
    assert bucket["digest"]["enabled"]


async def test_clear_drops_empty_user() -> None:
    """A user with only LLM config (no digest) gets fully removed by clear()."""
    s, _ = fresh_storage()
    await s.set_llm_provider("77", "openai")
    assert "77" in s._data
    await s.clear("77")
    assert "77" not in s._data


async def test_persistence_round_trip() -> None:
    """Data must survive a re-instantiation (atomic writes hit disk)."""
    s1, path = fresh_storage()
    await s1.set_digest_tz("42", "America/Los_Angeles")
    await s1.set_digest_hour("42", 10, 999)
    # Re-instantiate from the same path — should re-load identical state.
    s2 = UserConfigStorage(path)
    d = s2.get_digest("42")
    assert d == s1.get_digest("42"), (
        f"reload mismatch:\n  before={s1.get_digest('42')}\n  after ={d}"
    )
    # And the on-disk JSON is well-formed.
    raw = json.loads(path.read_text())
    assert raw["42"]["digest"]["tz"] == "America/Los_Angeles"


# --- Picker rendering scenarios -------------------------------------------


def _flatten(kb) -> list[tuple[str, str]]:
    """Flatten an InlineKeyboardMarkup to [(text, callback_data), ...]."""
    return [(b.text, b.callback_data) for row in kb.inline_keyboard for b in row]


def _has_callback(kb, prefix: str) -> bool:
    return any(cb.startswith(prefix) for _, cb in _flatten(kb))


async def test_picker_first_time() -> None:
    """No tz set → tz picker. No Back button (nowhere to go back). All 10 zones."""
    text, kb = build_digest_response(None)
    assert "pick a time zone" in text or "Tap a zone" in text, text
    cbs = [cb for _, cb in _flatten(kb)]
    assert sum(cb.startswith("digest:tz:") for cb in cbs) == len(TZ_OPTIONS)
    assert "digest:hourpick" not in cbs, "Back button shouldn't appear pre-hour"
    assert "cancel:digest" in cbs


async def test_picker_after_tz_only() -> None:
    """Tz set, no hour yet → hour picker, no ✅ on hours, no 🔕 Off."""
    digest = {
        "enabled": False,
        "hour_local": None,
        "tz": "America/Los_Angeles",
        "chat_id": None,
    }
    text, kb = build_digest_response(digest)
    assert "OFF" in text, text
    flat = _flatten(kb)
    hour_btns = [(t, c) for t, c in flat if c.startswith("digest:hour:")]
    assert len(hour_btns) == 24
    assert all("✅" not in t for t, _ in hour_btns), "no hour selected yet"
    assert not _has_callback(kb, "digest:off"), "🔕 Off must be hidden when disabled"
    assert _has_callback(kb, "digest:tzpick")
    assert _has_callback(kb, "digest:run")


async def test_picker_active() -> None:
    """Active digest → ✅ on chosen hour, 🔕 Off shown, ON status line."""
    digest = {
        "enabled": True,
        "hour_local": 10,
        "tz": "America/Los_Angeles",
        "chat_id": 999,
    }
    text, kb = build_digest_response(digest)
    assert "ON" in text and "10:00 PT" in text, text
    flat = _flatten(kb)
    h10 = next(t for t, c in flat if c == "digest:hour:10")
    assert "✅" in h10, f"hour 10 should be marked: {h10}"
    # Other hours unmarked.
    h11 = next(t for t, c in flat if c == "digest:hour:11")
    assert "✅" not in h11
    assert _has_callback(kb, "digest:off")


async def test_picker_tz_override() -> None:
    """mode='tz' on a fully-configured user shows tz picker + Back button."""
    digest = {
        "enabled": True,
        "hour_local": 10,
        "tz": "America/Los_Angeles",
        "chat_id": 999,
    }
    text, kb = build_digest_response(digest, mode="tz")
    flat = _flatten(kb)
    # Active tz marked.
    pt = next(t for t, c in flat if c == "digest:tz:America/Los_Angeles")
    assert "✅" in pt
    # Back button present (hour was set, so there's somewhere to return to).
    assert _has_callback(kb, "digest:hourpick")


async def test_status_line_variants() -> None:
    pre_tz, _ = build_digest_response(None)
    assert "pick a time zone" in pre_tz

    post_tz_no_hour, _ = build_digest_response(
        {"enabled": False, "hour_local": None, "tz": "Etc/UTC", "chat_id": None}
    )
    assert "OFF" in post_tz_no_hour and "UTC" in post_tz_no_hour

    off_after_setup, _ = build_digest_response(
        {"enabled": False, "hour_local": 10, "tz": "Etc/UTC", "chat_id": 999}
    )
    assert "OFF" in off_after_setup and "last set" in off_after_setup

    on, _ = build_digest_response(
        {"enabled": True, "hour_local": 10, "tz": "Etc/UTC", "chat_id": 999}
    )
    assert "ON" in on


# --- Callback dispatch scenarios ------------------------------------------


class _FakeMessage:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


class _FakeQuery:
    """Minimal stand-in for telegram.CallbackQuery — only what _handle_digest reads."""

    def __init__(self, chat_id: int) -> None:
        self.message = _FakeMessage(chat_id)
        self.last_text: str | None = None
        self.last_kb = None

    async def edit_message_text(
        self, text: str, parse_mode: str | None = None, reply_markup=None
    ) -> None:
        self.last_text = text
        self.last_kb = reply_markup


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_: object) -> None:
        self.sent.append((chat_id, text))


class _FakeContext:
    """Minimal stand-in for ContextTypes.DEFAULT_TYPE — _handle_digest only
    reads `context.bot` for the run-now stub message."""

    def __init__(self) -> None:
        self.bot = _FakeBot()


async def _make_dispatch_env(chat_id: int = 999) -> tuple[Any, Any, Any, Any]:
    """Patches the storage singleton in callbacks to a temp-file UCS so
    dispatch tests don't bleed into the on-disk data dir. Returns
    (storage, query, context, restore_fn)."""
    s, _ = fresh_storage()
    # callbacks reads `user_config_storage` directly — swap it.
    from tg_bot.handlers import callbacks as cbmod

    original = cbmod.user_config_storage
    cbmod.user_config_storage = s
    return (
        s,
        _FakeQuery(chat_id),
        _FakeContext(),
        lambda: setattr(cbmod, "user_config_storage", original),
    )


async def test_callback_hour_enables() -> None:
    s, q, ctx, restore = await _make_dispatch_env()
    try:
        from tg_bot.handlers.callbacks import _handle_digest

        await _handle_digest(q, ctx, user_id=42, data="digest:hour:10")
        d = s.get_digest("42")
        assert d["enabled"] and d["hour_local"] == 10 and d["chat_id"] == 999
        assert q.last_text is not None  # picker re-rendered
    finally:
        restore()


async def test_callback_tz_no_enable() -> None:
    s, q, ctx, restore = await _make_dispatch_env()
    try:
        from tg_bot.handlers.callbacks import _handle_digest

        await _handle_digest(q, ctx, user_id=42, data="digest:tz:America/Los_Angeles")
        d = s.get_digest("42")
        assert d["tz"] == "America/Los_Angeles"
        assert d["enabled"] is False  # tz alone doesn't enable
    finally:
        restore()


async def test_callback_pure_mode_swap() -> None:
    """tzpick / hourpick must NOT write to storage — they only redraw."""
    s, q, ctx, restore = await _make_dispatch_env()
    try:
        from tg_bot.handlers.callbacks import _handle_digest

        # Pre-existing state.
        await s.set_digest_tz("42", "America/Los_Angeles")
        await s.set_digest_hour("42", 10, 999)
        snapshot = json.dumps(s.get_digest("42"), sort_keys=True)
        await _handle_digest(q, ctx, user_id=42, data="digest:tzpick")
        await _handle_digest(q, ctx, user_id=42, data="digest:hourpick")
        assert json.dumps(s.get_digest("42"), sort_keys=True) == snapshot
    finally:
        restore()


async def test_callback_off_preserves() -> None:
    s, q, ctx, restore = await _make_dispatch_env()
    try:
        from tg_bot.handlers.callbacks import _handle_digest

        await s.set_digest_tz("42", "America/Los_Angeles")
        await s.set_digest_hour("42", 10, 999)
        await _handle_digest(q, ctx, user_id=42, data="digest:off")
        d = s.get_digest("42")
        assert d["enabled"] is False
        assert d["hour_local"] == 10  # preserved
        assert d["tz"] == "America/Los_Angeles"  # preserved
    finally:
        restore()


async def test_callback_bad_inputs_noop() -> None:
    s, q, ctx, restore = await _make_dispatch_env()
    try:
        from tg_bot.handlers.callbacks import _handle_digest

        # Non-int hour — should not write.
        await _handle_digest(q, ctx, user_id=42, data="digest:hour:abc")
        assert s.get_digest("42") is None
        # Garbage tz — should not write.
        await _handle_digest(q, ctx, user_id=42, data="digest:tz:Mars/Phobos")
        assert s.get_digest("42") is None
    finally:
        restore()


async def test_next_fire_wrap() -> None:
    """If now is already past today's target hour, fire wraps to tomorrow."""
    pt = ZoneInfo("America/Los_Angeles")
    # 11:00 PT now, target 10:00 → tomorrow 10:00.
    now = datetime(2026, 5, 6, 11, 0, tzinfo=pt)
    fire = next_fire(10, "America/Los_Angeles", now=now)
    assert fire.day == 7, fire
    assert fire.hour == 10
    # 09:00 PT now, target 10:00 → today 10:00.
    now2 = datetime(2026, 5, 6, 9, 0, tzinfo=pt)
    fire2 = next_fire(10, "America/Los_Angeles", now=now2)
    assert fire2.day == 6 and fire2.hour == 10
    # humanize: 4h delta from 06:00 to 10:00.
    now3 = datetime(2026, 5, 6, 6, 0, tzinfo=pt)
    fire3 = next_fire(10, "America/Los_Angeles", now=now3)
    assert humanize_delta(fire3, now=now3) == "in 4h"


# --- Runner ----------------------------------------------------------------


SCENARIOS = [
    ("empty state", test_empty_state),
    ("bad inputs rejected", test_bad_inputs_rejected),
    ("first-time tz → hour flow", test_first_time_tz_then_hour),
    ("iter_enabled filters partial", test_iter_enabled_filters_partial),
    ("tz change keeps active", test_tz_change_keeps_active),
    ("disable preserves hour+tz", test_disable_preserves_hour_tz),
    ("re-enable via hour tap", test_reenable_via_hour_tap),
    ("clear() preserves digest", test_clear_preserves_digest),
    ("clear() drops empty user", test_clear_drops_empty_user),
    ("persistence round-trip", test_persistence_round_trip),
    ("picker first-time = tz screen", test_picker_first_time),
    ("picker after tz only = hour screen, no ✅", test_picker_after_tz_only),
    ("picker active = hour screen, ✅ + 🔕 Off", test_picker_active),
    ("picker mode='tz' override + Back button", test_picker_tz_override),
    ("status line variants", test_status_line_variants),
    ("next_fire / humanize_delta math", test_next_fire_wrap),
    ("callback digest:hour enables", test_callback_hour_enables),
    ("callback digest:tz keeps disabled", test_callback_tz_no_enable),
    ("callback tzpick/hourpick = pure mode swap", test_callback_pure_mode_swap),
    ("callback digest:off preserves hour+tz", test_callback_off_preserves),
    ("callback bad inputs are no-ops", test_callback_bad_inputs_noop),
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
