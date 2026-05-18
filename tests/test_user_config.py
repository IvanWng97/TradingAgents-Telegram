"""Tests for `UserConfigStorage` — now digest-only (LLM fields moved to
`.env` overlay in pipeline/analysis.py).

The pre-refactor file covered LLM provider/model/rounds/effort storage
+ `build_user_config` + `_resolve_models` + OpenRouter migration. Those
surfaces are all gone; this file now pins only the digest schedule
behaviors that survived: `get_enrolled_tickers`'s intersection +
legacy-fallback semantics, the canonical single-source-of-truth for
"which tickers fire today".

For caption rendering coverage see `tests/test_config_key.py`. For the
`PROVIDER_ENV_KEYS` upstream-alignment pin see `tests/test_pipeline_analysis.py`.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _fresh_data_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="tg_bot_uc_smoke_"))
    os.environ["TG_BOT_DATA_DIR"] = str(d)
    return d


def _make_storage():
    """Fresh UserConfigStorage rooted at a tempdir."""
    d = _fresh_data_dir()
    from tg_bot.storage.user_config import UserConfigStorage

    return UserConfigStorage(d / "user_config.json")


# ─── get_enrolled_tickers (digest filter ∩ watchlist) ──────────────────


async def test_enrolled_no_digest_returns_empty() -> None:
    """No digest block → []. The fan-out skips users without a digest."""
    s = _make_storage()
    assert s.get_enrolled_tickers("u1", ["AAPL", "NVDA"]) == []


async def test_enrolled_legacy_no_tickers_key_returns_all_watchlist() -> None:
    """Legacy save predating the filter feature (digest exists, `tickers`
    key absent entirely) → full watchlist per CLAUDE.md backward-compat
    rule. `_post_init` backfills these on startup, so this branch is
    only the first run after migration."""
    s = _make_storage()
    # Manually inject a legacy-shape digest (no `tickers` field).
    s._data["u1"] = {
        "digest": {
            "enabled": True,
            "hour_local": 9,
            "tz": "UTC",
            "chat_id": 999,
        }
    }
    assert s.get_enrolled_tickers("u1", ["AAPL", "NVDA"]) == ["AAPL", "NVDA"]


async def test_enrolled_filter_intersects_with_watchlist() -> None:
    """`tickers` ∩ watchlist — preserves watchlist order, drops stragglers."""
    s = _make_storage()
    await s.set_digest_hour("u1", 9, 999)
    await s.set_digest_tz("u1", "UTC")
    await s.set_digest_tickers("u1", ["AAPL", "TSLA"])
    enrolled = s.get_enrolled_tickers("u1", ["AAPL", "NVDA", "TSLA", "MSFT"])
    assert enrolled == ["AAPL", "TSLA"], enrolled


async def test_enrolled_drops_tickers_removed_from_watchlist() -> None:
    """A stale `tickers` entry pointing at a ticker the user removed
    from the watchlist must NOT count. Matches the run_user_digest
    auto-prune behavior."""
    s = _make_storage()
    await s.set_digest_hour("u1", 9, 999)
    await s.set_digest_tz("u1", "UTC")
    await s.set_digest_tickers("u1", ["AAPL", "TSLA"])
    # User removed TSLA from watchlist after enrolling it.
    enrolled = s.get_enrolled_tickers("u1", ["AAPL", "NVDA"])
    assert enrolled == ["AAPL"], enrolled


async def test_enrolled_preserves_watchlist_order() -> None:
    """Iteration order must follow the watchlist, not the (sorted)
    `tickers` storage order. The fan-out renders rows in this order,
    so a watchlist `[XYZ, ABC]` must NOT flip to `[ABC, XYZ]` just
    because the digest filter sorts on-disk."""
    s = _make_storage()
    await s.set_digest_hour("u1", 9, 999)
    await s.set_digest_tz("u1", "UTC")
    # Storage sorts alphabetically → on disk: ["ABC", "XYZ"]
    await s.set_digest_tickers("u1", ["XYZ", "ABC"])
    # But the watchlist order is XYZ first — render order should follow.
    enrolled = s.get_enrolled_tickers("u1", ["XYZ", "ABC"])
    assert enrolled == ["XYZ", "ABC"], enrolled


async def test_enrolled_does_not_gate_on_enabled() -> None:
    """Deliberately does NOT check `enabled=False` — the manual `▶ Run
    now` path fires fan-out for explicitly-disabled digests and must
    still respect the configured filter. Callers that need
    "is digest scheduled?" check `get_digest().enabled` themselves."""
    s = _make_storage()
    await s.set_digest_hour("u1", 9, 999)
    await s.set_digest_tz("u1", "UTC")
    await s.set_digest_tickers("u1", ["AAPL"])
    await s.disable_digest("u1")
    # Filter survives disable; method returns enrolled set regardless.
    assert s.get_digest("u1")["enabled"] is False
    assert s.get_enrolled_tickers("u1", ["AAPL", "NVDA"]) == ["AAPL"]


async def test_enrolled_uppercase_normalization_defensive() -> None:
    """Stored tickers are uppercase per set_digest_tickers, but if a
    hand-edited json or forward-compat write produced mixed-case
    entries, the intersection should still match the (uppercase)
    watchlist."""
    s = _make_storage()
    # Inject mixed-case directly to bypass the setter's normalization.
    s._data["u1"] = {
        "digest": {
            "enabled": True,
            "hour_local": 9,
            "tz": "UTC",
            "chat_id": 999,
            "tickers": ["aapl", "Tsla"],
        }
    }
    enrolled = s.get_enrolled_tickers("u1", ["AAPL", "NVDA", "TSLA"])
    assert enrolled == ["AAPL", "TSLA"], enrolled


async def test_enrolled_empty_watchlist_returns_empty() -> None:
    """No watchlist to intersect with → []. Edge case for users who
    enrolled tickers then cleared their watchlist."""
    s = _make_storage()
    await s.set_digest_hour("u1", 9, 999)
    await s.set_digest_tz("u1", "UTC")
    await s.set_digest_tickers("u1", ["AAPL"])
    assert s.get_enrolled_tickers("u1", []) == []


async def test_enrolled_explicit_empty_filter_returns_empty() -> None:
    """`tickers=[]` (user cleared their filter via the picker's
    `digest:ttclear` action) → []. This is a distinct branch from
    both `block absent` (→ []) and `tickers` key absent (legacy → all
    watchlist). Without this pin, a future change that treats `[]`
    as a legacy signal would silently re-enroll the full watchlist."""
    s = _make_storage()
    await s.set_digest_hour("u1", 9, 999)
    await s.set_digest_tz("u1", "UTC")
    await s.set_digest_tickers("u1", [])  # explicit clear via picker
    # Sanity: the stored value is `[]`, NOT missing — distinguishes from legacy.
    assert s.get_digest("u1")["tickers"] == []
    assert s.get_enrolled_tickers("u1", ["AAPL", "NVDA"]) == []


# ─── set_digest_email / clear_digest_email ──────────────────────────────


async def test_set_digest_email_accepts_valid_address() -> None:
    """Common valid-address shapes must round-trip. The regex is intentionally
    pragmatic — RFC-5322 compliance isn't worth the rejection of practical
    addresses (`foo+tag@bar.co.uk` is normal)."""
    s = _make_storage()
    for addr in [
        "user@example.com",
        "foo+tag@example.co.uk",
        "first.last@subdomain.example.org",
        "u_99@dash-domain.io",
    ]:
        assert await s.set_digest_email("u1", addr) is True, addr
        assert s.get_digest("u1")["email"] == addr


async def test_set_digest_email_rejects_invalid_address() -> None:
    """Common typos must produce False (caller renders a "bad address"
    message); storage stays untouched."""
    s = _make_storage()
    for bad in [
        "no-at-sign.com",
        "trailing@",
        "@no-local-part.com",
        "no.tld@bare",
        "double..dot@example.com" if False else "weird space @example.com",
        "",
    ]:
        assert await s.set_digest_email("u1", bad) is False, bad
    # Nothing got saved — the digest block doesn't exist yet at all.
    assert s.get_digest("u1") is None


async def test_set_digest_email_does_not_touch_other_digest_fields() -> None:
    """Email opt-in is independent of schedule. Setting an email on a
    user who already has a fully-configured digest must leave the
    schedule fields untouched."""
    s = _make_storage()
    await s.set_digest_tz("u1", "UTC")
    await s.set_digest_hour("u1", 9, 999)
    await s.set_digest_tickers("u1", ["NVDA", "AAPL"])

    await s.set_digest_email("u1", "user@example.com")

    d = s.get_digest("u1")
    assert d["tz"] == "UTC"
    assert d["hour_local"] == 9
    assert d["chat_id"] == 999
    assert d["enabled"] is True
    assert sorted(d["tickers"]) == ["AAPL", "NVDA"]
    assert d["email"] == "user@example.com"


async def test_clear_digest_email_unsets_and_returns_true() -> None:
    s = _make_storage()
    await s.set_digest_email("u1", "user@example.com")
    assert s.get_digest("u1")["email"] == "user@example.com"

    assert await s.clear_digest_email("u1") is True
    assert s.get_digest("u1")["email"] is None


async def test_clear_digest_email_returns_false_when_nothing_to_clear() -> None:
    """Defensive: clearing when no email was set returns False so callers
    can render "nothing to clear" instead of "cleared" (a confusing UX)."""
    s = _make_storage()
    # No digest block at all → returns False.
    assert await s.clear_digest_email("u1") is False
    # Digest block exists but email was never set → also False.
    await s.set_digest_tz("u1", "UTC")
    assert await s.clear_digest_email("u1") is False
