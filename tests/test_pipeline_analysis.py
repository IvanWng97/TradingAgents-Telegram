"""Tests for `pipeline/analysis.py` env-overlay + config-building primitives.

These pin the contract that `.env` is the single source of truth for the
bot's LLM configuration. The legacy `build_user_config(user_id, storage)`
path stays covered by `test_user_config.py` until that path is removed in
a later wave of the refactor.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_bot.pipeline.analysis import (  # noqa: E402
    EFFORT_KEY_BY_PROVIDER,
    _LOCAL_EFFORT_OVERRIDES,
    _apply_local_effort_overlay,
    build_config,
    check_llm_configured,
    get_current_config_key,
)
from tg_bot.pipeline.config_key import AnalysisConfigKey  # noqa: E402


# ─── _LOCAL_EFFORT_OVERRIDES derivation ─────────────────────────────────


async def test_local_effort_overrides_derived_from_effort_keys() -> None:
    """Pin the derivation: each env var name is `TRADINGAGENTS_` + the
    upper-cased config-key, mapping back to its original config-key.
    Adding a provider to `EFFORT_KEY_BY_PROVIDER` automatically extends
    the overlay — no second-edit needed."""
    expected = {
        f"TRADINGAGENTS_{key.upper()}": key for key in EFFORT_KEY_BY_PROVIDER.values()
    }
    assert _LOCAL_EFFORT_OVERRIDES == expected
    # Concrete snapshot so a typo in EFFORT_KEY_BY_PROVIDER would fail
    # loudly here too, not just the derivation rule.
    assert _LOCAL_EFFORT_OVERRIDES == {
        "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
        "TRADINGAGENTS_ANTHROPIC_EFFORT": "anthropic_effort",
        "TRADINGAGENTS_GOOGLE_THINKING_LEVEL": "google_thinking_level",
    }


# ─── _apply_local_effort_overlay ────────────────────────────────────────


async def test_overlay_writes_openai_effort_when_env_set() -> None:
    config = {"llm_provider": "openai", "openai_reasoning_effort": None}
    _apply_local_effort_overlay(
        config, env={"TRADINGAGENTS_OPENAI_REASONING_EFFORT": "high"}
    )
    assert config["openai_reasoning_effort"] == "high"


async def test_overlay_writes_anthropic_effort_when_env_set() -> None:
    config = {"llm_provider": "anthropic", "anthropic_effort": None}
    _apply_local_effort_overlay(
        config, env={"TRADINGAGENTS_ANTHROPIC_EFFORT": "medium"}
    )
    assert config["anthropic_effort"] == "medium"


async def test_overlay_writes_google_thinking_level_when_env_set() -> None:
    config = {"llm_provider": "google", "google_thinking_level": None}
    _apply_local_effort_overlay(
        config, env={"TRADINGAGENTS_GOOGLE_THINKING_LEVEL": "low"}
    )
    assert config["google_thinking_level"] == "low"


async def test_overlay_no_op_when_env_empty() -> None:
    """An env mapping with no relevant vars leaves the config untouched
    — the upstream default (None) survives."""
    config = {
        "llm_provider": "openai",
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "google_thinking_level": None,
    }
    snapshot = dict(config)
    _apply_local_effort_overlay(config, env={"UNRELATED_VAR": "value"})
    assert config == snapshot


async def test_overlay_skips_empty_string_values() -> None:
    """An empty-string env var is treated as unset — matches the
    "falsy means missing" convention used throughout the bot."""
    config = {"openai_reasoning_effort": None}
    _apply_local_effort_overlay(
        config, env={"TRADINGAGENTS_OPENAI_REASONING_EFFORT": ""}
    )
    assert config["openai_reasoning_effort"] is None


async def test_overlay_writes_all_three_when_all_set() -> None:
    """All effort keys present simultaneously — the config doesn't reject
    multiple keys being set (only one matches the active provider, but
    that's `AnalysisConfigKey.from_config`'s problem)."""
    config = {
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "google_thinking_level": None,
    }
    _apply_local_effort_overlay(
        config,
        env={
            "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "high",
            "TRADINGAGENTS_ANTHROPIC_EFFORT": "medium",
            "TRADINGAGENTS_GOOGLE_THINKING_LEVEL": "low",
        },
    )
    assert config == {
        "openai_reasoning_effort": "high",
        "anthropic_effort": "medium",
        "google_thinking_level": "low",
    }


# ─── build_config ───────────────────────────────────────────────────────


async def test_build_config_returns_dict() -> None:
    config = build_config()
    assert isinstance(config, dict)
    # tradingagents ships these baseline keys; missing means the upstream
    # import broke OR the dict was returned uninitialized.
    assert "llm_provider" in config
    assert "deep_think_llm" in config


async def test_build_config_returns_fresh_copy() -> None:
    """Mutating the returned dict must not poison the next call's config.
    Caller paths (cache lookup, graph pool, etc.) get their own copy."""
    config_a = build_config()
    config_a["llm_provider"] = "mutated"
    config_b = build_config()
    assert config_b["llm_provider"] != "mutated", (
        "build_config returned a shared reference — caller mutations leak"
    )


# ─── get_current_config_key ─────────────────────────────────────────────


async def test_get_current_config_key_returns_analysis_config_key() -> None:
    key = get_current_config_key()
    assert isinstance(key, AnalysisConfigKey)


async def test_get_current_config_key_matches_build_config() -> None:
    """The cached/computed key must reflect what `build_config()` returns —
    if these diverge, cache slug ≠ pool key ≠ Telegraph title."""
    key = get_current_config_key()
    config = build_config()
    assert key == AnalysisConfigKey.from_config(config)


# ─── check_llm_configured ───────────────────────────────────────────────


async def test_check_llm_configured_no_provider_reports_reason() -> None:
    """An empty `llm_provider` produces a `no provider` reason — caller
    renders a "set TRADINGAGENTS_LLM_PROVIDER" message. The string contains
    "no provider" so `llm_setup_error_message.startswith("no provider")`
    matches."""
    reason = check_llm_configured({"llm_provider": ""})
    assert reason is not None and "no provider" in reason


async def test_check_llm_configured_missing_env_key_names_var() -> None:
    """Provider picked but matching env var unset → reason names the
    expected env var by name (`DEEPSEEK_API_KEY` etc.), not a generic
    'auth failed' downstream from the SDK."""
    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        reason = check_llm_configured({"llm_provider": "deepseek"})
        assert reason is not None and "DEEPSEEK_API_KEY" in reason, reason
    finally:
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved


async def test_check_llm_configured_ok_returns_none() -> None:
    """Provider set + matching env var present → gate opens (returns None)."""
    os.environ["DEEPSEEK_API_KEY"] = "sk-test-irrelevant"
    try:
        assert check_llm_configured({"llm_provider": "deepseek"}) is None
    finally:
        os.environ.pop("DEEPSEEK_API_KEY", None)


async def test_check_llm_configured_openrouter_names_openrouter_key() -> None:
    """openrouter is listed in PROVIDER_ENV_KEYS so the precheck names
    OPENROUTER_API_KEY when unset, instead of the user hitting the cryptic
    'OPENAI_API_KEY missing' from the openai SDK at LLM-init time."""
    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        reason = check_llm_configured({"llm_provider": "openrouter"})
        assert reason is not None and "OPENROUTER_API_KEY" in reason, reason
    finally:
        if saved is not None:
            os.environ["OPENROUTER_API_KEY"] = saved


async def test_check_llm_configured_ollama_no_env_needed() -> None:
    """ollama runs locally — no env var required, gate opens immediately."""
    assert check_llm_configured({"llm_provider": "ollama"}) is None


async def test_check_llm_configured_cn_variant_names_cn_suffixed_key() -> None:
    """CN provider variants (qwen-cn / glm-cn / minimax-cn) use DISTINCT
    CN-suffixed env vars upstream — not shared with their non-CN siblings.
    Without `qwen-cn` registered in `PROVIDER_ENV_KEYS`, the precheck would
    silently pass when only `DASHSCOPE_API_KEY` (non-CN) is set but the user
    has `TRADINGAGENTS_LLM_PROVIDER=qwen-cn` — analysis then 401s at
    upstream API-call time with a cryptic message instead of the targeted
    'DASHSCOPE_CN_API_KEY not set' warning at the entry point."""
    saved_cn = os.environ.pop("DASHSCOPE_CN_API_KEY", None)
    saved_base = os.environ.pop("DASHSCOPE_API_KEY", None)
    # Set the non-CN sibling to prove the precheck doesn't fall back to it.
    os.environ["DASHSCOPE_API_KEY"] = "sk-test-irrelevant"
    try:
        reason = check_llm_configured({"llm_provider": "qwen-cn"})
        assert reason is not None and "DASHSCOPE_CN_API_KEY" in reason, reason
    finally:
        os.environ.pop("DASHSCOPE_API_KEY", None)
        if saved_base is not None:
            os.environ["DASHSCOPE_API_KEY"] = saved_base
        if saved_cn is not None:
            os.environ["DASHSCOPE_CN_API_KEY"] = saved_cn


async def test_check_llm_configured_defaults_to_build_config() -> None:
    """Calling with no args reads `build_config()` — keeps callers terse
    and ensures the precheck always asks 'is THE CURRENT config valid'."""
    # We can't assert a specific outcome (depends on the env at test time),
    # but the call must not crash. Pin the "no arg works" contract.
    result = check_llm_configured()
    # `result` is None or a non-empty reason string — both are valid; we're
    # just pinning that the no-arg path doesn't TypeError.
    assert result is None or (isinstance(result, str) and result)


# ─── PROVIDER_ENV_KEYS upstream alignment (moved from test_user_config.py) ──


async def test_provider_env_keys_match_upstream() -> None:
    """The bot's `PROVIDER_ENV_KEYS` must agree with upstream tradingagents'
    `PROVIDER_API_KEY_ENV` for every shared provider. A divergence (like
    the GLM `ZHIPUAI_API_KEY` → `ZHIPU_API_KEY` rename in v0.2.5) silently
    breaks: the bot's precheck passes by reading the old env var while
    tradingagents itself reads the new one and 401s on first API call."""
    from tg_bot.pipeline.analysis import PROVIDER_ENV_KEYS
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


# ─── pool_stats() — backs the /status "Graph pool" line ────────────────


async def test_pool_stats_returns_zero_before_lazy_init() -> None:
    """Before the first `/watch` tap builds the pool, `pool_stats()` must
    return `(0, 0)`. This drives the "not yet built" branch in `status_cmd`
    (handlers/commands.py) — without this pin, a future refactor that
    initialises `_graph_pool` eagerly would silently flip the `/status`
    text from "not yet built" to "0 instance(s) built" without any test
    failing."""
    from tg_bot.pipeline import analysis as analysis_mod

    saved = analysis_mod._graph_pool
    analysis_mod._graph_pool = None
    try:
        assert analysis_mod.pool_stats() == (0, 0)
    finally:
        analysis_mod._graph_pool = saved


async def test_pool_stats_returns_one_pool_with_instance_count_after_init() -> None:
    """After the lazy init builds the single `GraphPool`, `pool_stats()`
    returns `(1, N)` where N tracks `GraphPool.size` — the number of
    `TradingAgentsGraph` instances actually built so far (NOT the max
    capacity). Drives the `/status` "N instance(s) built" branch."""
    from tg_bot.pipeline import analysis as analysis_mod
    from tg_bot.pipeline.analysis import GraphPool

    saved = analysis_mod._graph_pool
    # Stub builder — we're testing the size accounting, not the actual
    # TradingAgentsGraph construction (which would pull in LangChain init).
    pool = GraphPool(max_size=3, builder=lambda: object())
    analysis_mod._graph_pool = pool
    try:
        # Just-installed pool has zero instances built.
        assert analysis_mod.pool_stats() == (1, 0)
        # Acquire one instance to trigger the builder, then release.
        with pool.acquire():
            assert analysis_mod.pool_stats() == (1, 1)
        # Instance returns to the pool; size stays at 1 (it's the
        # count of built instances, not the in-use count).
        assert analysis_mod.pool_stats() == (1, 1)
    finally:
        analysis_mod._graph_pool = saved
