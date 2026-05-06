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

Run with: .venv/bin/python3 scripts/smoke_digest.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

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
