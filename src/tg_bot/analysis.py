"""TradingAgents adapter: per-user run + model catalog accessors."""

import contextlib
import logging
import os
import queue
import threading
from collections import OrderedDict
from datetime import date
from typing import Optional

from telegram.helpers import escape_markdown

from tg_bot.config import Config
from tg_bot.progress import (
    ProgressReporter,
    delegating_progress_callback,
    set_reporter,
)

logger = logging.getLogger(__name__)


# Maps the provider name (as picked via /config) → the env var that holds
# its API key. Used by `check_llm_configured` to give users a targeted
# error before the LLM call 401s with a generic message. Keep in sync
# with `.env.example` and the catalog in `MODEL_OPTIONS` below.
PROVIDER_ENV_KEYS: dict[str, Optional[str]] = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "glm": "ZHIPUAI_API_KEY",
    "ollama": None,  # local; no key needed
    # openrouter has no MODEL_OPTIONS catalog yet so the /config picker
    # still short-circuits (run falls back to DEFAULT_CONFIG models, which
    # openrouter happens to accept), but tradingagents' OpenAIClient
    # natively supports the provider — reads OPENROUTER_API_KEY + routes
    # to https://openrouter.ai/api/v1. Listing the env key here makes the
    # precheck surface "openrouter picked but OPENROUTER_API_KEY not set"
    # instead of the cryptic downstream "OPENAI_API_KEY missing" error
    # from the openai SDK when the env isn't loaded.
    "openrouter": "OPENROUTER_API_KEY",
    # azure: intentionally absent — no native tradingagents support yet.
}


def check_llm_configured(user_id, user_config_storage) -> Optional[str]:
    """Return None if the user is ready to run analyses, or a short reason
    string explaining what to fix. Callers wrap the reason in their own
    user-facing rendering (full message vs `/status` line)."""
    provider = user_config_storage.get_llm_provider(user_id)
    if not provider:
        return "no provider — tap /config"
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
            "Tap /config to pick a provider \\+ deep/quick models, then try again\\."
        )
    # Mode B: provider picked, but matching env var missing. Format is
    # "deepseek picked but DEEPSEEK_API_KEY not set in .env".
    return (
        f"⚠️ *LLM key missing*\\.\n\n"
        f"`{escape_markdown(reason, version=2)}`\n\n"
        "Add the key to your `\\.env` and restart the bot \\(`docker\\-compose up \\-d`\\)\\."
    )


# Pool of TradingAgentsGraph instances keyed by (provider, deep, quick).
# TradingAgentsGraph init is expensive (langgraph compile, LLM clients, memory
# log), and the graph mutates self.ticker/self.curr_state during propagate()
# — two concurrent calls on the same instance would race.
#
# A pool gives us both: parallel runs (each gets its own instance) AND
# caching across runs (instances are returned to the pool on completion).
# Per-key pool size is `Config.MAX_CONCURRENT_ANALYSES` — the asyncio
# semaphore prevents over-subscription, so the pool's blocking branch is
# unreachable. Key-level LRU at GRAPH_CACHE_MAX evicts the oldest pool
# (and all its instances) when too many distinct LLM-config keys exist.
GRAPH_CACHE_MAX = 4


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


_graph_pool: "OrderedDict[tuple[str, str, str, int, Optional[str]], GraphPool]" = (
    OrderedDict()
)
_pool_mutex = threading.Lock()


def pool_stats() -> tuple[int, int]:
    """Snapshot of (num_keys, total_instances) for the /status command."""
    with _pool_mutex:
        return (len(_graph_pool), sum(p.size for p in _graph_pool.values()))


# Tradingagents is treated as an external dependency.  When it isn't
# installed, the rest of the bot still loads — handlers degrade gracefully.
TRADINGAGENTS_AVAILABLE = False
TradingAgentsGraph = None
DEFAULT_CONFIG = None
MODEL_OPTIONS: dict = {}


# Curated starter catalog for openrouter. tradingagents' upstream
# `MODEL_OPTIONS` doesn't yet ship openrouter; we patch it in at import
# time below so /config can let users actually pick a model instead of
# silently falling back to DEFAULT_CONFIG (the openai catalog — works
# today against openrouter's permissive routing, but accidental and
# fragile). Mix of free + paid + reasoning so the first entry (default
# fallback) is a free model good for testing.
_OPENROUTER_MODELS: dict[str, list[tuple[str, str]]] = {
    "quick": [
        (
            "Llama 3.3 70B (free, rate-limited)",
            "meta-llama/llama-3.3-70b-instruct:free",
        ),
        ("GPT-4o Mini — Fast, cheap", "openai/gpt-4o-mini"),
        ("Claude 3.5 Haiku — Fast", "anthropic/claude-3.5-haiku"),
        ("DeepSeek v3 — Cheap, capable", "deepseek/deepseek-chat"),
    ],
    "deep": [
        (
            "Llama 3.3 70B (free, rate-limited)",
            "meta-llama/llama-3.3-70b-instruct:free",
        ),
        ("Claude Sonnet 4.5 — Strong reasoning", "anthropic/claude-sonnet-4.5"),
        ("GPT-4o — Reliable, multimodal", "openai/gpt-4o"),
        ("DeepSeek R1 — Cheap reasoning", "deepseek/deepseek-r1"),
    ],
}

_OPENROUTER_MODEL_MIGRATIONS = {
    "anthropic/claude-3.5-sonnet": "anthropic/claude-sonnet-4.5",
}


try:
    from tradingagents.graph.trading_graph import (
        TradingAgentsGraph as _TradingAgentsGraph,
    )
    from tradingagents.default_config import DEFAULT_CONFIG as _DEFAULT_CONFIG
    from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS as _MODEL_OPTIONS

    TradingAgentsGraph = _TradingAgentsGraph
    DEFAULT_CONFIG = _DEFAULT_CONFIG
    # Defensive copy + augment with openrouter so a future upstream that
    # ships its own openrouter entry takes precedence (we only fill the
    # gap when the key is absent).
    MODEL_OPTIONS = dict(_MODEL_OPTIONS)
    if "openrouter" not in MODEL_OPTIONS:
        MODEL_OPTIONS["openrouter"] = _OPENROUTER_MODELS
    TRADINGAGENTS_AVAILABLE = True
except ImportError as e:
    logger.warning("TradingAgents not available: %s", e)


def get_model_options(provider: str, mode: str) -> list[tuple[str, str]]:
    """Return [(label, model_id), ...] for provider+mode, excluding 'custom' sentinels.

    Returns an empty list for providers not in the catalog (azure only —
    openrouter is patched in above with a curated starter list).
    """
    options = MODEL_OPTIONS.get(provider, {}).get(mode, [])
    return [(label, value) for label, value in options if value != "custom"]


def has_model_catalog(provider: str) -> bool:
    return bool(MODEL_OPTIONS.get(provider))


def _resolve_models(
    user_id, user_config_storage, provider
) -> tuple[Optional[str], Optional[str]]:
    """Pick (deep, quick) for this user+provider; falls back to the catalog's
    first entry when unset. (None, None) for providers without a catalog.

    Also rewrites known-stale OpenRouter model IDs so existing saved user
    configs survive provider-side slug retirements.
    """
    deep = user_config_storage.get_llm_model(user_id, "deep")
    quick = user_config_storage.get_llm_model(user_id, "quick")
    if provider == "openrouter":
        deep = _OPENROUTER_MODEL_MIGRATIONS.get(deep, deep)
        quick = _OPENROUTER_MODEL_MIGRATIONS.get(quick, quick)
    if not deep:
        deep_options = get_model_options(provider, "deep")
        deep = deep_options[0][1] if deep_options else None
    if not quick:
        quick_options = get_model_options(provider, "quick")
        quick = quick_options[0][1] if quick_options else None
    return deep, quick


# Per-provider key for the "thinking effort" knob. Vocabulary
# (low/medium/high) is shared across providers but the config-dict key
# differs. Providers absent from this map have no effort knob — we
# silently skip applying it.
#
# Single source of truth for which provider stores effort where.
# `AnalysisConfigKey.from_config` iterates `.values()` so a new provider
# adding a thinking knob only needs to register its key here.
EFFORT_KEY_BY_PROVIDER = {
    "openai": "openai_reasoning_effort",
    "anthropic": "anthropic_effort",
    "google": "google_thinking_level",
}


def build_user_config(user_id, user_config_storage) -> dict:
    """Resolve the tradingagents config dict for this user — same logic
    `run_trading_analysis` uses, exposed so cache-key construction agrees
    with the actual run. Returns DEFAULT_CONFIG if the user hasn't picked
    a provider via /config (matches the run's silent fallback)."""
    user_provider = user_config_storage.get_llm_provider(user_id)
    config = DEFAULT_CONFIG.copy()
    if user_provider:
        config["llm_provider"] = user_provider
        deep_model, quick_model = _resolve_models(
            user_id, user_config_storage, user_provider
        )
        if deep_model:
            config["deep_think_llm"] = deep_model
        if quick_model:
            config["quick_think_llm"] = quick_model
        if not (deep_model and quick_model):
            logger.warning(
                "No catalog models for provider %r; using DEFAULT_CONFIG models "
                "(%s / %s) — the provider's API may reject these.",
                user_provider,
                config["deep_think_llm"],
                config["quick_think_llm"],
            )
    # Quality knobs — applied independently of provider since the user's
    # last picked rounds/effort survive provider switches. Rounds always
    # gets written (graph-level param). Effort only applied when the
    # current provider has a corresponding key.
    config["max_debate_rounds"] = user_config_storage.get_max_debate_rounds(user_id)
    effort = user_config_storage.get_effort_level(user_id)
    if effort and user_provider:
        provider_key = EFFORT_KEY_BY_PROVIDER.get(user_provider)
        if provider_key:
            config[provider_key] = effort
    return config


def run_trading_analysis(
    ticker: str,
    user_id,
    user_config_storage,
    reporter: Optional[ProgressReporter] = None,
):
    """Run TradingAgentsGraph for `ticker` using the user's stored LLM config.

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

    config = build_user_config(user_id, user_config_storage)

    pool = _get_or_create_pool(config)
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
    """Return the pool of TradingAgentsGraph instances for this LLM-config
    combo, creating it if not seen yet. Pool builder closes over `config`
    so each newly-built instance binds the right provider+models.

    Pool key includes `max_debate_rounds` and the active effort value
    because tradingagents bakes both into the graph at construction time
    (`max_debate_rounds` → `ConditionalLogic.__init__`; effort → LLM
    client kwargs). Without this, a User-A run with rounds=1 would be
    reused by a User-B request with rounds=3 silently — User B's "Thorough"
    selection produces a Fast-mode graph with no error surfaced.

    LRU-evicts the oldest key when GRAPH_CACHE_MAX is exceeded — that drops
    all instances for that key (each pinned an LLM client + ChromaDB).
    """
    # `AnalysisConfigKey` is the single source of truth for the
    # (provider, deep, quick, rounds, effort) tuple — same dataclass the
    # cache uses for its slug and the formatters use for the caption.
    # Using it here keeps invariant #1 enforced structurally: any future
    # knob added to the dataclass flows into the pool key automatically,
    # so there's no manual sync risk.
    from tg_bot.config_key import AnalysisConfigKey

    key = AnalysisConfigKey.from_config(config)

    def _builder():
        logger.info("Building TradingAgentsGraph for %s", key.slug())
        return TradingAgentsGraph(
            debug=Config.TA_DEBUG,
            config=config,
            callbacks=[delegating_progress_callback],
        )

    with _pool_mutex:
        pool = _graph_pool.get(key)
        if pool is None:
            pool = GraphPool(max_size=Config.MAX_CONCURRENT_ANALYSES, builder=_builder)
            _graph_pool[key] = pool
            while len(_graph_pool) > GRAPH_CACHE_MAX:
                evicted_key, _ = _graph_pool.popitem(last=False)
                logger.info("Evicting graph pool for %s (LRU)", evicted_key.slug())
        else:
            _graph_pool.move_to_end(key)
        return pool
