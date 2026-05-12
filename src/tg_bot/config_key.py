"""Single source of truth for the analysis config identity.

Three surfaces in the bot need a stable, mutually-consistent identifier
for *which LLM configuration produced an analysis*:

- **Cache slug** (filesystem dir name under `result_cache/<slug>/…`) —
  the cache key that disambiguates outputs from different configs.
- **Caption "via" line** (`via deepseek · deepseek-v4-pro/...`) — the
  human-readable trace rendered into the photo caption.
- **Telegraph title suffix** (`NVDA Analysis · deepseek · …`) — appended
  to the page title so the URL Telegraph generates self-documents which
  config produced the page (instead of all configs colliding on
  `NVDA-Analysis-05-10` and getting silent `-2`/`-3` suffixes).

Before this module existed each surface had its own generator
(`cache._slug` / `formatters.build_config_summary` / inline `f"{ticker}
Analysis"`) — they pulled from the same source data but formatted
independently, so adding a new knob (e.g. `effort`) meant remembering to
extend each one. Cross-cutting invariant #1 in CLAUDE.md is exactly this
"keep them aligned" rule; this dataclass enforces it structurally.

`AnalysisConfigKey` is the source of truth. Existing public helpers
(`cache._slug`, `formatters.build_config_summary`) delegate to it so
their callers don't have to migrate, but new code should construct an
`AnalysisConfigKey` and call the shape it needs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# Filesystem-safe regex re-imported from cache for the slug() method.
# Keeping the constant local avoids a load-time cycle.
import re


_SLUG_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class AnalysisConfigKey:
    """Frozen tuple of the knobs that influence analysis output.

    Two configs with identical `(provider, deep, quick, rounds, effort)`
    produce identical analyses; they share a cache slot, a caption
    "via" line, and a Telegraph URL. Anything outside this five-tuple
    (chat_id, user_id, ticker, date) is orthogonal and lives at the
    callsite, not in the key itself."""

    provider: str
    deep: str
    quick: str
    rounds: int = 1
    effort: Optional[str] = None

    @classmethod
    def from_config(cls, config: dict) -> "AnalysisConfigKey":
        """Pull the key knobs out of a resolved tradingagents config dict.

        `effort` lives under a provider-specific key
        (`openai_reasoning_effort` / `anthropic_effort` /
        `google_thinking_level`); we iterate `EFFORT_KEY_BY_PROVIDER`
        values rather than hardcoding so a new provider adding a thinking
        knob only needs to register its key in `analysis.py`.

        Late import: `analysis.py` already depends on this module
        transitively (through `formatters` and `cache`), and importing
        analysis at module load would create a cycle. The dict is
        module-level on analysis so the lookup is O(1) once the import
        is cached after first call."""
        from tg_bot.analysis import EFFORT_KEY_BY_PROVIDER

        return cls(
            provider=config.get("llm_provider", "unknown"),
            deep=config.get("deep_think_llm", "default"),
            quick=config.get("quick_think_llm", "default"),
            rounds=config.get("max_debate_rounds", 1),
            effort=next(
                (config[k] for k in EFFORT_KEY_BY_PROVIDER.values() if config.get(k)),
                None,
            ),
        )

    def slug(self) -> str:
        """Filesystem-safe cache directory slug.

        Shape: `provider__deep__quick[__r{n}][__e{level}]`. The
        `__r{n}` / `__e{level}` suffixes are appended only when those
        knobs differ from default, so users on default config keep their
        existing cache slot when new knobs ship (no orphaning).

        Provider / model strings can contain `/`, `:` etc. (e.g.
        `openai/gpt-4o`, `meta:llama3`) which aren't directory-safe; the
        regex squashes them to `_`."""
        base = _SLUG_SAFE.sub("_", f"{self.provider}__{self.deep}__{self.quick}")
        if self.rounds != 1:
            base += f"__r{self.rounds}"
        if self.effort:
            base += f"__e{self.effort}"
        return base

    def caption(self) -> str:
        """Caption fragment for the photo `via <caption>` line.

        Shape: `<provider> · <deep>/<quick> [· r{n}] [· e={level}]`.
        Middot separator + space matches the broader caption aesthetic
        (`<i>Generated …</i> · <i>via …</i>`). Rounds/effort are
        appended only when customized so the common-case line stays
        tight."""
        parts = [self.provider, f"{self.deep}/{self.quick}"]
        if self.rounds != 1:
            parts.append(f"r{self.rounds}")
        if self.effort:
            parts.append(f"e={self.effort}")
        return " · ".join(parts)

    def telegraph_title(self, ticker: str) -> str:
        """Title submitted to Telegraph's `create_page` / `edit_page`.

        Shape: `<TICKER> Analysis · <provider> · <deep>/<quick> [· r{n}]
        [· e={level}]`. The config suffix matches `caption()` so the
        URL Telegraph slugifies (`https://telegra.ph/NVDA-Analysis-…`)
        self-documents which config produced the analysis instead of
        all configs colliding on `NVDA-Analysis-05-10` and Telegraph
        appending arbitrary `-2`/`-3` suffixes.

        Telegraph's title slugifier collapses non-word chars and
        truncates around 256 chars; provider + two model names + an
        optional `r{n}`/`e=` is well under that for every provider we
        support."""
        return f"{ticker} Analysis · {self.caption()}"
