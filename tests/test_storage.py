"""Smoke tests for `JsonStorage` base-class behaviors.

The `WatchlistStorage` and `UserConfigStorage` subclasses inherit the
atomic + fsync write path and the corrupt-JSON recovery path from
`JsonStorage._load` / `_save` in `_base.py`. Those subclass-specific
behaviors (sort/dedup, digest schema, etc.) are pinned by
`smoke_watchlist.py` / `smoke_user_config.py` / `smoke_digest.py`; this
suite covers the shared base contract — primarily the two-tier
corrupt-JSON recovery documented in storage/CLAUDE.md.

Run with: pytest tests/test_storage.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _fresh_data_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="tg_bot_storage_smoke_"))
    os.environ["TG_BOT_DATA_DIR"] = str(d)
    return d


async def test_corrupt_json_resets_state_and_renames_file() -> None:
    """Pins the documented corrupt-JSON recovery path in `_base.py:30-58`:
    on `JSONDecodeError` the file is renamed to `<name>.corrupt-<ts>`,
    state resets to `{}`, and reads keep working. Without this, a single
    truncated write would crash the bot on every startup until an
    operator hand-deleted the file."""
    d = _fresh_data_dir()
    path = d / "watchlist.json"
    # Syntactically invalid JSON — exactly what a half-written save (or
    # hand-edit gone wrong) leaves behind. The opening `{` lets the
    # decoder commit before failing, matching the real-world shape.
    path.write_text("{ not valid json")

    from tg_bot.storage.watchlist import WatchlistStorage

    storage = WatchlistStorage(path)

    # (a) Internal state resets to {} so the bot keeps running.
    assert storage._data == {}, (
        f"corrupt recovery should reset to empty; got {storage._data!r}"
    )

    # (b) A `<name>.corrupt-<unix_ts>` sibling exists — operator can
    # inspect / salvage. The exact suffix is `corrupt-<int>` so glob it.
    backups = sorted(d.glob("watchlist.json.corrupt-*"))
    assert len(backups) == 1, (
        f"expected exactly 1 backup, got {[p.name for p in backups]}"
    )
    # And the original file is gone (renamed, not copied).
    assert not path.is_file(), "original watchlist.json should be renamed away"

    # (c) Reads against an unknown user return [] without crashing —
    # the contract that lets `/watch` render its empty-watchlist nudge
    # instead of a 500 in the handler.
    assert storage.get_watchlist("1") == []


async def test_save_failure_leaves_original_intact_and_no_tempfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins Invariant #8 / `_base.py:_save` atomic-write durability at the
    shared `JsonStorage` base level (inherited by WatchlistStorage AND
    UserConfigStorage). `_save` writes to a tempfile, fsyncs, then
    `os.replace`s into place; if the rename fails it unlinks the tempfile
    and re-raises. The guarantee under test: a failed save NEVER corrupts
    the existing on-disk file (no partial/truncated write) and NEVER leaves
    an orphan `*.tmp` accumulating in `data/`. Only the cache layer pinned
    this before (test_cache.py); the storage base was unpinned."""
    d = _fresh_data_dir()
    path = d / "watchlist.json"

    # Seed a known-good, complete JSON file — the "only copy of user data".
    seed = {"1": ["AAPL", "MSFT"]}
    path.write_text(json.dumps(seed, indent=2, ensure_ascii=False), encoding="utf-8")

    from tg_bot.storage import _base
    from tg_bot.storage.watchlist import WatchlistStorage

    storage = WatchlistStorage(path)

    # Make the atomic rename fail mid-save. Patch the exact `os.replace`
    # that `_base._save` calls (it does `import os` then `os.replace(...)`),
    # so this hits the real write path, not a stand-in.
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr(_base.os, "replace", _boom)

    # Mutate-then-save: `add_ticker` appends to `_data` then awaits
    # `_save_async` → `_save`, where `os.replace` now blows up. The OSError
    # must propagate (the except unlinks the tempfile and re-raises).
    with pytest.raises(OSError, match="simulated rename failure"):
        await storage.add_ticker("1", "NVDA")

    # (a) The original on-disk file is untouched — same bytes, still
    # complete/valid JSON, no NVDA leaked through, not truncated to a
    # partial write.
    raw = path.read_text(encoding="utf-8")
    assert json.loads(raw) == seed, (
        f"failed save must not corrupt the original file; got {raw!r}"
    )

    # (b) No orphan tempfile left behind. `_save` names tempfiles
    # `<name>.<random>.tmp` in the same dir; the except unlinks on failure.
    leftover = sorted(p.name for p in d.glob("*.tmp"))
    assert leftover == [], f"failed save leaked tempfiles: {leftover}"
