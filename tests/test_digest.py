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

Run with: pytest tests/test_digest.py
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
        "tickers": [],
        "email": None,
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
        # Captures `query.answer(toast, show_alert=...)` calls so tests can
        # assert that an invalid-input branch surfaces a toast instead of
        # silently no-op'ing.
        self.answers: list[tuple[str, bool]] = []

    async def edit_message_text(
        self, text: str, parse_mode: str | None = None, reply_markup=None
    ) -> None:
        self.last_text = text
        self.last_kb = reply_markup

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **_: object) -> None:
        self.sent.append((chat_id, text))


class _FakeContext:
    """Minimal stand-in for ContextTypes.DEFAULT_TYPE. `_handle_digest`
    reads `context.bot`, `context.application.job_queue` (via
    register_digest_job / cancel_digest_job), and `context.chat_data`
    (digest ticker pagination + run-once guard)."""

    def __init__(self) -> None:
        self.bot = _FakeBot()
        # _FakeFanOutApp gives us a job_queue with the right interface.
        self.application = _FakeFanOutApp()
        self.chat_data: dict = {}


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

        # Non-int hour — should not write, AND should surface a toast so
        # the user knows the tap registered.
        await _handle_digest(q, ctx, user_id=42, data="digest:hour:abc")
        assert s.get_digest("42") is None
        assert q.answers, "expected a query.answer() toast on bad hour"
        prior = len(q.answers)
        # Garbage tz — same: no write, and a toast so the picker isn't
        # frozen-looking.
        await _handle_digest(q, ctx, user_id=42, data="digest:tz:Mars/Phobos")
        assert s.get_digest("42") is None
        assert len(q.answers) > prior, "expected a query.answer() toast on bad tz"
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


def _disable_llm_precheck_globally() -> None:
    """Disable check_llm_configured for the rest of the smoke run.

    The precheck has dedicated coverage in `test_llm_precheck_*`; the
    fan-out / cancel / progress tests below should not have to also arm
    a provider + matching env var. Patches the import in both modules
    that bind to it — `callbacks` (for `_handle_digest`'s `digest:run`
    branch) and `analysis_runner` (for `run_user_digest`'s entry guard).
    """
    # Patch both modules that bind `check_llm_configured`:
    # - `callbacks` (used by `_handle_digest`'s `digest:run` branch)
    # - `analysis_runner` (used by `run_user_digest`'s entry guard)
    from tg_bot.handlers import analysis_runner, callbacks

    def noop(*_a, **_kw):
        return None

    callbacks.check_llm_configured = noop
    analysis_runner.check_llm_configured = noop


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


class _AllEnrolledUserConfig:
    """Test fake for `runner.user_config_storage` — treats the entire
    watchlist as digest-enrolled. Many fan-out tests want this
    "everything fires" semantic without setting up a full digest block;
    previously this was implicit via the legacy `tickers absent →
    watchlist` fallback in the inline run_user_digest filter. Now that
    the intersection lives in `UserConfigStorage.get_enrolled_tickers`
    and explicitly requires a digest block, this fake lets the same
    tests keep their concise setup.

    Use in tests where the digest-filter semantic isn't the thing under
    test (most fan-out / cancel / progress tests). Tests that DO
    exercise the filter set up a real storage via `fresh_storage()`.
    """

    def get_enrolled_tickers(self, _uid, watchlist):
        return list(watchlist)

    def get_digest(self, _uid):
        # Returned dict is unused by the call paths these tests exercise.
        return {"enabled": True, "tickers": None}

    async def disable_digest(self, _uid):
        return True


class _ChatDataDict(dict):
    """Minimal stand-in for PTB's chat_data — auto-creates {} on access."""

    def __getitem__(self, key):  # type: ignore[override]
        if key not in self:
            self[key] = {}
        return super().__getitem__(key)


async def test_register_adds_job() -> None:
    from tg_bot.handlers.analysis_runner import register_digest_job

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
    from tg_bot.handlers.analysis_runner import register_digest_job

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
    from tg_bot.handlers.analysis_runner import cancel_digest_job, register_digest_job

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
    from tg_bot.handlers.analysis_runner import register_digest_job

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
    from tg_bot.handlers import analysis_runner as runner

    # Patch in an empty watchlist storage.
    class _W:
        def get_watchlist(self, _uid):
            return []

    original = runner.watchlist_storage
    runner.watchlist_storage = _W()
    try:
        app = _FakeFanOutApp()
        await runner.run_user_digest(app, 42, 999)
        assert app.bot.sent == [] and app.bot.edits == []
    finally:
        runner.watchlist_storage = original


async def test_fanout_all_markets_closed_forbidden_disables() -> None:
    """If the user blocked the bot WHILE the all-closed heads-up is being
    sent, Telegram returns Forbidden. The all-closed branch must mirror
    the regular header path's behavior: auto-disable the digest + cancel
    the JobQueue job so we stop firing daily into a chat we can't deliver
    to. Pins this NEW dataflow path (PR #71 added it).

    Test parallels `test_fanout_forbidden_disables` (which covers the
    normal-header Forbidden path) — same assertions, different trigger
    point in `run_user_digest`."""
    from tg_bot.handlers import analysis_runner as runner

    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA"]

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_cal = runner.is_market_open_for
    runner.watchlist_storage = _W()
    runner.user_config_storage = s
    # Force the calendar gate into all-closed mode so the heads-up branch
    # fires; FakeFanOutApp(forbidden=True) makes send_message raise.
    runner.is_market_open_for = lambda *_args, **_kw: False
    try:
        app = _FakeFanOutApp(forbidden=True)
        # Pre-register a job to verify it gets cancelled.
        runner.register_digest_job(app, 42, s.get_digest("42"))
        assert len(app.job_queue.get_jobs_by_name("digest:42")) == 1

        await runner.run_user_digest(app, 42, 999)

        d = s.get_digest("42")
        assert d["enabled"] is False, (
            "all-closed Forbidden path must auto-disable the digest"
        )
        assert app.job_queue.get_jobs_by_name("digest:42") == [], (
            "all-closed Forbidden path must cancel the JobQueue job"
        )
    finally:
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner.is_market_open_for = orig_cal


async def test_fanout_all_markets_closed_sends_oneliner_and_skips() -> None:
    """When every enrolled ticker's market is closed today (e.g. user has
    only US tickers and it's Christmas, or only Tokyo tickers on a TSE
    holiday), the fan-out short-circuits with a one-line heads-up — NO
    header with a Cancel button, NO progress edits, NO LLM calls. The
    message lists the skipped tickers so the user can see why.

    Pins the calendar-gate's all-closed branch in `run_user_digest`."""
    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA", "AAPL"]

    # The session autouse fixture forces is_market_open_for → True;
    # re-patch for this specific test to simulate a closed-markets day.
    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_cal = runner.is_market_open_for
    runner.watchlist_storage = _W()
    runner.user_config_storage = _AllEnrolledUserConfig()
    runner.is_market_open_for = lambda *_args, **_kw: False
    try:
        app = _FakeFanOutApp()
        await runner.run_user_digest(app, 42, 999)
        # Exactly one message (the heads-up), no progress edits.
        assert len(app.bot.sent) == 1, app.bot.sent
        assert app.bot.edits == [], app.bot.edits
        msg = app.bot.sent[0]["text"]
        assert "Markets closed today" in msg, msg
        # Skipped tickers listed verbatim so user knows what was dropped.
        assert "NVDA" in msg and "AAPL" in msg, msg
    finally:
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner.is_market_open_for = orig_cal


async def test_fanout_partial_market_closure_drops_only_closed() -> None:
    """When some tickers' markets are open and others are closed (mixed
    US+Asia watchlist on a US holiday, say), the fan-out runs only the
    open ones AND renders a "skipped (markets closed)" footnote so the
    user can see which tickers didn't fire today and why."""
    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA", "0700.HK", "601318.SS"]

    # NVDA open, the two CN/HK tickers closed.
    def _selective_open(ticker, _date):
        return ticker == "NVDA"

    async def _fake_analyze(_uid, ticker, reporter=None):
        return {
            "ticker": ticker,
            "signal": "BUY",
            "telegraph_url": f"https://telegra.ph/{ticker}",
        }

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    orig_cal = runner.is_market_open_for
    runner.watchlist_storage = _W()
    runner.user_config_storage = _AllEnrolledUserConfig()
    runner._analyze_one_for_digest = _fake_analyze
    runner.is_market_open_for = _selective_open
    try:
        app = _FakeFanOutApp()
        await runner.run_user_digest(app, 42, 999)
        # Header lists only the OPEN ticker as pending; closed ones don't
        # appear in any "pending/analyzing" row — they're in the footnote.
        header = app.bot.sent[0]["text"]
        assert "NVDA" in header, header
        # "0/1" — one ticker enrolled in the actual fan-out (the open one).
        assert "0/1" in header, header
        # Footnote lists the skipped tickers verbatim so the user can see
        # exactly which ones were dropped.
        assert "0700.HK" in header and "601318.SS" in header, header
        assert "Skipped (markets closed)" in header, header
        # Summary (last edit) shows NVDA's signal + still has the footnote.
        summary = app.bot.edits[-1]["text"]
        assert "🟢" in summary and "NVDA" in summary and "BUY" in summary
        assert "0700.HK" in summary and "601318.SS" in summary
    finally:
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a
        runner.is_market_open_for = orig_cal


class _UserConfigWithEmail:
    """Like `_AllEnrolledUserConfig` but `get_digest` returns a configurable
    email field so tests can drive the email-mirror path in `run_user_digest`.

    The mirror branch in run_user_digest reads `digest.get("email")` once
    after the summary edit. Returning the email here is enough to flip
    that branch; we don't need full digest schema fidelity."""

    def __init__(self, email: str | None) -> None:
        self._email = email

    def get_enrolled_tickers(self, _uid, watchlist):
        return list(watchlist)

    def get_digest(self, _uid):
        return {"enabled": True, "tickers": None, "email": self._email}

    async def disable_digest(self, _uid):
        return True


async def test_fanout_calls_email_mirror_when_opted_in_and_configured() -> None:
    """User has email set + RESEND_API_KEY/RESEND_FROM present → after the
    final summary edit, `send_digest_email` IS called. Pins the happy
    path of the email-mirror wiring in `run_user_digest`."""
    import os
    from unittest.mock import patch

    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA"]

    async def _fake_analyze(_uid, ticker, reporter=None):
        return {
            "ticker": ticker,
            "signal": "BUY",
            "telegraph_url": f"https://telegra.ph/{ticker}",
        }

    os.environ["RESEND_API_KEY"] = "re_test"
    os.environ["RESEND_FROM"] = "bot@example.com"
    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    runner.user_config_storage = _UserConfigWithEmail("user@example.com")
    runner._analyze_one_for_digest = _fake_analyze
    try:
        sent_to: list[str] = []

        async def fake_send(**kwargs):
            sent_to.append(kwargs["to_addr"])
            return runner.EmailSendResult(
                ok=True,
                recipient=kwargs["to_addr"],
                message_id="re_fake_id",
            )

        with patch.object(runner, "send_digest_email", side_effect=fake_send):
            app = _FakeFanOutApp()
            await runner.run_user_digest(app, 42, 999)

        assert sent_to == ["user@example.com"], (
            f"expected one email to user@example.com, got {sent_to}"
        )
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a


async def test_fanout_skips_email_mirror_when_no_opt_in() -> None:
    """User has NO email set → `send_digest_email` is NOT called. Pins
    the default Telegram-only path so existing users (who haven't run
    `/email set`) never get unexpected emails."""
    from unittest.mock import patch

    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA"]

    async def _fake_analyze(_uid, ticker, reporter=None):
        return {
            "ticker": ticker,
            "signal": "BUY",
            "telegraph_url": f"https://telegra.ph/{ticker}",
        }

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    # Email = None ⇒ mirror path must not fire.
    runner.user_config_storage = _UserConfigWithEmail(None)
    runner._analyze_one_for_digest = _fake_analyze
    try:
        with patch.object(runner, "send_digest_email") as mock_send:
            app = _FakeFanOutApp()
            await runner.run_user_digest(app, 42, 999)
        mock_send.assert_not_called()
    finally:
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a


async def test_fanout_email_failure_does_not_break_telegram_path() -> None:
    """The mirror failing must NEVER affect Telegram delivery. If
    `send_digest_email` raises or returns False, the digest summary
    must still have been edited successfully to Telegram. This pins
    the "email is a mirror, never a replacement" contract."""
    import os
    from unittest.mock import patch

    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA"]

    async def _fake_analyze(_uid, ticker, reporter=None):
        return {
            "ticker": ticker,
            "signal": "BUY",
            "telegraph_url": f"https://telegra.ph/{ticker}",
        }

    os.environ["RESEND_API_KEY"] = "re_test"
    os.environ["RESEND_FROM"] = "bot@example.com"
    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    runner.user_config_storage = _UserConfigWithEmail("user@example.com")
    runner._analyze_one_for_digest = _fake_analyze
    try:

        async def boom(**_kwargs):
            raise RuntimeError("Resend API down")

        with patch.object(runner, "send_digest_email", side_effect=boom):
            app = _FakeFanOutApp()
            # If the mirror's RuntimeError bubbled, this would raise too.
            await runner.run_user_digest(app, 42, 999)

        # Telegram summary still landed — last edit contains the signal-emoji.
        assert app.bot.edits, "Telegram summary edit didn't fire"
        summary = app.bot.edits[-1]["text"]
        assert "🟢" in summary and "NVDA" in summary and "BUY" in summary
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a


async def test_fanout_forbidden_disables() -> None:
    """Header send fails with Forbidden → digest disabled + job cancelled."""
    from tg_bot.handlers import analysis_runner as runner

    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA"]

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    runner.watchlist_storage = _W()
    runner.user_config_storage = s
    try:
        app = _FakeFanOutApp(forbidden=True)
        # Pre-register a job to verify it gets cancelled.
        runner.register_digest_job(app, 42, s.get_digest("42"))
        assert len(app.job_queue.get_jobs_by_name("digest:42")) == 1

        await runner.run_user_digest(app, 42, 999)

        d = s.get_digest("42")
        assert d["enabled"] is False, "digest should auto-disable on Forbidden"
        assert app.job_queue.get_jobs_by_name("digest:42") == [], (
            "job should be cancelled"
        )
    finally:
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc


async def test_fanout_summary_renders() -> None:
    """Happy path: header sent in pending state, final edit shows the
    signal-emoji summary with Telegraph links + failure tally."""
    from tg_bot.handlers import analysis_runner as runner

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

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    runner.user_config_storage = _AllEnrolledUserConfig()
    runner._analyze_one_for_digest = _fake_analyze
    try:
        app = _FakeFanOutApp()
        await runner.run_user_digest(app, 42, 999)

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
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a


async def test_render_deferred_captures_latest_state() -> None:
    """When two tickers fire status changes within the throttle window,
    the second one mustn't get silently dropped — the deferred render
    must paint the latest state once the throttle expires. Patches
    _DIGEST_PROGRESS_INTERVAL down so the test runs in <1s instead of
    >2s."""
    from tg_bot.handlers import analysis_runner as runner

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

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    orig_iv = runner._DIGEST_PROGRESS_INTERVAL
    runner.watchlist_storage = _W()
    runner.user_config_storage = _AllEnrolledUserConfig()
    runner._analyze_one_for_digest = _fake
    runner._DIGEST_PROGRESS_INTERVAL = 0.3
    try:
        app = _FakeFanOutApp()
        await runner.run_user_digest(app, 42, 999)
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
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a
        runner._DIGEST_PROGRESS_INTERVAL = orig_iv


async def test_render_trampolines_after_cache_hit_neighbour() -> None:
    """Regression: when one ticker is a cache hit (instant `status`
    flip → `_render` enters edit_message_text immediately) and another
    is mid-startup (calls `report_starting` while T1's edit is on the
    wire), the bailed render must trampoline once T1's edit returns.
    Without the rerender flag, T2 sits on ⏳ until its first real LLM
    callback fires inside `to_thread` (5–30s in production), even
    though the slot is already taken.

    The default `_FakeFanOutBot.edit_message_text` is purely
    synchronous — no `await` to yield on, so coroutines can't actually
    interleave during the "edit." Without a yield point inside the
    mocked HTTP call, T2's `report_starting` won't arrive while T1's
    render holds `edit_in_flight=True`, and the trampoline never gets
    exercised (both old + new code paint identical edits). Subclass
    with `await asyncio.sleep(0)` so the bailed-during-edit path is
    actually triggered — without this, the test passes against the
    buggy old code too.
    """
    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _u):
            return ["CACHED", "FRESH"]

    class _YieldingBot(_FakeFanOutBot):
        async def edit_message_text(self, chat_id, message_id, text, **kw):
            await asyncio.sleep(0)  # force a yield mid-edit
            await super().edit_message_text(chat_id, message_id, text, **kw)

    class _YieldingApp(_FakeFanOutApp):
        def __init__(self) -> None:
            super().__init__()
            self.bot = _YieldingBot()

    async def _fake(_uid, ticker, reporter=None):
        if ticker == "CACHED":
            # Cache-hit path: no report_starting, instant return. Mirrors
            # the real `_analyze_one_for_digest` short-circuit.
            return {"ticker": ticker, "signal": "BUY", "telegraph_url": None}
        # Fresh path: sem acquire happens inside (skipped here), then
        # report_starting flips ⏳ → 📊 Starting…, then a long LLM sleep.
        if reporter is not None:
            await reporter.report_starting()
        await asyncio.sleep(0.6)
        return {"ticker": ticker, "signal": "BUY", "telegraph_url": None}

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    orig_iv = runner._DIGEST_PROGRESS_INTERVAL
    runner.watchlist_storage = _W()
    runner.user_config_storage = _AllEnrolledUserConfig()
    runner._analyze_one_for_digest = _fake
    runner._DIGEST_PROGRESS_INTERVAL = 0.1
    try:
        app = _YieldingApp()
        await runner.run_user_digest(app, 42, 999)
        # FRESH must appear as 📊 in at least one progress edit. Before
        # the trampoline fix, T1's instant cache-hit edit consumed the
        # only render slot, the FRESH "Starting…" render bailed, and no
        # further edit fired until FRESH completed (skipping the
        # 📊 row entirely).
        progress_edits = [e["text"] for e in app.bot.edits]
        fresh_visible_as_analyzing = [
            t for t in progress_edits if "FRESH" in t and "📊" in t
        ]
        assert fresh_visible_as_analyzing, (
            "FRESH never appeared in 📊 state during fan-out — trampoline "
            f"didn't fire. Edits seen: {progress_edits!r}"
        )
    finally:
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a
        runner._DIGEST_PROGRESS_INTERVAL = orig_iv


async def test_progress_completed_row_matches_summary() -> None:
    """A completed ticker should render identically in the progress
    view and the final summary — same signal emoji + Telegraph link, no
    flash from generic ✅ to coloured 🟢 at the final-edit boundary."""
    from tg_bot.handlers.analysis_runner import (
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

    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _u):
            return ["A", "B", "C"]

    started = threading.Event()
    release = threading.Event()
    a_b_c_calls: list[str] = []

    # Force a fresh sem with cap=1 so only one ticker can be analyzing.
    orig_sem = runner._run_semaphore
    runner._run_semaphore = asyncio.Semaphore(1)

    async def _slow(_uid, ticker, reporter=None):
        # Mimic _analyze_one_for_digest's sem.acquire so the test
        # actually exercises the pending → cancelled path.
        sem = runner._get_run_semaphore()
        await sem.acquire()
        try:
            a_b_c_calls.append(ticker)
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
            from tg_bot.pipeline.progress import CancelledByUserError

            raise CancelledByUserError("simulated mid-flight cancel")
        finally:
            sem.release()

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    runner.user_config_storage = _AllEnrolledUserConfig()
    runner._analyze_one_for_digest = _slow
    try:
        app = _FakeFanOutApp()
        run_task = asyncio.create_task(runner.run_user_digest(app, 42, 999))

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

        await runner._handle_digest_cancel(
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
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a
        runner._run_semaphore = orig_sem


async def test_fanout_pending_task_cancel_sets_status_cancelled() -> None:
    """Companion to `test_fanout_cancel_pending_via_task_cancel`: that
    test verifies the queued tickers don't take a slot; this one verifies
    the `_wrapped` `asyncio.CancelledError` branch actually flips
    `status[ticker] = "cancelled"` (not None / leftover "pending"). The
    distinction matters — a None would render as ❓ error in the
    summary, not ⛔ cancelled. Pin the per-row outcome so a refactor of
    `_wrapped`'s exception handling doesn't silently swap the badge."""
    import threading

    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _u):
            return ["A", "B"]

    started = threading.Event()
    release = threading.Event()
    called: list[str] = []

    orig_sem = runner._run_semaphore
    runner._run_semaphore = asyncio.Semaphore(1)  # cap=1 so B queues

    async def _slow(_uid, ticker, reporter=None):
        sem = runner._get_run_semaphore()
        await sem.acquire()
        try:
            called.append(ticker)
            started.set()
            while not release.is_set():
                await asyncio.sleep(0.02)
            from tg_bot.pipeline.progress import CancelledByUserError

            raise CancelledByUserError("simulated cancel mid-flight")
        finally:
            sem.release()

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    runner.user_config_storage = _AllEnrolledUserConfig()
    runner._analyze_one_for_digest = _slow
    try:
        app = _FakeFanOutApp()
        run_task = asyncio.create_task(runner.run_user_digest(app, 42, 999))

        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.02)
        assert started.is_set(), "A never reached the analyzer"

        digest_cancels = app.chat_data[999]["digest_cancels"]
        msg_id = next(iter(digest_cancels.keys()))

        class _Ctx:
            def __init__(self, cd):
                self.chat_data = cd

        await runner._handle_digest_cancel(
            _Ctx(app.chat_data[999]), object(), str(msg_id)
        )
        release.set()
        await run_task

        # Only A ran (B was cancelled while queued on the semaphore).
        assert called == ["A"], f"only A should have run; saw {called}"

        # The summary must render both as cancelled (⛔) — proves
        # `_wrapped`'s `asyncio.CancelledError` branch fired for B and
        # set status["B"] = "cancelled", NOT left as None/pending (which
        # would render as ❓ error).
        summary = app.bot.edits[-1]["text"]
        # ⛔ rows for BOTH tickers (proves the cancelled status flip
        # happened for the pending one, not just the in-flight one).
        assert "⛔ <b>A</b> — cancelled" in summary, summary
        assert "⛔ <b>B</b> — cancelled" in summary, summary
        # And no ❓ rows (would mean the status flip went to None).
        assert "❓" not in summary, f"unexpected error row in summary: {summary}"
        assert "2 cancelled" in summary and "of 2" in summary
    finally:
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a
        runner._run_semaphore = orig_sem


async def test_fanout_forbidden_during_progress_edit() -> None:
    """Initial header send succeeds, then the FIRST progress edit fails
    with Forbidden. Digest must auto-disable, JobQueue job must cancel,
    and the in-flight fan-out must abort (cancel_event set + tasks
    cancelled) instead of grinding through every ticker silently."""
    from tg_bot.handlers import analysis_runner as runner

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
    await s.set_digest_tickers("42", ["A", "B", "C"])

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
                from tg_bot.pipeline.progress import CancelledByUserError

                raise CancelledByUserError("forbidden auto-cancel")
        return {"ticker": _t, "signal": "BUY", "telegraph_url": None}

    orig_w = runner.watchlist_storage
    orig_a = runner._analyze_one_for_digest
    orig_uc = runner.user_config_storage
    orig_iv = runner._DIGEST_PROGRESS_INTERVAL
    runner.watchlist_storage = _W()
    runner._analyze_one_for_digest = _slow
    runner.user_config_storage = s
    runner._DIGEST_PROGRESS_INTERVAL = 0.1
    try:
        app = _FakeFanOutApp()
        app.bot = _ForbiddenOnFirstEdit()
        # Pre-register a JobQueue entry so cancel_digest_job has work to do.
        runner.register_digest_job(app, 42, s.get_digest("42"))
        assert len(app.job_queue.get_jobs_by_name("digest:42")) == 1

        await runner.run_user_digest(app, 42, 999)

        # Auto-disabled.
        assert s.get_digest("42")["enabled"] is False
        # JobQueue job cancelled.
        assert app.job_queue.get_jobs_by_name("digest:42") == []
    finally:
        runner.watchlist_storage = orig_w
        runner._analyze_one_for_digest = orig_a
        runner.user_config_storage = orig_uc
        runner._DIGEST_PROGRESS_INTERVAL = orig_iv


async def test_progress_renders_cancelled() -> None:
    """`_format_digest_progress` renders the ⛔ cancelled state."""
    from tg_bot.handlers.analysis_runner import _format_digest_progress

    status = {"NVDA": "cancelled", "AAPL": "pending"}
    out = _format_digest_progress(["NVDA", "AAPL"], status, "2026-05-06")
    assert "⛔" in out and "NVDA" in out and "cancelled" in out
    assert "⏳" in out and "AAPL" in out


async def test_summary_tally_includes_cancelled() -> None:
    """`_format_digest_summary` shows a 'X cancelled, Y failed of N' tally."""
    from tg_bot.handlers.analysis_runner import _format_digest_summary

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

    from tg_bot.handlers import analysis_runner as runner

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

    await runner._handle_digest_cancel(_Ctx(chat_data), _Q(), "42")

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
    from tg_bot.handlers import analysis_runner as runner

    class _Ctx:
        chat_data = {"digest_cancels": {}}

    class _Q:
        pass

    # No exception should be raised; nothing to assert beyond that.
    await runner._handle_digest_cancel(_Ctx(), _Q(), "999")
    await runner._handle_digest_cancel(_Ctx(), _Q(), "not-an-int")


async def test_fanout_cancel_mid_run() -> None:
    """End-to-end cancel: tap the button mid fan-out → in-flight tickers
    get marked cancelled, the final summary tally reflects it."""
    import threading

    from tg_bot.handlers import analysis_runner as runner

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
        from tg_bot.pipeline.progress import CancelledByUserError

        raise CancelledByUserError("cancelled in test")

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    runner.user_config_storage = _AllEnrolledUserConfig()
    runner._analyze_one_for_digest = _slow_analyze
    try:
        app = _FakeFanOutApp()
        run_task = asyncio.create_task(runner.run_user_digest(app, 42, 999))

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

        await runner._handle_digest_cancel(
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
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a


# --- Ticker filter feature ------------------------------------------------


async def test_tickers_default_empty() -> None:
    """First call to set_digest_hour creates a digest block with tickers=[]
    so new users must explicitly opt tickers in via the filter screen."""
    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)
    d = s.get_digest("42")
    assert d["tickers"] == [], d


async def test_set_digest_tickers_canonical() -> None:
    """Stored list is de-duped, uppercased, and sorted — keeps on-disk
    order predictable across writes."""
    s, _ = fresh_storage()
    await s.set_digest_tickers("42", ["nvda", "AAPL", "nvda", "  TSLA  ", ""])
    d = s.get_digest("42")
    assert d["tickers"] == ["AAPL", "NVDA", "TSLA"], d


async def test_tickers_independent() -> None:
    """Setting tickers must not touch enabled/hour/tz, and disabling the
    digest must not wipe tickers."""
    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)
    await s.set_digest_tickers("42", ["NVDA"])
    d = s.get_digest("42")
    assert d["enabled"] and d["hour_local"] == 10 and d["tz"] == "America/Los_Angeles"
    assert d["tickers"] == ["NVDA"]
    await s.disable_digest("42")
    d2 = s.get_digest("42")
    assert d2["enabled"] is False
    assert d2["tickers"] == ["NVDA"], "disable must not wipe filter"


async def test_picker_tickers_screen() -> None:
    """Tickers screen renders the toggle grid with ✅ on selected entries
    plus the bulk + Back rows."""
    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)
    await s.set_digest_tickers("42", ["NVDA"])
    digest = s.get_digest("42")
    text, kb = build_digest_response(
        digest, mode="tickers", watchlist=["AAPL", "NVDA", "TSLA"], page=0
    )
    flat = [b.text for row in kb.inline_keyboard for b in row]
    assert "✅ NVDA" in flat, flat
    assert "AAPL" in flat and "TSLA" in flat
    assert "✓ Select all" in flat and "✗ Clear" in flat
    assert "← Back" in flat


async def test_picker_tickers_empty_watchlist() -> None:
    """Empty-watchlist case shows a hint and only a Back button — no grid."""
    text, kb = build_digest_response(
        {"tickers": []}, mode="tickers", watchlist=[], page=0
    )
    flat = [b.text for row in kb.inline_keyboard for b in row]
    assert flat == ["← Back"], flat
    assert "empty" in text.lower() or "/add" in text


async def test_picker_hour_shows_count() -> None:
    """Hour screen carries a 📋 Tickers (N/M) button when a watchlist is
    passed in, and the status line shows the same count."""
    digest = {
        "enabled": True,
        "hour_local": 10,
        "tz": "America/Los_Angeles",
        "chat_id": 999,
        "tickers": ["NVDA"],
    }
    text, kb = build_digest_response(
        digest, mode="hours", watchlist=["AAPL", "NVDA", "TSLA"]
    )
    flat = [b.text for row in kb.inline_keyboard for b in row]
    assert any("📋 Tickers (1/3)" in t for t in flat), flat
    assert "1/3 tickers" in text


async def test_callback_tt_toggle() -> None:
    """digest:tt:NVDA toggles inclusion and saves to storage on every tap."""
    s, q, ctx, restore = await _make_dispatch_env()
    try:
        from tg_bot.handlers import callbacks as cbmod
        from tg_bot.handlers.callbacks import _handle_digest

        # Pre-condition: tz must be set before tickers are editable (the
        # picker UI enforces this; the M1 callback gate enforces it on the
        # backend in case a stale button slips past).
        await s.set_digest_tz("42", "America/Los_Angeles")

        class _W:
            def get_watchlist(self, _u):
                return ["AAPL", "NVDA"]

        orig_w = cbmod.watchlist_storage
        cbmod.watchlist_storage = _W()
        try:
            # First tap — adds.
            await _handle_digest(q, ctx, user_id=42, data="digest:tt:NVDA")
            assert s.get_digest("42")["tickers"] == ["NVDA"]
            # Second tap — removes (toggle).
            await _handle_digest(q, ctx, user_id=42, data="digest:tt:NVDA")
            assert s.get_digest("42")["tickers"] == []
        finally:
            cbmod.watchlist_storage = orig_w
    finally:
        restore()


async def test_callback_tt_rejects_stale() -> None:
    """A stale callback button referencing a ticker no longer in the
    watchlist must surface a toast and not modify storage."""
    s, q, ctx, restore = await _make_dispatch_env()
    try:
        from tg_bot.handlers import callbacks as cbmod
        from tg_bot.handlers.callbacks import _handle_digest

        await s.set_digest_tz("42", "America/Los_Angeles")

        class _W:
            def get_watchlist(self, _u):
                return ["AAPL"]

        orig_w = cbmod.watchlist_storage
        cbmod.watchlist_storage = _W()
        try:
            await _handle_digest(q, ctx, user_id=42, data="digest:tt:NVDA")
            assert s.get_digest("42").get("tickers") == []
            assert q.answers, "expected a toast on stale ticker tap"
        finally:
            cbmod.watchlist_storage = orig_w
    finally:
        restore()


async def test_callback_tt_bulk() -> None:
    """ttall picks up every watchlist entry; ttclear empties."""
    s, q, ctx, restore = await _make_dispatch_env()
    try:
        from tg_bot.handlers import callbacks as cbmod
        from tg_bot.handlers.callbacks import _handle_digest

        await s.set_digest_tz("42", "America/Los_Angeles")

        class _W:
            def get_watchlist(self, _u):
                return ["AAPL", "NVDA", "TSLA"]

        orig_w = cbmod.watchlist_storage
        cbmod.watchlist_storage = _W()
        try:
            await _handle_digest(q, ctx, user_id=42, data="digest:ttall")
            assert s.get_digest("42")["tickers"] == ["AAPL", "NVDA", "TSLA"]
            await _handle_digest(q, ctx, user_id=42, data="digest:ttclear")
            assert s.get_digest("42")["tickers"] == []
        finally:
            cbmod.watchlist_storage = orig_w
    finally:
        restore()


async def test_callback_ttpage() -> None:
    """ttpage:next advances; ttpage:prev clamps at 0."""
    s, q, ctx, restore = await _make_dispatch_env()
    try:
        from tg_bot.handlers import callbacks as cbmod
        from tg_bot.handlers.callbacks import _handle_digest

        await s.set_digest_tz("42", "America/Los_Angeles")

        class _W:
            def get_watchlist(self, _u):
                return [f"T{i}" for i in range(15)]  # 2 pages at PAGE_SIZE=9

        orig_w = cbmod.watchlist_storage
        cbmod.watchlist_storage = _W()
        try:
            ctx.chat_data["digest_tickers_page"] = 0
            await _handle_digest(q, ctx, user_id=42, data="digest:ttpage:next")
            assert ctx.chat_data["digest_tickers_page"] == 1
            await _handle_digest(q, ctx, user_id=42, data="digest:ttpage:prev")
            assert ctx.chat_data["digest_tickers_page"] == 0
            await _handle_digest(q, ctx, user_id=42, data="digest:ttpage:prev")
            assert ctx.chat_data["digest_tickers_page"] == 0  # clamped
        finally:
            cbmod.watchlist_storage = orig_w
    finally:
        restore()


async def test_fanout_filter_intersects() -> None:
    """Filter is intersected with current watchlist: a ticker user removed
    after picking it for the digest is silently dropped, the rest still run."""
    from tg_bot.handlers import analysis_runner as runner

    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)
    # Filter has NVDA + AAPL + TSLA; current watchlist no longer has TSLA.
    await s.set_digest_tickers("42", ["NVDA", "AAPL", "TSLA"])

    class _W:
        def get_watchlist(self, _u):
            return ["NVDA", "AAPL"]  # TSLA removed since filter was set

    seen: list[str] = []

    async def _fake(_uid, ticker, reporter=None):
        seen.append(ticker)
        return {"ticker": ticker, "signal": "BUY", "telegraph_url": None}

    orig_w = runner.watchlist_storage
    orig_a = runner._analyze_one_for_digest
    orig_uc = runner.user_config_storage
    runner.watchlist_storage = _W()
    runner._analyze_one_for_digest = _fake
    runner.user_config_storage = s
    try:
        app = _FakeFanOutApp()
        await runner.run_user_digest(app, 42, 999)
        assert sorted(seen) == ["AAPL", "NVDA"], seen
    finally:
        runner.watchlist_storage = orig_w
        runner._analyze_one_for_digest = orig_a
        runner.user_config_storage = orig_uc


async def test_callback_tt_requires_tz() -> None:
    """A `tt:`/`ttall`/`ttclear` tap with no digest configured (no tz)
    must decline with a toast, never write a partial digest record."""
    s, q, ctx, restore = await _make_dispatch_env()
    try:
        from tg_bot.handlers import callbacks as cbmod
        from tg_bot.handlers.callbacks import _handle_digest

        class _W:
            def get_watchlist(self, _u):
                return ["NVDA"]

        orig_w = cbmod.watchlist_storage
        cbmod.watchlist_storage = _W()
        try:
            # No tz/hour/digest configured. A stale or hand-crafted callback
            # that lands here must decline.
            await _handle_digest(q, ctx, user_id=42, data="digest:tt:NVDA")
            await _handle_digest(q, ctx, user_id=42, data="digest:ttall")
            await _handle_digest(q, ctx, user_id=42, data="digest:ttclear")
            d = s.get_digest("42")
            assert d is None or not d.get("tickers"), (
                f"no partial write should happen; got {d}"
            )
            assert len(q.answers) >= 3, "each path should toast"
        finally:
            cbmod.watchlist_storage = orig_w
    finally:
        restore()


async def test_set_digest_tickers_rejects_string() -> None:
    """Passing a bare string instead of a list raises TypeError; without the
    guard, set() over the string would shred it into chars."""
    s, _ = fresh_storage()
    try:
        await s.set_digest_tickers("42", "NVDA")  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        raise AssertionError("expected TypeError on string input")


async def test_post_init_backfill_idempotent() -> None:
    """First call backfills; second is a no-op. No re-writes, no clobbering
    a user's explicit empty filter on the second pass."""
    s, _ = fresh_storage()

    # Simulate a legacy save: digest block with no tickers field.
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)
    # Manually strip the tickers field to mimic a pre-feature on-disk record.
    s._data["42"]["digest"].pop("tickers", None)
    await s._save_async()
    assert "tickers" not in s.get_digest("42")

    # Stub a watchlist storage with a known list.
    class _W:
        def get_watchlist(self, _u):
            return ["NVDA", "AAPL"]

    wl = _W()

    # Run the migration loop twice.
    async def _migrate():
        migrated = 0
        for uid in s.iter_users_with_digest():
            d = s.get_digest(uid)
            if d and ("tickers" not in d or d.get("tickers") is None):
                await s.set_digest_tickers(uid, wl.get_watchlist(uid))
                migrated += 1
        return migrated

    n1 = await _migrate()
    assert n1 == 1, f"first pass should backfill 1 user; got {n1}"
    assert s.get_digest("42")["tickers"] == ["AAPL", "NVDA"]
    n2 = await _migrate()
    assert n2 == 0, f"second pass should be no-op; got {n2}"

    # And: a user who had explicitly cleared their filter (tickers=[])
    # must NOT be re-backfilled on a subsequent run.
    await s.set_digest_tz("99", "America/New_York")
    await s.set_digest_hour("99", 9, 100)
    await s.set_digest_tickers("99", [])  # explicit empty
    n3 = await _migrate()
    assert n3 == 0
    assert s.get_digest("99")["tickers"] == []


async def test_iter_users_with_digest_includes_disabled() -> None:
    """Migration walks all digest users, not just enabled ones — a user who
    turned digest off pre-deploy still gets the back-compat backfill."""
    s, _ = fresh_storage()
    # Enabled user.
    await s.set_digest_tz("aaa", "America/New_York")
    await s.set_digest_hour("aaa", 9, 100)
    # Disabled (was enabled, then turned off).
    await s.set_digest_tz("bbb", "America/Los_Angeles")
    await s.set_digest_hour("bbb", 10, 200)
    await s.disable_digest("bbb")
    # Partial — only tz set.
    await s.set_digest_tz("ccc", "Europe/London")

    uids = sorted(s.iter_users_with_digest())
    assert uids == ["aaa", "bbb", "ccc"], uids


async def test_iter_enabled_digests_skips_disabled_users() -> None:
    """Pins the documented asymmetry in `storage/CLAUDE.md`:
    `iter_users_with_digest` returns every user with any digest block
    (enabled or not — used by `_post_init`'s backfill walk), while
    `iter_enabled_digests` skips disabled users (used as the scheduling
    input). Mixing them re-registers JobQueue jobs for disabled users,
    which is the footgun the contract calls out — test the divergence
    directly so a refactor that "unifies" them breaks loudly."""
    s, _ = fresh_storage()
    # Enabled user — must appear in BOTH iterators.
    await s.set_digest_tz("on", "America/New_York")
    await s.set_digest_hour("on", 9, 100)
    # Disabled user (was enabled, then turned off) — must appear in
    # iter_users_with_digest (back-compat backfill needs to see them)
    # but NOT in iter_enabled_digests (scheduling must skip).
    await s.set_digest_tz("off", "Europe/London")
    await s.set_digest_hour("off", 10, 200)
    await s.disable_digest("off")

    enabled = [uid for uid, _ in s.iter_enabled_digests()]
    assert enabled == ["on"], f"only 'on' should schedule; got {enabled}"

    all_with_digest = sorted(s.iter_users_with_digest())
    assert all_with_digest == ["off", "on"], (
        f"both users should appear in iter_users_with_digest; got {all_with_digest}"
    )


async def test_fanout_empty_filter_reminder() -> None:
    """User with active digest but empty `tickers` filter: fan-out sends
    a one-line reminder, no analyses run."""
    from tg_bot.handlers import analysis_runner as runner

    s, _ = fresh_storage()
    await s.set_digest_tz("42", "America/Los_Angeles")
    await s.set_digest_hour("42", 10, 999)
    # Filter explicitly empty — new-user "must opt in" semantic.
    assert s.get_digest("42")["tickers"] == []

    class _W:
        def get_watchlist(self, _u):
            return ["NVDA", "AAPL"]

    ran: list[str] = []

    async def _fake(_uid, ticker, reporter=None):
        ran.append(ticker)
        return {"ticker": ticker, "signal": "BUY", "telegraph_url": None}

    orig_w = runner.watchlist_storage
    orig_a = runner._analyze_one_for_digest
    orig_uc = runner.user_config_storage
    runner.watchlist_storage = _W()
    runner._analyze_one_for_digest = _fake
    runner.user_config_storage = s
    try:
        app = _FakeFanOutApp()
        await runner.run_user_digest(app, 42, 999)
        assert ran == [], "no analyses should run when filter is empty"
        assert app.bot.sent, "reminder message should be sent"
        assert "no tickers" in app.bot.sent[0]["text"].lower()
    finally:
        runner.watchlist_storage = orig_w
        runner._analyze_one_for_digest = orig_a
        runner.user_config_storage = orig_uc


# --- Runner ----------------------------------------------------------------


# ─── Email-status footer ────────────────────────────────────────────────
#
# Pins the second `edit_message_text` that appends "📧 …" to the summary.
# Four branches: opted-in+success, opted-in+failure, opted-in+env-missing,
# not-opted-in (status quo — no footer). The shared scaffolding stubs
# `_analyze_one_for_digest` to a fixed BUY signal and `send_digest_email`
# to a configurable return so the test can drive the email outcome.


async def test_fanout_appends_email_status_line_on_success() -> None:
    """Opt-in + RESEND configured + send returns ok=True → the digest
    summary is followed by a second edit that appends `📧 Sent to <addr>`.
    Pins the happy path of the user-visible email-status footer (the
    receipt confirmation in the Telegram message)."""
    import os
    from unittest.mock import patch

    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA"]

    async def _fake_analyze(_uid, ticker, reporter=None):
        return {
            "ticker": ticker,
            "signal": "BUY",
            "telegraph_url": f"https://telegra.ph/{ticker}",
        }

    os.environ["RESEND_API_KEY"] = "re_test"
    os.environ["RESEND_FROM"] = "bot@example.com"
    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    runner.user_config_storage = _UserConfigWithEmail("user@example.com")
    runner._analyze_one_for_digest = _fake_analyze
    try:

        async def fake_send(**kwargs):
            return runner.EmailSendResult(
                ok=True, recipient=kwargs["to_addr"], message_id="re_xyz"
            )

        with patch.object(runner, "send_digest_email", side_effect=fake_send):
            app = _FakeFanOutApp()
            await runner.run_user_digest(app, 42, 999)

        # Last edit MUST be the second pass with the email-status footer.
        final = app.bot.edits[-1]["text"]
        assert "📧 Sent to user@example.com" in final, (
            f"expected success email-status footer; got: {final!r}"
        )
        # Primary summary content must still be present in the final edit.
        assert "NVDA" in final
        assert "BUY" in final
    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a


async def test_fanout_appends_email_failed_line_on_send_failure() -> None:
    """Opt-in + RESEND configured + send returns ok=False (Resend exception
    or malformed response) → the digest summary is followed by a second
    edit that appends `📧 Email failed — check logs`. Pins the
    user-visible signal that the email mirror didn't deliver."""
    import os
    from unittest.mock import patch

    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA"]

    async def _fake_analyze(_uid, ticker, reporter=None):
        return {
            "ticker": ticker,
            "signal": "BUY",
            "telegraph_url": f"https://telegra.ph/{ticker}",
        }

    os.environ["RESEND_API_KEY"] = "re_test"
    os.environ["RESEND_FROM"] = "bot@example.com"
    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    runner.user_config_storage = _UserConfigWithEmail("user@example.com")
    runner._analyze_one_for_digest = _fake_analyze
    try:

        async def fake_send(**kwargs):
            return runner.EmailSendResult(
                ok=False,
                recipient=kwargs["to_addr"],
                error="RuntimeError",
            )

        with patch.object(runner, "send_digest_email", side_effect=fake_send):
            app = _FakeFanOutApp()
            await runner.run_user_digest(app, 42, 999)

        final = app.bot.edits[-1]["text"]
        assert "📧 Email failed" in final, (
            f"expected failure email-status footer; got: {final!r}"
        )

    finally:
        os.environ.pop("RESEND_API_KEY", None)
        os.environ.pop("RESEND_FROM", None)
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a


async def test_fanout_appends_not_configured_line_when_env_missing() -> None:
    """Opt-in but RESEND env NOT set → `send_digest_email` is never
    invoked (the call-site gate catches it), but the user still gets a
    footer line — `📧 Email opt-in but server not configured` — so they
    know their opt-in choice didn't take effect. Pins the operator-side
    misconfiguration signal that's user-facing (not just in bot logs)."""
    import os
    from unittest.mock import patch

    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA"]

    async def _fake_analyze(_uid, ticker, reporter=None):
        return {
            "ticker": ticker,
            "signal": "BUY",
            "telegraph_url": f"https://telegra.ph/{ticker}",
        }

    saved_key = os.environ.pop("RESEND_API_KEY", None)
    saved_from = os.environ.pop("RESEND_FROM", None)
    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    runner.user_config_storage = _UserConfigWithEmail("user@example.com")
    runner._analyze_one_for_digest = _fake_analyze
    try:
        with patch.object(runner, "send_digest_email") as mock_send:
            app = _FakeFanOutApp()
            await runner.run_user_digest(app, 42, 999)
        mock_send.assert_not_called()

        final = app.bot.edits[-1]["text"]
        assert "📧 Email opt-in but server not configured" in final, (
            f"expected not-configured footer; got: {final!r}"
        )
    finally:
        if saved_key is not None:
            os.environ["RESEND_API_KEY"] = saved_key
        if saved_from is not None:
            os.environ["RESEND_FROM"] = saved_from
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a


async def test_fanout_no_email_status_line_when_not_opted_in() -> None:
    """User has NO email set → no footer line in the final summary edit.
    Pins the existing Telegram-only UX for users who haven't run
    `/email <addr>`; the new footer must never appear unannounced."""
    from unittest.mock import patch

    from tg_bot.handlers import analysis_runner as runner

    class _W:
        def get_watchlist(self, _uid):
            return ["NVDA"]

    async def _fake_analyze(_uid, ticker, reporter=None):
        return {
            "ticker": ticker,
            "signal": "BUY",
            "telegraph_url": f"https://telegra.ph/{ticker}",
        }

    orig_w = runner.watchlist_storage
    orig_uc = runner.user_config_storage
    orig_a = runner._analyze_one_for_digest
    runner.watchlist_storage = _W()
    runner.user_config_storage = _UserConfigWithEmail(None)  # no opt-in
    runner._analyze_one_for_digest = _fake_analyze
    try:
        with patch.object(runner, "send_digest_email") as mock_send:
            app = _FakeFanOutApp()
            await runner.run_user_digest(app, 42, 999)
        mock_send.assert_not_called()

        # NO edit text should carry the email-status emoji 📧.
        for edit in app.bot.edits:
            assert "📧" not in edit["text"], (
                f"unexpected email footer in non-opt-in path: {edit['text']!r}"
            )
    finally:
        runner.watchlist_storage = orig_w
        runner.user_config_storage = orig_uc
        runner._analyze_one_for_digest = orig_a
