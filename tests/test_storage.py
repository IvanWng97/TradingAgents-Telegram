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

import os
import sys
import tempfile
from pathlib import Path


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
