"""TradingAgents adapter: per-user run + model catalog accessors."""

import contextlib
import logging
import os
import queue
import threading
from datetime import date

from telegram.helpers import escape_markdown

from tg_bot.config import Config
from tg_bot.pipeline.progress import (
    ProgressReporter,
    delegating_progress_callback,
    set_reporter,
)

logger = logging.getLogger(__name__)


# Maps the provider name (set via `TRADINGAGENTS_LLM_PROVIDER` in `.env`)
# → the env var that holds its API key. Used by `check_llm_configured`
# to give users a targeted error before the LLM call 401s with a generic
# message. Keep in sync with `.env.example`.
# Per-provider key for the "thinking effort" knob. Vocabulary
# (low/medium/high) is shared across providers but the config-dict key
# differs. Providers absent from this map have no effort knob — we
# silently skip applying it.
#
# Single source of truth for which provider stores effort where.
# `AnalysisConfigKey.from_config` iterates `.values()` so a new provider
# adding a thinking knob only needs to register its key here.
# Defined before `_LOCAL_EFFORT_OVERRIDES` (which derives env-var names
# from these keys) — keeps the dict the single source for both surfaces.
EFFORT_KEY_BY_PROVIDER = {
    "openai": "openai_reasoning_effort",
    "anthropic": "anthropic_effort",
    "google": "google_thinking_level",
}


# Mapping from env var name → DEFAULT_CONFIG key for the per-provider
# reasoning-effort knob. Upstream tradingagents' `_ENV_OVERRIDES` covers
# provider/models/debate-rounds but NOT these provider-specific keys, so
# we overlay them locally to keep `.env` as the single source of truth.
# Derived from `EFFORT_KEY_BY_PROVIDER` so a new provider added there
# automatically gets env-var overlay support — no second edit needed.
_LOCAL_EFFORT_OVERRIDES = {
    f"TRADINGAGENTS_{config_key.upper()}": config_key
    for config_key in EFFORT_KEY_BY_PROVIDER.values()
}


def _apply_local_effort_overlay(config: dict, env: "dict | None" = None) -> None:
    """Overlay our local `TRADINGAGENTS_*_EFFORT` env vars onto `config`
    in place. Mirrors upstream's `_apply_env_overrides` pattern. Only
    writes when the env var is non-empty — leaves the upstream default
    in place otherwise (which is None for all three effort keys today).

    `env` arg is for testability (default: `os.environ`). Callers in
    production pass nothing; tests pass a controlled mapping."""
    src = env if env is not None else os.environ
    for env_var, config_key in _LOCAL_EFFORT_OVERRIDES.items():
        raw = src.get(env_var)
        if raw:
            config[config_key] = raw


def build_config() -> dict:
    """Return a fresh copy of the active tradingagents config dict.

    Source of truth: tradingagents' `DEFAULT_CONFIG` (already overlaid
    by upstream's `_ENV_OVERRIDES` at lib import time for provider /
    models / debate-rounds / etc.) + our local effort overlay applied
    immediately after the import below for the per-provider effort keys
    upstream doesn't expose. `.env` is the single source of truth — there
    is no per-user override path.

    Both overlays run ONCE at module-import time, not on every call.
    Changing env vars at run time (e.g. a test patching `os.environ`)
    will NOT be reflected — production env vars don't change post-start,
    and tests that need to round-trip env values should call
    `_apply_local_effort_overlay(config, env=...)` directly with an
    explicit env dict.

    Returns a **shallow** copy: top-level scalars are safe to mutate,
    but if `DEFAULT_CONFIG` ever gains a nested mutable (list / dict),
    mutating it through the returned dict would leak back into the
    global. Today every consumed key is a scalar — keep it that way.

    Returns an empty dict when tradingagents isn't available (caller
    paths short-circuit via `TRADINGAGENTS_AVAILABLE` before reaching here)."""
    return dict(DEFAULT_CONFIG) if DEFAULT_CONFIG else {}


def get_current_config_key():
    """Return the `AnalysisConfigKey` for the active env-derived config.

    Computed each call rather than cached — `build_config()` + dataclass
    init is microseconds, and avoiding the cache means tests that
    monkeypatch env vars don't need a reset hook. Production env vars
    don't change at runtime (restart required), so the result is
    effectively constant.

    Returns None when tradingagents isn't available."""
    if DEFAULT_CONFIG is None:
        return None
    from tg_bot.pipeline.config_key import AnalysisConfigKey

    return AnalysisConfigKey.from_config(build_config())


PROVIDER_ENV_KEYS: dict[str, str | None] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    # qwen-cn / glm-cn / minimax-cn use distinct CN-suffixed env vars upstream
    # — not shared with the non-CN siblings. Verified via tradingagents'
    # `PROVIDER_API_KEY_ENV`; pinned by `test_provider_env_keys_match_upstream`.
    "qwen-cn": "DASHSCOPE_CN_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "glm-cn": "ZHIPU_CN_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "ollama": None,  # local; no key needed (point at remote via OLLAMA_BASE_URL)
    # openrouter routes via tradingagents' OpenAIClient to
    # https://openrouter.ai/api/v1; listing it here makes `check_llm_configured`
    # surface "OPENROUTER_API_KEY not set" instead of the cryptic downstream
    # "OPENAI_API_KEY missing" from the openai SDK when the env isn't loaded.
    "openrouter": "OPENROUTER_API_KEY",
    # azure: intentionally absent — no native tradingagents support yet.
}


def check_llm_configured(config: dict | None = None) -> str | None:
    """Return None if the bot is ready to run analyses, or a short reason
    string explaining what to fix. Callers wrap the reason in their own
    user-facing rendering (full message vs `/status` line).

    `config` defaults to `build_config()` — pass an explicit dict only in
    tests that need to check a specific provider/env combination."""
    if config is None:
        config = build_config()
    provider = config.get("llm_provider")
    if not provider:
        return "no provider — set TRADINGAGENTS_LLM_PROVIDER in .env"
    env_var = PROVIDER_ENV_KEYS.get(provider)
    if env_var and not os.environ.get(env_var):
        return f"{provider} picked but {env_var} not set in .env"
    return None


def llm_setup_error_message(reason: str) -> str:
    """Render the short reason from `check_llm_configured` as a friendly
    MarkdownV2 message for the user, including the next-step hint.

    Lives next to `check_llm_configured` so both halves of the LLM-setup
    precheck (the check + the user-facing render) stay in one module —
    previously this lived in `handlers/callbacks.py`, which forced
    `handlers/analysis_runner.run_user_digest` to late-import it
    (cycle: callbacks → analysis_runner → callbacks). Moving it here
    breaks the cycle so both call sites can import at the top level.
    """
    if reason.startswith("no provider"):
        return (
            "⚠️ *No LLM provider configured*\\.\n\n"
            "Set `TRADINGAGENTS_LLM_PROVIDER` in your `\\.env` "
            "\\(see `docs/CONFIGURATION\\.md`\\) and restart the bot\\."
        )
    # Mode B: provider picked, but matching env var missing. Format is
    # "deepseek picked but DEEPSEEK_API_KEY not set in .env".
    return (
        f"⚠️ *LLM key missing*\\.\n\n"
        f"`{escape_markdown(reason, version=2)}`\n\n"
        "Add the key to your `\\.env` and restart the bot \\(`docker\\-compose up \\-d`\\)\\."
    )


# Pool of TradingAgentsGraph instances bound to the active env config.
# TradingAgentsGraph init is expensive (langgraph compile, LLM clients, memory
# log), and the graph mutates self.ticker/self.curr_state during propagate()
# — two concurrent calls on the same instance would race.
#
# A pool gives us both: parallel runs (each gets its own instance) AND
# caching across runs (instances are returned to the pool on completion).
# Pool size is `Config.MAX_CONCURRENT_ANALYSES` — the asyncio semaphore
# upstream prevents over-subscription, so the pool's blocking branch is
# unreachable.
#
# Before the env-config refactor this was a dict-of-pools keyed by
# `AnalysisConfigKey` with an LRU + `GRAPH_CACHE_MAX` floor (so per-user
# `/config` switches didn't thrash ~500ms rebuilds). With `.env` as the
# single source of truth, only one config exists per process lifetime, so
# the dict-of-pools degenerated to a single-key dict — collapsed here to
# a single lazy `GraphPool` variable. Re-introducing multi-config support
# would just re-add the dict wrapper; the `GraphPool` class itself stays
# unchanged.


class GraphPool:
    """Thread-safe pool of identical TradingAgentsGraph instances.

    `acquire()` is a context manager — yields an instance, returns it on
    exit (or after exception). Builds on demand up to `max_size`, then
    blocks until one is returned.
    """

    def __init__(self, max_size: int, builder) -> None:
        self._q: queue.Queue = queue.Queue()
        self._builder = builder
        self._size = 0
        self._max = max_size
        self._mutex = threading.Lock()

    @property
    def size(self) -> int:
        """Current number of built instances (in pool + in flight)."""
        return self._size

    @contextlib.contextmanager
    def acquire(self):
        graph = None
        try:
            graph = self._q.get_nowait()
            logger.debug(
                "pool.acquire: reused cached instance (size=%d, free=%d)",
                self._size,
                self._q.qsize(),
            )
        except queue.Empty:
            pass

        if graph is None:
            should_build = False
            with self._mutex:
                if self._size < self._max:
                    self._size += 1
                    should_build = True
            if should_build:
                # Build OUTSIDE the mutex — graph init is slow and other
                # concurrent acquires shouldn't be blocked on it.
                logger.debug(
                    "pool.acquire: no free instance, building (now size=%d/%d)",
                    self._size,
                    self._max,
                )
                try:
                    graph = self._builder()
                except Exception:
                    with self._mutex:
                        self._size -= 1
                    raise
            else:
                # Pool already at max; block until one is returned. Per design
                # the asyncio semaphore upstream caps concurrency to the same
                # number, so this branch is unreachable — if it fires, the
                # invariant has broken and runs are silently serializing
                # without checking the cancel flag.
                logger.warning(
                    "pool.acquire: at max=%d, blocking on queue.get — "
                    "semaphore/pool sizing invariant broken",
                    self._max,
                )
                graph = self._q.get()
                logger.debug("pool.acquire: woke from blocking get")

        try:
            yield graph
        finally:
            self._q.put(graph)
            logger.debug(
                "pool.release: returned to pool (size=%d, free=%d)",
                self._size,
                self._q.qsize(),
            )


_graph_pool: "GraphPool | None" = None
_pool_mutex = threading.Lock()


def pool_stats() -> tuple[int, int]:
    """Snapshot of (num_pools, total_instances) for the /status command.

    Returns `(0, 0)` before the lazy init, then `(1, N)` where N is the
    number of `TradingAgentsGraph` instances the single pool has built so
    far (bounded by `Config.MAX_CONCURRENT_ANALYSES`)."""
    with _pool_mutex:
        if _graph_pool is None:
            return (0, 0)
        return (1, _graph_pool.size)


# Tradingagents is treated as an external dependency.  When it isn't
# installed, the rest of the bot still loads — handlers degrade gracefully.
TRADINGAGENTS_AVAILABLE = False
TradingAgentsGraph = None
DEFAULT_CONFIG = None


try:
    from tradingagents.graph.trading_graph import (
        TradingAgentsGraph as _TradingAgentsGraph,
    )
    from tradingagents.default_config import DEFAULT_CONFIG as _DEFAULT_CONFIG

    TradingAgentsGraph = _TradingAgentsGraph
    DEFAULT_CONFIG = _DEFAULT_CONFIG
    # Apply our local effort env-var overlay onto DEFAULT_CONFIG. Upstream's
    # own `_apply_env_overrides` has already run inside tradingagents at its
    # module load; this closes the gap for the per-provider effort keys
    # upstream's `_ENV_OVERRIDES` list doesn't cover. Once applied, every
    # consumer of `DEFAULT_CONFIG` (build_config, AnalysisConfigKey.from_config)
    # sees the env-derived effort.
    #
    # **Run-order matters**: we run AFTER upstream, so if upstream ever adds
    # `TRADINGAGENTS_OPENAI_REASONING_EFFORT` / `TRADINGAGENTS_ANTHROPIC_EFFORT` /
    # `TRADINGAGENTS_GOOGLE_THINKING_LEVEL` to its own `_ENV_OVERRIDES`, OUR
    # write wins (last writer). The effective behavior is "this overlay is
    # the source of truth for the three keys in `EFFORT_KEY_BY_PROVIDER`."
    # If upstream coverage lands and we want to defer to it, drop the
    # provider's entry from `EFFORT_KEY_BY_PROVIDER` instead of hoisting
    # this call before the import — the test in `test_pipeline_analysis.py`
    # pins the derivation contract.
    _apply_local_effort_overlay(DEFAULT_CONFIG)
    TRADINGAGENTS_AVAILABLE = True
except ImportError as e:
    logger.warning("TradingAgents not available: %s", e)


def run_trading_analysis(
    ticker: str,
    reporter: ProgressReporter | None = None,
):
    """Run TradingAgentsGraph for `ticker` using the `.env`-derived config.

    `reporter`, when supplied, receives per-step caption updates via the
    delegating LangChain callback baked into every graph. The reporter is
    bound to the current thread for the duration of propagate() so the
    singleton callback can dispatch to it.

    Graph instances come from a per-(provider, deep, quick) pool so single-
    tap and parallel queue runs both benefit from caching. Pool grows on
    demand up to `Config.MAX_CONCURRENT_ANALYSES`; the asyncio semaphore in
    the handler bounds how many runs reach the pool at once.

    Returns (final_state, signal). (None, None) if tradingagents isn't
    available — caller should surface a helpful error.
    """
    if not TRADINGAGENTS_AVAILABLE:
        return None, None

    pool = _get_or_create_pool(build_config())
    set_reporter(reporter)
    logger.debug("[%s] propagate START", ticker)
    try:
        with pool.acquire() as ta:
            final_state, signal = ta.propagate(
                company_name=ticker, trade_date=date.today()
            )
    finally:
        set_reporter(None)
    # Don't log final_state itself — it's tens of KB of report text.
    logger.info("Analysis complete for %s — signal=%s", ticker, signal)
    logger.debug("Final state for %s: %s", ticker, final_state)
    return final_state, signal


def _get_or_create_pool(config: dict) -> GraphPool:
    """Return the single process-wide GraphPool, lazily building on first
    call. Pool builder closes over `config` so newly-built instances bind
    the right `.env`-derived provider/models/rounds/effort.

    Renamed from the legacy dict-of-pools function but kept the
    `(config)` signature so the few callers + test fixtures don't need
    parameter changes. Subsequent calls IGNORE the `config` arg — the
    pool is bound to the config that built it (and after a restart the
    new pool picks up the new `.env`)."""
    global _graph_pool
    if _graph_pool is not None:
        return _graph_pool

    def _builder():
        from tg_bot.pipeline.config_key import AnalysisConfigKey

        slug = AnalysisConfigKey.from_config(config).slug()
        logger.info("Building TradingAgentsGraph for %s", slug)
        return TradingAgentsGraph(
            debug=Config.TA_DEBUG,
            config=config,
            callbacks=[delegating_progress_callback],
        )

    # Double-checked locking — cheap fast-path above, mutex for the
    # single init below. Workers don't race-build.
    with _pool_mutex:
        if _graph_pool is None:
            _graph_pool = GraphPool(
                max_size=Config.MAX_CONCURRENT_ANALYSES,
                builder=_builder,
            )
        return _graph_pool
