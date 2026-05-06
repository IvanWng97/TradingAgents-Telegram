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

JobQueue + fan-out scenarios:
  - register_digest_job adds a job under the canonical name.
  - Re-registering replaces (doesn't duplicate) — name unique.
  - cancel_digest_job removes all jobs for the user.
  - Disabled / partial digests are no-ops (no job scheduled).
  - run_user_digest with an empty watchlist sends nothing.
  - Forbidden on header send → digest auto-disables, job cancels.
  - Successful run: header sent, fanned-out tickers analyzed via the
    semaphore, summary message edits the header in place.

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
    """Minimal stand-in for ContextTypes.DEFAULT_TYPE. `_handle_digest`
    reads `context.bot` and `context.application.job_queue` (via
    register_digest_job / cancel_digest_job)."""

    def __init__(self) -> None:
        self.bot = _FakeBot()
        # _FakeFanOutApp gives us a job_queue with the right interface.
        self.application = _FakeFanOutApp()


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


# --- JobQueue + fan-out scenarios -----------------------------------------


class _FakeJob:
    def __init__(self, name: str, callback, time_, data: dict) -> None:
        self.name = name
        self.callback = callback
        self.time = time_
        self.data = data
        self.removed = False

    def schedule_removal(self) -> None:
        self.removed = True


class _FakeJobQueue:
    def __init__(self) -> None:
        self._jobs: list[_FakeJob] = []

    def run_daily(self, callback, time, name, data) -> _FakeJob:
        job = _FakeJob(name, callback, time, data)
        self._jobs.append(job)
        return job

    def get_jobs_by_name(self, name: str):
        # PTB's API returns active jobs; filter out scheduled-for-removal.
        return [j for j in self._jobs if j.name == name and not j.removed]


class _FakeFanOutBot:
    """Records send/edit calls. Optionally raises Forbidden on send_message."""

    def __init__(self, forbidden: bool = False) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.markup_edits: list[dict] = []
        self._next_id = 5000
        self._forbidden = forbidden

    async def send_message(self, chat_id, text, **kw):
        if self._forbidden:
            from telegram.error import Forbidden

            raise Forbidden("user blocked the bot")
        mid = self._next_id
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "message_id": mid, **kw})

        # Return an object with .message_id (matches Telegram's Message).
        class _M:
            pass

        m = _M()
        m.message_id = mid
        return m

    async def edit_message_text(self, chat_id, message_id, text, **kw):
        self.edits.append(
            {"chat_id": chat_id, "message_id": message_id, "text": text, **kw}
        )

    async def edit_message_reply_markup(self, chat_id, message_id, **kw):
        self.markup_edits.append({"chat_id": chat_id, "message_id": message_id, **kw})


class _FakeFanOutApp:
    def __init__(self, forbidden: bool = False) -> None:
        self.bot = _FakeFanOutBot(forbidden=forbidden)
        self.job_queue = _FakeJobQueue()
        # PTB's Application.chat_data is a defaultdict-like mapping per
        # chat. The fake just hands back a regular dict per-chat.
        self.chat_data: dict[int, dict] = _ChatDataDict()


class _ChatDataDict(dict):
    """Minimal stand-in for PTB's chat_data — auto-creates {} on access."""

    def __getitem__(self, key):  # type: ignore[override]
        if key not in self:
            self[key] = {}
        return super().__getitem__(key)


async def test_register_adds_job() -> None:
    from tg_bot.handlers.callbacks import register_digest_job

    app = _FakeFanOutApp()
    digest = {
        "enabled": True,
        "hour_local": 10,
        "tz": "America/Los_Angeles",
        "chat_id": 999,
    }
    register_digest_job(app, 42, digest)
    jobs = app.job_queue.get_jobs_by_name("digest:42")
    assert len(jobs) == 1
    assert jobs[0].time.hour == 10
    assert str(jobs[0].time.tzinfo) == "America/Los_Angeles"
    assert jobs[0].data == {"user_id": 42, "chat_id": 999}


async def test_register_replaces_existing() -> None:
    from tg_bot.handlers.callbacks import register_digest_job

    app = _FakeFanOutApp()
    digest = {
        "enabled": True,
        "hour_local": 10,
        "tz": "America/Los_Angeles",
        "chat_id": 999,
    }
    register_digest_job(app, 42, digest)
    register_digest_job(app, 42, {**digest, "hour_local": 14})
    jobs = app.job_queue.get_jobs_by_name("digest:42")
    assert len(jobs) == 1, "must not duplicate"
    assert jobs[0].time.hour == 14


async def test_cancel_removes_jobs() -> None:
    from tg_bot.handlers.callbacks import cancel_digest_job, register_digest_job

    app = _FakeFanOutApp()
    digest = {
        "enabled": True,
        "hour_local": 10,
        "tz": "America/Los_Angeles",
        "chat_id": 999,
    }
    register_digest_job(app, 42, digest)
    n = cancel_digest_job(app, 42)
    assert n == 1
    assert app.job_queue.get_jobs_by_name("digest:42") == []


async def test_register_noop_on_incomplete() -> None:
    from tg_bot.handlers.callbacks import register_digest_job

    app = _FakeFanOutApp()
    # Disabled.
    register_digest_job(
        app,
        42,
        {
            "enabled": False,
            "hour_local": 10,
            "tz": "America/Los_Angeles",
            "chat_id": 999,
        },
    )
    assert app.job_queue.get_jobs_by_name("digest:42") == []
    # Partial — no chat_id.
    register_digest_job(
        app,
        42,
        {
            "enabled": True,
            "hour_local": 10,
            "tz": "America/Los_Angeles",
            "chat_id": None,
        },
    )
    assert app.job_queue.get_jobs_by_name("digest:42") == []


async def test_fanout_empty_watchlist_silent() -> None:
    """User with empty watchlist: no header sent, no edits, returns cleanly."""
    from tg_bot.handlers import callbacks as cbmod

    # Patch in an empty watchlist storage.
    class _W:
        def get_watchlist(self, _uid):
            return []

    original = cbmod.watchlist_storage
    cbmod.watchlist_storage = _W()
    try:
        app = _FakeFanOutApp()
        await cbmod.run_user_digest(app, 42, 999)
        assert app.bot.sent == [] and app.bot.edits == []
    finally:
        cbmod.watchlist_storage = original


async def test_fanout_forbidden_disables() -> None:
    """Header send fails with Forbidden → digest disabled + job cancelled."""
    from tg_bot.handlers import callbacks as cbmod

    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA"]

    orig_w = cbmod.watchlist_storage
    orig_uc = cbmod.user_config_storage
    cbmod.watchlist_storage = _W()
    cbmod.user_config_storage = s
    try:
        app = _FakeFanOutApp(forbidden=True)
        # Pre-register a job to verify it gets cancelled.
        cbmod.register_digest_job(app, 42, s.get_digest("42"))
        assert len(app.job_queue.get_jobs_by_name("digest:42")) == 1

        await cbmod.run_user_digest(app, 42, 999)

        d = s.get_digest("42")
        assert d["enabled"] is False, "digest should auto-disable on Forbidden"
        assert app.job_queue.get_jobs_by_name("digest:42") == [], (
            "job should be cancelled"
        )
    finally:
        cbmod.watchlist_storage = orig_w
        cbmod.user_config_storage = orig_uc


async def test_fanout_summary_renders() -> None:
    """Happy path: header sent in pending state, final edit shows the
    signal-emoji summary with Telegraph links + failure tally."""
    from tg_bot.handlers import callbacks as cbmod

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA", "AAPL", "TSLA"]

    canned: dict[str, dict | None] = {
        "NVDA": {
            "ticker": "NVDA",
            "signal": "BUY",
            "telegraph_url": "https://telegra.ph/NVDA",
        },
        "AAPL": {"ticker": "AAPL", "signal": "HOLD", "telegraph_url": None},
        "TSLA": None,  # simulate one failure
    }

    async def _fake_analyze(_uid, ticker, reporter=None):
        # Mimic the real analyze's per-step callback so the digest's
        # progress view exercises the analyzing-state branch.
        if reporter is not None:
            await reporter.report("market analyst")
        return canned[ticker]

    orig_w = cbmod.watchlist_storage
    orig_a = cbmod._analyze_one_for_digest
    cbmod.watchlist_storage = _W()
    cbmod._analyze_one_for_digest = _fake_analyze
    try:
        app = _FakeFanOutApp()
        await cbmod.run_user_digest(app, 42, 999)

        # Initial header lists every ticker as ⏳ pending.
        assert len(app.bot.sent) == 1
        header_text = app.bot.sent[0]["text"]
        assert "0/3" in header_text
        assert "⏳" in header_text
        assert all(t in header_text for t in ["NVDA", "AAPL", "TSLA"])

        # Final summary edit (last edit overwrites any progress edits).
        assert len(app.bot.edits) >= 1
        body = app.bot.edits[-1]["text"]
        assert "🟢" in body and "NVDA" in body and "BUY" in body
        assert "🟡" in body and "AAPL" in body and "HOLD" in body
        assert "❓" in body and "TSLA" in body and "error" in body
        # New tally format: "{cancelled, failed} of N"
        assert "1 failed" in body and "of 3" in body
        # Telegraph link in NVDA row.
        assert "📄" in body
    finally:
        cbmod.watchlist_storage = orig_w
        cbmod._analyze_one_for_digest = orig_a


async def test_render_deferred_captures_latest_state() -> None:
    """When two tickers fire status changes within the throttle window,
    the second one mustn't get silently dropped — the deferred render
    must paint the latest state once the throttle expires. Patches
    _DIGEST_PROGRESS_INTERVAL down so the test runs in <1s instead of
    >2s."""
    from tg_bot.handlers import callbacks as cbmod

    class _W:
        def get_watchlist(self, _u):
            return ["NVDA", "AAPL"]

    started = {"NVDA": False, "AAPL": False}

    async def _fake(_uid, ticker, reporter=None):
        # Fire `report_starting` on both nearly simultaneously — this is
        # the exact race the deferred render is meant to handle.
        if reporter is not None:
            await reporter.report_starting()
        started[ticker] = True
        # Hold long enough for the deferred render to land.
        await asyncio.sleep(0.6)
        return {"ticker": ticker, "signal": "BUY", "telegraph_url": None}

    orig_w = cbmod.watchlist_storage
    orig_a = cbmod._analyze_one_for_digest
    orig_iv = cbmod._DIGEST_PROGRESS_INTERVAL
    cbmod.watchlist_storage = _W()
    cbmod._analyze_one_for_digest = _fake
    cbmod._DIGEST_PROGRESS_INTERVAL = 0.3
    try:
        app = _FakeFanOutApp()
        await cbmod.run_user_digest(app, 42, 999)
        # At least one progress edit must contain BOTH tickers in the
        # 📊 state simultaneously — without the deferred-render fix,
        # only the first ticker's transition would ever appear in any
        # progress edit; the second one would race past Starting and
        # only show up in the final summary.
        progress_edits = [e["text"] for e in app.bot.edits if "📊" in e["text"]]
        assert progress_edits, "no progress edit ever rendered 📊"
        both_visible = [
            t
            for t in progress_edits
            if "NVDA" in t and "AAPL" in t and t.count("📊") >= 2
        ]
        assert both_visible, (
            "no progress edit showed both tickers as 📊 simultaneously — "
            "deferred render didn't capture the second state change"
        )
    finally:
        cbmod.watchlist_storage = orig_w
        cbmod._analyze_one_for_digest = orig_a
        cbmod._DIGEST_PROGRESS_INTERVAL = orig_iv


async def test_progress_completed_row_matches_summary() -> None:
    """A completed ticker should render identically in the progress
    view and the final summary — same signal emoji + Telegraph link, no
    flash from generic ✅ to coloured 🟢 at the final-edit boundary."""
    from tg_bot.handlers.callbacks import (
        _format_digest_progress,
        _format_digest_summary,
    )

    result = {
        "ticker": "NVDA",
        "signal": "BUY",
        "telegraph_url": "https://telegra.ph/NVDA",
    }
    status = {"NVDA": result}
    progress = _format_digest_progress(["NVDA"], status, "2026-05-06")
    summary = _format_digest_summary(["NVDA"], status, "2026-05-06")
    # Both must contain: the signal emoji, the BUY label, and the link.
    for body in (progress, summary):
        assert "🟢" in body, f"missing emoji in: {body!r}"
        assert "BUY" in body
        assert "📄" in body and "telegra.ph/NVDA" in body
    # Generic ✅ must NOT appear once the row is rendered as completed.
    assert "✅ *NVDA*" not in progress


async def test_iter_enabled_skips_malformed_entries() -> None:
    """A corrupt `digest` block (or non-dict bucket) must skip and
    continue, not tank the whole _post_init walk for everyone."""
    s, _ = fresh_storage()
    # Inject malformed shapes directly into _data — bypassing the
    # async setters (which would have rejected them).
    s._data["good"] = {
        "digest": {
            "enabled": True,
            "hour_local": 10,
            "tz": "America/Los_Angeles",
            "chat_id": 999,
        }
    }
    s._data["bad-digest-shape"] = {"digest": "not-a-dict"}
    s._data["bad-bucket-shape"] = ["this isn't even a dict"]
    s._data["empty"] = {}

    enabled = s.iter_enabled_digests()
    user_ids = [uid for uid, _ in enabled]
    assert user_ids == ["good"], f"only 'good' should survive; got {user_ids}"


async def test_fanout_cancel_pending_via_task_cancel() -> None:
    """Cancel while tickers haven't yet acquired the semaphore exercises
    the `Task.cancel()` path (asyncio.CancelledError), not the
    threading.Event path. Cap=1 so only one ticker enters the analyzing
    state; the rest sit in `await sem.acquire()` and unwind via
    Task.cancel."""
    import threading

    from tg_bot.handlers import callbacks as cbmod

    class _W:
        def get_watchlist(self, _u):
            return ["A", "B", "C"]

    started = threading.Event()
    release = threading.Event()
    a_b_c_calls: list[str] = []

    # Force a fresh sem with cap=1 so only one ticker can be analyzing.
    orig_sem = cbmod._run_semaphore
    cbmod._run_semaphore = asyncio.Semaphore(1)

    async def _slow(_uid, ticker, reporter=None):
        # Mimic _analyze_one_for_digest's sem.acquire so the test
        # actually exercises the pending → cancelled path.
        sem = cbmod._get_run_semaphore()
        await sem.acquire()
        try:
            a_b_c_calls.append(ticker)
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
            from tg_bot.progress import CancelledByUserError

            raise CancelledByUserError("simulated mid-flight cancel")
        finally:
            sem.release()

    orig_w = cbmod.watchlist_storage
    orig_a = cbmod._analyze_one_for_digest
    cbmod.watchlist_storage = _W()
    cbmod._analyze_one_for_digest = _slow
    try:
        app = _FakeFanOutApp()
        run_task = asyncio.create_task(cbmod.run_user_digest(app, 42, 999))

        # Wait for ticker A to be in flight.
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.02)
        assert started.is_set(), "first ticker never started"

        # Tap cancel — A is in flight (cancel_event path), B and C are
        # awaiting sem.acquire (Task.cancel path).
        digest_cancels = app.chat_data[999]["digest_cancels"]
        msg_id = next(iter(digest_cancels.keys()))

        class _Ctx:
            def __init__(self, cd):
                self.chat_data = cd

        await cbmod._handle_digest_cancel(
            _Ctx(app.chat_data[999]), object(), str(msg_id)
        )
        release.set()
        await run_task

        # B and C never got a chance to call their fake analyzer — they
        # were cancelled while waiting on the semaphore.
        assert a_b_c_calls == ["A"], f"only A should have run; saw {a_b_c_calls}"
        # Final summary must show all three as cancelled.
        body = app.bot.edits[-1]["text"]
        assert "3 cancelled" in body and "of 3" in body
    finally:
        cbmod.watchlist_storage = orig_w
        cbmod._analyze_one_for_digest = orig_a
        cbmod._run_semaphore = orig_sem


async def test_fanout_forbidden_during_progress_edit() -> None:
    """Initial header send succeeds, then the FIRST progress edit fails
    with Forbidden. Digest must auto-disable, JobQueue job must cancel,
    and the in-flight fan-out must abort (cancel_event set + tasks
    cancelled) instead of grinding through every ticker silently."""
    from tg_bot.handlers import callbacks as cbmod

    class _ForbiddenOnFirstEdit(_FakeFanOutBot):
        def __init__(self):
            super().__init__()
            self._edits_seen = 0

        async def edit_message_text(self, chat_id, message_id, text, **kw):
            self._edits_seen += 1
            if self._edits_seen == 1:
                from telegram.error import Forbidden

                raise Forbidden("user blocked the bot mid-run")
            return await super().edit_message_text(chat_id, message_id, text, **kw)

    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)

    class _W:
        def get_watchlist(self, _u):
            return ["A", "B", "C"]

    async def _slow(_uid, _t, reporter=None):
        # Trigger an edit by reporting a step.
        if reporter is not None:
            await reporter.report_starting()
        # Then sleep so cancel can interrupt.
        for _ in range(50):
            await asyncio.sleep(0.05)
            if reporter and reporter.cancel_event and reporter.cancel_event.is_set():
                from tg_bot.progress import CancelledByUserError

                raise CancelledByUserError("forbidden auto-cancel")
        return {"ticker": _t, "signal": "BUY", "telegraph_url": None}

    orig_w = cbmod.watchlist_storage
    orig_a = cbmod._analyze_one_for_digest
    orig_uc = cbmod.user_config_storage
    orig_iv = cbmod._DIGEST_PROGRESS_INTERVAL
    cbmod.watchlist_storage = _W()
    cbmod._analyze_one_for_digest = _slow
    cbmod.user_config_storage = s
    cbmod._DIGEST_PROGRESS_INTERVAL = 0.1
    try:
        app = _FakeFanOutApp()
        app.bot = _ForbiddenOnFirstEdit()
        # Pre-register a JobQueue entry so cancel_digest_job has work to do.
        cbmod.register_digest_job(app, 42, s.get_digest("42"))
        assert len(app.job_queue.get_jobs_by_name("digest:42")) == 1

        await cbmod.run_user_digest(app, 42, 999)

        # Auto-disabled.
        assert s.get_digest("42")["enabled"] is False
        # JobQueue job cancelled.
        assert app.job_queue.get_jobs_by_name("digest:42") == []
    finally:
        cbmod.watchlist_storage = orig_w
        cbmod._analyze_one_for_digest = orig_a
        cbmod.user_config_storage = orig_uc
        cbmod._DIGEST_PROGRESS_INTERVAL = orig_iv


async def test_progress_renders_cancelled() -> None:
    """`_format_digest_progress` renders the ⛔ cancelled state."""
    from tg_bot.handlers.callbacks import _format_digest_progress

    status = {"NVDA": "cancelled", "AAPL": "pending"}
    out = _format_digest_progress(["NVDA", "AAPL"], status, "2026-05-06")
    assert "⛔" in out and "NVDA" in out and "cancelled" in out
    assert "⏳" in out and "AAPL" in out


async def test_summary_tally_includes_cancelled() -> None:
    """`_format_digest_summary` shows a 'X cancelled, Y failed of N' tally."""
    from tg_bot.handlers.callbacks import _format_digest_summary

    status = {
        "NVDA": {"ticker": "NVDA", "signal": "BUY", "telegraph_url": None},
        "AAPL": "cancelled",
        "TSLA": None,
    }
    out = _format_digest_summary(["NVDA", "AAPL", "TSLA"], status, "2026-05-06")
    assert "⛔" in out and "AAPL" in out
    assert "❓" in out and "TSLA" in out
    assert "1 cancelled" in out and "1 failed" in out and "of 3" in out


async def test_handle_digest_cancel_signals_event_and_tasks() -> None:
    """Cancel handler must set the threading event AND cancel each task."""
    import threading

    from tg_bot.handlers import callbacks as cbmod

    cancel_event = threading.Event()

    # Build two never-finishing tasks so .cancel() actually has work to do.
    async def _hang():
        await asyncio.Event().wait()

    t1 = asyncio.create_task(_hang())
    t2 = asyncio.create_task(_hang())
    # Yield once so they're actually running.
    await asyncio.sleep(0)

    chat_data = {
        "digest_cancels": {
            42: {"cancel_event": cancel_event, "tasks": [t1, t2]},
        }
    }

    class _Ctx:
        def __init__(self, cd):
            self.chat_data = cd

    class _Q:
        pass

    await cbmod._handle_digest_cancel(_Ctx(chat_data), _Q(), "42")

    assert cancel_event.is_set()
    assert t1.cancelled() or t1.cancelling()
    assert t2.cancelled() or t2.cancelling()
    # Drain so pytest doesn't complain.
    for t in (t1, t2):
        try:
            await t
        except asyncio.CancelledError:
            pass


async def test_handle_digest_cancel_unknown_message_id() -> None:
    """Stale / unknown message_id is a no-op."""
    from tg_bot.handlers import callbacks as cbmod

    class _Ctx:
        chat_data = {"digest_cancels": {}}

    class _Q:
        pass

    # No exception should be raised; nothing to assert beyond that.
    await cbmod._handle_digest_cancel(_Ctx(), _Q(), "999")
    await cbmod._handle_digest_cancel(_Ctx(), _Q(), "not-an-int")


async def test_fanout_cancel_mid_run() -> None:
    """End-to-end cancel: tap the button mid fan-out → in-flight tickers
    get marked cancelled, the final summary tally reflects it."""
    import threading

    from tg_bot.handlers import callbacks as cbmod

    started = threading.Event()
    release = threading.Event()

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA", "AAPL", "TSLA"]

    async def _slow_analyze(_uid, _ticker, reporter=None):
        # Signal that at least one analysis has reached the running phase.
        started.set()
        # Mimic running until the cancel signal lands.
        while not (
            reporter and reporter.cancel_event and reporter.cancel_event.is_set()
        ):
            await asyncio.sleep(0.05)
            if release.is_set():
                break
        # Raise the canonical "user-cancelled" exception so _wrapped's
        # except branch fires.
        from tg_bot.progress import CancelledByUserError

        raise CancelledByUserError("cancelled in test")

    orig_w = cbmod.watchlist_storage
    orig_a = cbmod._analyze_one_for_digest
    cbmod.watchlist_storage = _W()
    cbmod._analyze_one_for_digest = _slow_analyze
    try:
        app = _FakeFanOutApp()
        run_task = asyncio.create_task(cbmod.run_user_digest(app, 42, 999))

        # Wait for the fan-out to be in flight.
        for _ in range(50):
            if started.is_set():
                break
            await asyncio.sleep(0.02)
        assert started.is_set(), "analysis never started"

        # Find the registered cancel entry and tap it.
        digest_cancels = app.chat_data[999]["digest_cancels"]
        msg_id = next(iter(digest_cancels.keys()))

        class _Ctx:
            def __init__(self, cd):
                self.chat_data = cd

        await cbmod._handle_digest_cancel(
            _Ctx(app.chat_data[999]), object(), str(msg_id)
        )

        # Let the gather observe the cancels and unwind.
        release.set()
        await run_task

        # Final summary edit (last in edits) shows "3 cancelled".
        body = app.bot.edits[-1]["text"]
        assert "⛔" in body
        assert "3 cancelled" in body and "of 3" in body
    finally:
        cbmod.watchlist_storage = orig_w
        cbmod._analyze_one_for_digest = orig_a


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
    ("register_digest_job adds a job", test_register_adds_job),
    ("register replaces (no duplicate)", test_register_replaces_existing),
    ("cancel_digest_job removes jobs", test_cancel_removes_jobs),
    ("register no-op on disabled / partial", test_register_noop_on_incomplete),
    ("fanout silent on empty watchlist", test_fanout_empty_watchlist_silent),
    ("fanout Forbidden auto-disables digest", test_fanout_forbidden_disables),
    ("fanout summary message renders", test_fanout_summary_renders),
    (
        "iter_enabled skips malformed entries",
        test_iter_enabled_skips_malformed_entries,
    ),
    (
        "fanout cancel pending via Task.cancel",
        test_fanout_cancel_pending_via_task_cancel,
    ),
    (
        "fanout Forbidden mid-progress-edit",
        test_fanout_forbidden_during_progress_edit,
    ),
    (
        "deferred render captures latest state",
        test_render_deferred_captures_latest_state,
    ),
    (
        "progress completed row matches summary",
        test_progress_completed_row_matches_summary,
    ),
    ("progress render handles cancelled state", test_progress_renders_cancelled),
    ("summary tally includes cancelled count", test_summary_tally_includes_cancelled),
    (
        "digest_cancel sets event + cancels tasks",
        test_handle_digest_cancel_signals_event_and_tasks,
    ),
    (
        "digest_cancel on unknown message_id is no-op",
        test_handle_digest_cancel_unknown_message_id,
    ),
    ("fanout cancel mid-run renders cancelled", test_fanout_cancel_mid_run),
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
