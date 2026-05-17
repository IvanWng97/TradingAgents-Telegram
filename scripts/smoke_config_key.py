"""Smoke tests for `AnalysisConfigKey` — the single source of truth for
the (provider, deep, quick, rounds, effort) tuple shared across the
cache slug, the caption "via" line, and the Telegraph title suffix.

Cross-cutting invariant #1 in CLAUDE.md is the human-enforced "keep all
three shapes in sync when adding a knob" rule; these scenarios are the
structural enforcement — extending the dataclass with a new field
without updating slug/caption/title produces visible test failures.

Callers (cache.py, callbacks.py, analysis.py) consume the dataclass
directly — there are no longer any thin delegators (`cache._slug` and
`formatters.build_config_summary` were inlined in the cleanup PR).

Run with: .venv/bin/python3 scripts/smoke_config_key.py
"""

from __future__ import annotations

import sys
from pathlib import Path


PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_bot.pipeline.config_key import AnalysisConfigKey  # noqa: E402


# ---------- from_config ----------


def test_from_config_default_rounds_no_effort() -> None:
    """Default-config user (rounds=1 omitted from the dict, no effort
    set anywhere) — `from_config` should produce rounds=1 + effort=None."""
    k = AnalysisConfigKey.from_config(
        {
            "llm_provider": "openai",
            "deep_think_llm": "gpt-4o",
            "quick_think_llm": "o4-mini",
        }
    )
    assert k.provider == "openai"
    assert k.deep == "gpt-4o"
    assert k.quick == "o4-mini"
    assert k.rounds == 1
    assert k.effort is None


def test_from_config_custom_rounds() -> None:
    k = AnalysisConfigKey.from_config(
        {
            "llm_provider": "deepseek",
            "deep_think_llm": "deepseek-v4-pro",
            "quick_think_llm": "deepseek-v4-flash",
            "max_debate_rounds": 2,
        }
    )
    assert k.rounds == 2
    assert k.effort is None


def test_from_config_openai_effort_picks_up() -> None:
    """OpenAI stores effort under `openai_reasoning_effort` — `from_config`
    iterates `EFFORT_KEY_BY_PROVIDER.values()` so the provider-specific
    key gets picked up without the dataclass knowing the mapping."""
    k = AnalysisConfigKey.from_config(
        {
            "llm_provider": "openai",
            "deep_think_llm": "gpt-4o",
            "quick_think_llm": "o4-mini",
            "openai_reasoning_effort": "high",
        }
    )
    assert k.effort == "high"


def test_from_config_anthropic_effort_picks_up() -> None:
    k = AnalysisConfigKey.from_config(
        {
            "llm_provider": "anthropic",
            "deep_think_llm": "claude-sonnet-4",
            "quick_think_llm": "claude-haiku-4",
            "anthropic_effort": "medium",
        }
    )
    assert k.effort == "medium"


def test_from_config_stale_provider_effort_key_wins_first() -> None:
    """First-truthy-wins iteration over EFFORT_KEY_BY_PROVIDER means a
    stale `openai_reasoning_effort` lingering in the dict beats the
    current-provider `anthropic_effort`. This is the silent
    misattribution flagged in pipeline/CLAUDE.md — pin it so a future
    iteration-order change (or alphabetical sort) is loud."""
    k = AnalysisConfigKey.from_config(
        {
            "llm_provider": "anthropic",
            "deep_think_llm": "claude-sonnet-4",
            "quick_think_llm": "claude-haiku-4",
            # Stale leftover from a prior provider — should NOT win in
            # principle, but the iteration order makes it.
            "openai_reasoning_effort": "high",
            "anthropic_effort": "medium",
        }
    )
    # provider tracks the user's current selection; effort silently
    # misattributes to the stale openai value because EFFORT_KEY_BY_PROVIDER
    # iterates openai → anthropic → google and stops at the first truthy.
    assert k.provider == "anthropic"
    assert k.deep == "claude-sonnet-4"
    assert k.quick == "claude-haiku-4"
    assert k.effort == "high", (
        "first-truthy-wins should pick openai's stale value before anthropic's; "
        "if this fails, the iteration order changed — re-audit pipeline/CLAUDE.md"
    )


def test_from_config_missing_provider_falls_back() -> None:
    """Defensive: bot defaults to 'unknown' if provider key is missing
    so the key still constructs and the slug is still filesystem-safe."""
    k = AnalysisConfigKey.from_config({})
    assert k.provider == "unknown"
    assert k.deep == "default"
    assert k.quick == "default"


# ---------- slug() ----------


def test_slug_default_shape_omits_rounds_and_effort() -> None:
    """Default user (rounds=1, effort=None) → no `__r{n}` / `__e{level}`
    suffixes. Critical for backward compat — existing default-config
    users mustn't lose their cache slot."""
    k = AnalysisConfigKey(provider="openai", deep="gpt-4o", quick="o4-mini")
    assert k.slug() == "openai__gpt-4o__o4-mini"


def test_slug_appends_rounds_when_nondefault() -> None:
    k = AnalysisConfigKey(
        provider="deepseek",
        deep="deepseek-v4-pro",
        quick="deepseek-v4-flash",
        rounds=2,
    )
    assert k.slug() == "deepseek__deepseek-v4-pro__deepseek-v4-flash__r2"


def test_slug_appends_effort_when_set() -> None:
    k = AnalysisConfigKey(
        provider="openai", deep="gpt-4o", quick="o4-mini", effort="high"
    )
    assert k.slug() == "openai__gpt-4o__o4-mini__ehigh"


def test_slug_appends_both_when_both_customized() -> None:
    k = AnalysisConfigKey(
        provider="openai", deep="gpt-4o", quick="o4-mini", rounds=3, effort="low"
    )
    assert k.slug() == "openai__gpt-4o__o4-mini__r3__elow"


def test_slug_sanitizes_filesystem_unsafe_chars() -> None:
    """OpenRouter / Azure use `provider/model` form. The slug must be
    filesystem-safe — slashes squashed to underscores."""
    k = AnalysisConfigKey(
        provider="openrouter", deep="openai/gpt-4o", quick="anthropic/claude-haiku-4"
    )
    s = k.slug()
    assert "/" not in s
    assert "openai_gpt-4o" in s
    assert "anthropic_claude-haiku-4" in s


# ---------- caption() ----------


def test_caption_default_shape() -> None:
    k = AnalysisConfigKey(provider="openai", deep="gpt-4o", quick="o4-mini")
    assert k.caption() == "openai · gpt-4o/o4-mini"


def test_caption_appends_rounds_when_nondefault() -> None:
    k = AnalysisConfigKey(
        provider="deepseek",
        deep="deepseek-v4-pro",
        quick="deepseek-v4-flash",
        rounds=2,
    )
    assert k.caption() == "deepseek · deepseek-v4-pro/deepseek-v4-flash · r2"


def test_caption_appends_effort_with_equals_prefix() -> None:
    k = AnalysisConfigKey(
        provider="anthropic",
        deep="claude-sonnet-4",
        quick="claude-haiku-4",
        effort="medium",
    )
    assert k.caption() == "anthropic · claude-sonnet-4/claude-haiku-4 · e=medium"


def test_caption_both_customized_in_order() -> None:
    """Rounds before effort — keeps the line predictably parseable."""
    k = AnalysisConfigKey(
        provider="openai",
        deep="gpt-4o",
        quick="o4-mini",
        rounds=3,
        effort="high",
    )
    assert k.caption() == "openai · gpt-4o/o4-mini · r3 · e=high"


# ---------- telegraph_title() ----------


def test_telegraph_title_includes_ticker_and_caption() -> None:
    """The title is what Telegraph slugifies into the URL — putting the
    config in it makes the URL self-document which config produced the
    page, and prevents `NVDA Analysis` collision across configs."""
    k = AnalysisConfigKey(provider="openai", deep="gpt-4o", quick="o4-mini")
    assert k.telegraph_title("NVDA") == "NVDA Analysis · openai · gpt-4o/o4-mini"


def test_telegraph_title_extends_with_customization_suffixes() -> None:
    k = AnalysisConfigKey(
        provider="deepseek",
        deep="deepseek-v4-pro",
        quick="deepseek-v4-flash",
        rounds=2,
        effort="high",
    )
    assert k.telegraph_title("INTU") == (
        "INTU Analysis · deepseek · deepseek-v4-pro/deepseek-v4-flash · r2 · e=high"
    )


def test_telegraph_title_differs_across_configs_preventing_collision() -> None:
    """Two users on the same ticker but different configs should get
    different Telegraph titles so the slugifier produces distinct URLs
    (Telegraph appends `-2`/`-3` on title collision)."""
    a = AnalysisConfigKey(provider="openai", deep="gpt-4o", quick="o4-mini")
    b = AnalysisConfigKey(
        provider="deepseek", deep="deepseek-v4-pro", quick="deepseek-v4-flash"
    )
    assert a.telegraph_title("NVDA") != b.telegraph_title("NVDA")


# ---------- ordering ----------


SCENARIOS = [
    (
        "from_config default rounds + no effort",
        test_from_config_default_rounds_no_effort,
    ),
    ("from_config custom rounds", test_from_config_custom_rounds),
    ("from_config openai effort picked up", test_from_config_openai_effort_picks_up),
    (
        "from_config anthropic effort picked up",
        test_from_config_anthropic_effort_picks_up,
    ),
    (
        "from_config stale provider effort key wins first (first-truthy-wins)",
        test_from_config_stale_provider_effort_key_wins_first,
    ),
    (
        "from_config missing provider → unknown",
        test_from_config_missing_provider_falls_back,
    ),
    (
        "slug() default shape omits suffixes",
        test_slug_default_shape_omits_rounds_and_effort,
    ),
    ("slug() appends __r{n} when customized", test_slug_appends_rounds_when_nondefault),
    ("slug() appends __e{level} when set", test_slug_appends_effort_when_set),
    (
        "slug() appends both when both customized",
        test_slug_appends_both_when_both_customized,
    ),
    (
        "slug() sanitizes filesystem-unsafe chars",
        test_slug_sanitizes_filesystem_unsafe_chars,
    ),
    ("caption() default shape", test_caption_default_shape),
    (
        "caption() appends `· r{n}` when customized",
        test_caption_appends_rounds_when_nondefault,
    ),
    ("caption() appends `· e={level}`", test_caption_appends_effort_with_equals_prefix),
    ("caption() both customized in order", test_caption_both_customized_in_order),
    (
        "telegraph_title() includes caption",
        test_telegraph_title_includes_ticker_and_caption,
    ),
    (
        "telegraph_title() extends with suffixes",
        test_telegraph_title_extends_with_customization_suffixes,
    ),
    (
        "telegraph_title() differs across configs",
        test_telegraph_title_differs_across_configs_preventing_collision,
    ),
]


def main() -> int:
    failures = 0
    for label, fn in SCENARIOS:
        try:
            fn()
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
    sys.exit(main())
