"""TradingAgents adapter: per-user run + model catalog accessors."""

import logging
import threading
from datetime import date
from typing import Optional

from tg_bot.config import Config
from tg_bot.progress import (
    ProgressReporter,
    delegating_progress_callback,
    set_reporter,
)

logger = logging.getLogger(__name__)


# Cache keyed by (provider, deep_think_llm, quick_think_llm). TradingAgentsGraph
# init is expensive (langgraph compile, LLM clients, memory log) so we reuse one
# instance per LLM-config combo. Each entry carries a threading.Lock because
# the graph mutates self.ticker / self.curr_state during propagate(); two
# concurrent calls on the same instance would race.
_GraphCacheEntry = tuple["TradingAgentsGraph", threading.Lock]
_graph_cache: dict[tuple[str, str, str], _GraphCacheEntry] = {}
_cache_mutex = threading.Lock()


# Tradingagents is treated as an external dependency.  When it isn't
# installed, the rest of the bot still loads — handlers degrade gracefully.
TRADINGAGENTS_AVAILABLE = False
TradingAgentsGraph = None
DEFAULT_CONFIG = None
MODEL_OPTIONS: dict = {}

try:
    from tradingagents.graph.trading_graph import (
        TradingAgentsGraph as _TradingAgentsGraph,
    )
    from tradingagents.default_config import DEFAULT_CONFIG as _DEFAULT_CONFIG
    from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS as _MODEL_OPTIONS

    TradingAgentsGraph = _TradingAgentsGraph
    DEFAULT_CONFIG = _DEFAULT_CONFIG
    MODEL_OPTIONS = _MODEL_OPTIONS
    TRADINGAGENTS_AVAILABLE = True
except ImportError as e:
    logger.warning("TradingAgents not available: %s", e)


def get_model_options(provider: str, mode: str) -> list[tuple[str, str]]:
    """Return [(label, model_id), ...] for provider+mode, excluding 'custom' sentinels.

    Returns an empty list for providers not in the catalog (openrouter, azure).
    """
    options = MODEL_OPTIONS.get(provider, {}).get(mode, [])
    return [(label, value) for label, value in options if value != "custom"]


def has_model_catalog(provider: str) -> bool:
    return bool(MODEL_OPTIONS.get(provider))


def _resolve_models(
    user_id, user_config_storage, provider
) -> tuple[Optional[str], Optional[str]]:
    """Pick (deep, quick) for this user+provider; falls back to the catalog's
    first entry when unset. (None, None) for providers without a catalog."""
    deep = user_config_storage.get_llm_model(user_id, "deep")
    quick = user_config_storage.get_llm_model(user_id, "quick")
    if not deep:
        deep_options = get_model_options(provider, "deep")
        deep = deep_options[0][1] if deep_options else None
    if not quick:
        quick_options = get_model_options(provider, "quick")
        quick = quick_options[0][1] if quick_options else None
    return deep, quick


def run_trading_analysis(
    ticker: str,
    user_id,
    user_config_storage,
    reporter: Optional[ProgressReporter] = None,
):
    """Run TradingAgentsGraph for `ticker` using the user's stored LLM config.

    `reporter`, when supplied, receives per-step caption updates via the
    delegating LangChain callback baked into every cached graph. Reporter
    is bound to the current thread for the duration of propagate() so the
    singleton callback can dispatch to it.

    Returns (final_state, signal). (None, None) if tradingagents isn't
    available — caller should surface a helpful error.
    """
    if not TRADINGAGENTS_AVAILABLE:
        return None, None

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

    ta, lock = _get_or_create_graph(config)
    set_reporter(reporter)
    try:
        with lock:
            final_state, signal = ta.propagate(
                company_name=ticker, trade_date=date.today()
            )
    finally:
        set_reporter(None)
    # Don't log final_state itself — it's tens of KB of report text.
    logger.info("Analysis complete for %s — signal=%s", ticker, signal)
    logger.debug("Final state for %s: %s", ticker, final_state)
    return final_state, signal


def _get_or_create_graph(config: dict) -> _GraphCacheEntry:
    """Return a cached TradingAgentsGraph for this LLM-config combo, building
    one if it hasn't been seen before. The returned lock must be held while
    calling propagate() since the graph carries mutable per-call state.

    The delegating progress callback is attached to every fresh graph so
    per-run progress can be dispatched via a threadlocal target — see
    tg_bot.progress.
    """
    key = (
        config.get("llm_provider", ""),
        config.get("deep_think_llm", ""),
        config.get("quick_think_llm", ""),
    )
    with _cache_mutex:
        entry = _graph_cache.get(key)
        if entry is None:
            logger.info("Building TradingAgentsGraph for %s", key)
            entry = (
                TradingAgentsGraph(
                    debug=Config.TA_DEBUG,
                    config=config,
                    callbacks=[delegating_progress_callback],
                ),
                threading.Lock(),
            )
            _graph_cache[key] = entry
        return entry
