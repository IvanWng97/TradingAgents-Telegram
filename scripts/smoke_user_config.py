"""Smoke tests for the rounds + effort knobs added to /config.

Covers:
- UserConfigStorage CRUD for max_debate_rounds and effort_level
- LLM_KEYS membership so clear() rolls them back
- set_llm_provider preserves rounds/effort (they're provider-agnostic)
- build_user_config merges rounds + effort and maps effort to the
  provider-specific key (openai_reasoning_effort / anthropic_effort /
  google_thinking_level)
- AnalysisConfigKey.caption() line shape

Run with: .venv/bin/python3 scripts/smoke_user_config.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


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


# ─── storage CRUD ──────────────────────────────────────────────────────


async def test_rounds_default_is_one() -> None:
    s = _make_storage()
    assert s.get_max_debate_rounds("u1") == 1


async def test_rounds_set_get_roundtrip() -> None:
    s = _make_storage()
    assert await s.set_max_debate_rounds("u1", 2) is True
    assert s.get_max_debate_rounds("u1") == 2
    assert await s.set_max_debate_rounds("u1", 3) is True
    assert s.get_max_debate_rounds("u1") == 3


async def test_rounds_rejects_out_of_range() -> None:
    s = _make_storage()
    assert await s.set_max_debate_rounds("u1", 0) is False
    assert await s.set_max_debate_rounds("u1", 4) is False
    assert await s.set_max_debate_rounds("u1", 99) is False
    # Unset → still default
    assert s.get_max_debate_rounds("u1") == 1


async def test_effort_default_is_none() -> None:
    s = _make_storage()
    assert s.get_effort_level("u1") is None


async def test_effort_set_get_roundtrip() -> None:
    s = _make_storage()
    for level in ("low", "medium", "high"):
        assert await s.set_effort_level("u1", level) is True
        assert s.get_effort_level("u1") == level


async def test_effort_clear_via_none() -> None:
    s = _make_storage()
    await s.set_effort_level("u1", "high")
    assert s.get_effort_level("u1") == "high"
    assert await s.set_effort_level("u1", None) is True
    assert s.get_effort_level("u1") is None


async def test_effort_rejects_invalid() -> None:
    s = _make_storage()
    assert await s.set_effort_level("u1", "extreme") is False
    assert await s.set_effort_level("u1", "") is False
    assert s.get_effort_level("u1") is None


async def test_clear_wipes_rounds_effort() -> None:
    """clear() (used by /config Cancel rollback when no prior provider) must
    pop rounds + effort along with provider/deep/quick — otherwise canceling
    out of a fresh first-time /config leaves stale rounds/effort behind."""
    s = _make_storage()
    await s.set_llm_provider("u1", "openai")
    await s.set_max_debate_rounds("u1", 3)
    await s.set_effort_level("u1", "high")
    assert await s.clear("u1") is True
    assert s.get_llm_provider("u1") is None
    assert s.get_max_debate_rounds("u1") == 1  # back to default
    assert s.get_effort_level("u1") is None


async def test_set_provider_preserves_rounds_effort() -> None:
    """rounds/effort are provider-agnostic — switching providers should
    keep the user's quality preferences."""
    s = _make_storage()
    await s.set_llm_provider("u1", "openai")
    await s.set_max_debate_rounds("u1", 2)
    await s.set_effort_level("u1", "medium")
    # Switch provider → deep/quick clear (provider-specific) but not rounds/effort.
    await s.set_llm_provider("u1", "anthropic")
    assert s.get_max_debate_rounds("u1") == 2
    assert s.get_effort_level("u1") == "medium"


# ─── build_user_config integration ────────────────────────────────────


async def test_build_user_config_default_rounds_no_effort() -> None:
    s = _make_storage()
    await s.set_llm_provider("u1", "openai")
    from tg_bot.analysis import build_user_config

    config = build_user_config("u1", s)
    assert config["max_debate_rounds"] == 1
    # No effort → no provider-specific key written.
    assert config.get("openai_reasoning_effort") is None


async def test_build_user_config_maps_effort_per_provider() -> None:
    """Same vocabulary value 'high' lands under three different keys
    depending on which provider the user has picked."""
    from tg_bot.analysis import build_user_config

    cases = [
        ("openai", "openai_reasoning_effort"),
        ("anthropic", "anthropic_effort"),
        ("google", "google_thinking_level"),
    ]
    for provider, expected_key in cases:
        s = _make_storage()
        await s.set_llm_provider("u1", provider)
        await s.set_effort_level("u1", "high")
        config = build_user_config("u1", s)
        assert config.get(expected_key) == "high", (
            f"{provider}: expected {expected_key}=high, got {config.get(expected_key)!r}"
        )


async def test_build_user_config_skips_effort_for_unsupported_provider() -> None:
    """Providers not in PROVIDERS_WITH_EFFORT (deepseek, qwen, etc.) must
    silently ignore the effort knob — no provider key gets set, no warning."""
    from tg_bot.analysis import build_user_config

    s = _make_storage()
    await s.set_llm_provider("u1", "deepseek")
    await s.set_effort_level("u1", "high")
    config = build_user_config("u1", s)
    # None of the three effort keys should be set.
    for key in ("openai_reasoning_effort", "anthropic_effort", "google_thinking_level"):
        assert config.get(key) is None, f"{key} should be unset for deepseek"


async def test_build_user_config_writes_rounds_even_without_provider() -> None:
    """A user who never ran /config still gets max_debate_rounds=1 in the
    resolved config (matches DEFAULT_CONFIG, doesn't trip tradingagents)."""
    s = _make_storage()
    from tg_bot.analysis import build_user_config

    config = build_user_config("u1", s)
    assert config["max_debate_rounds"] == 1


# ─── OpenRouter stale-slug migration ──────────────────────────────────


async def test_openrouter_migration_rewrites_stale_sonnet_slug() -> None:
    """A user who picked `anthropic/claude-3.5-sonnet` weeks ago via
    /config has it in their saved storage; OpenRouter has since retired
    that slug. `_resolve_models` rewrites on read so the analysis runs
    against the current `anthropic/claude-sonnet-4.5` slug without
    needing the user to rerun /config."""
    s = _make_storage()
    await s.set_llm_provider("u1", "openrouter")
    await s.set_llm_model("u1", "deep", "anthropic/claude-3.5-sonnet")
    from tg_bot.analysis import _resolve_models

    deep, _quick = _resolve_models("u1", s, "openrouter")
    assert deep == "anthropic/claude-sonnet-4.5", deep


async def test_openrouter_migration_scoped_to_openrouter_provider() -> None:
    """Migration only fires for `provider == "openrouter"`. A user on a
    different provider whose stored model ID happens to collide with a
    stale openrouter slug (highly unlikely but defensive) is not rewritten."""
    s = _make_storage()
    await s.set_llm_provider("u1", "anthropic")
    await s.set_llm_model("u1", "deep", "anthropic/claude-3.5-sonnet")
    from tg_bot.analysis import _resolve_models

    deep, _quick = _resolve_models("u1", s, "anthropic")
    # Migration is openrouter-only — the value passes through unchanged.
    # (Anthropic's own catalog uses bare model IDs like "claude-sonnet-4",
    # not the slash-prefixed openrouter form, so this collision is
    # synthetic; the assertion pins the scope guard.)
    assert deep == "anthropic/claude-3.5-sonnet", deep


async def test_openrouter_migration_passes_through_unknown_slugs() -> None:
    """Slugs not in the migration map (current models, custom IDs) pass
    through unchanged — the map is opt-in by entry, not opt-out by
    pattern."""
    s = _make_storage()
    await s.set_llm_provider("u1", "openrouter")
    await s.set_llm_model("u1", "deep", "meta-llama/llama-3.3-70b-instruct:free")
    from tg_bot.analysis import _resolve_models

    deep, _quick = _resolve_models("u1", s, "openrouter")
    assert deep == "meta-llama/llama-3.3-70b-instruct:free", deep


# ─── caption rendering (via AnalysisConfigKey) ─────────────────────────


async def test_config_summary_default() -> None:
    from tg_bot.config_key import AnalysisConfigKey

    out = AnalysisConfigKey.from_config(
        {
            "llm_provider": "openai",
            "deep_think_llm": "gpt-4o",
            "quick_think_llm": "o4-mini",
            "max_debate_rounds": 1,
        }
    ).caption()
    assert out == "openai · gpt-4o/o4-mini", out


async def test_config_summary_custom_rounds() -> None:
    from tg_bot.config_key import AnalysisConfigKey

    out = AnalysisConfigKey.from_config(
        {
            "llm_provider": "deepseek",
            "deep_think_llm": "deepseek-v4-pro",
            "quick_think_llm": "deepseek-v4-flash",
            "max_debate_rounds": 2,
        }
    ).caption()
    # Both models present, rounds suffix appended, no effort marker.
    assert "deepseek-v4-pro/deepseek-v4-flash" in out, out
    assert "· r2" in out, out
    assert "e=" not in out, out


async def test_config_summary_with_effort() -> None:
    from tg_bot.config_key import AnalysisConfigKey

    out = AnalysisConfigKey.from_config(
        {
            "llm_provider": "anthropic",
            "deep_think_llm": "claude-sonnet-4",
            "quick_think_llm": "claude-haiku-4",
            "max_debate_rounds": 2,
            "anthropic_effort": "high",
        }
    ).caption()
    assert "claude-sonnet-4/claude-haiku-4" in out, out
    assert "· r2 · e=high" in out, out


# ─── upstream env-key alignment ────────────────────────────────────────


async def test_provider_env_keys_match_upstream() -> None:
    """The bot's `PROVIDER_ENV_KEYS` must agree with upstream tradingagents'
    `PROVIDER_API_KEY_ENV` for every shared provider. A divergence (like
    the GLM `ZHIPUAI_API_KEY` → `ZHIPU_API_KEY` rename in v0.2.5) silently
    breaks: the bot's precheck passes by reading the old env var while
    tradingagents itself reads the new one and 401s on first API call."""
    from tg_bot.analysis import PROVIDER_ENV_KEYS
    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

    shared = set(PROVIDER_ENV_KEYS) & set(PROVIDER_API_KEY_ENV)
    mismatches = [
        (p, PROVIDER_ENV_KEYS[p], PROVIDER_API_KEY_ENV[p])
        for p in shared
        if PROVIDER_ENV_KEYS[p] != PROVIDER_API_KEY_ENV[p]
    ]
    assert not mismatches, "bot ↔ upstream env-key drift:\n" + "\n".join(
        f"  {p}: bot={b!r} upstream={u!r}" for p, b, u in mismatches
    )


async def test_valid_providers_includes_minimax() -> None:
    """MiniMax is a first-class provider upstream as of tradingagents v0.2.5;
    the bot must list it in VALID_PROVIDERS so /config can route to it."""
    from tg_bot.storage.user_config import UserConfigStorage

    assert "minimax" in UserConfigStorage.VALID_PROVIDERS, (
        UserConfigStorage.VALID_PROVIDERS
    )


SCENARIOS = [
    ("rounds default is 1", test_rounds_default_is_one),
    ("rounds set/get round-trip", test_rounds_set_get_roundtrip),
    ("rounds reject out-of-range", test_rounds_rejects_out_of_range),
    ("effort default is None", test_effort_default_is_none),
    ("effort set/get round-trip", test_effort_set_get_roundtrip),
    ("effort clear via None", test_effort_clear_via_none),
    ("effort reject invalid", test_effort_rejects_invalid),
    ("clear() wipes rounds+effort", test_clear_wipes_rounds_effort),
    ("set_provider preserves rounds+effort", test_set_provider_preserves_rounds_effort),
    (
        "build_user_config default rounds, no effort",
        test_build_user_config_default_rounds_no_effort,
    ),
    (
        "build_user_config maps effort per provider",
        test_build_user_config_maps_effort_per_provider,
    ),
    (
        "build_user_config skips effort for unsupported provider",
        test_build_user_config_skips_effort_for_unsupported_provider,
    ),
    (
        "build_user_config writes rounds even without provider",
        test_build_user_config_writes_rounds_even_without_provider,
    ),
    (
        "openrouter migration rewrites stale sonnet slug",
        test_openrouter_migration_rewrites_stale_sonnet_slug,
    ),
    (
        "openrouter migration scoped to openrouter provider",
        test_openrouter_migration_scoped_to_openrouter_provider,
    ),
    (
        "openrouter migration passes through unknown slugs",
        test_openrouter_migration_passes_through_unknown_slugs,
    ),
    ("config_summary default", test_config_summary_default),
    ("config_summary with custom rounds", test_config_summary_custom_rounds),
    ("config_summary with effort", test_config_summary_with_effort),
    (
        "PROVIDER_ENV_KEYS aligns with tradingagents upstream",
        test_provider_env_keys_match_upstream,
    ),
    ("VALID_PROVIDERS includes minimax", test_valid_providers_includes_minimax),
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
