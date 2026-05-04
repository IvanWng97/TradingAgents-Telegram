"""TradingAgents adapter: per-user run + model catalog accessors."""

import contextlib
import logging
import queue
import threading
from collections import OrderedDict
from datetime import date
from typing import Optional

from tg_bot.config import Config
from tg_bot.progress import (
    ProgressReporter,
    delegating_progress_callback,
    set_reporter,
)

logger = logging.getLogger(__name__)


# Pool of TradingAgentsGraph instances keyed by (provider, deep, quick).
# TradingAgentsGraph init is expensive (langgraph compile, LLM clients, memory
# log), and the graph mutates self.ticker/self.curr_state during propagate()
# — two concurrent calls on the same instance would race.
#
# A pool gives us both: parallel runs (each gets its own instance) AND
# caching across runs (instances are returned to the pool on completion).
# Pool grows lazily up to GRAPH_POOL_MAX_PER_KEY; further parallel runs
# block on queue.get() until an instance is returned.
#
# Key-level LRU at GRAPH_CACHE_MAX — when too many keys are tracked, the
# oldest pool (and all its instances) is evicted.
GRAPH_POOL_MAX_PER_KEY = 5
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
                try:
                    graph = self._builder()
                except Exception:
                    with self._mutex:
                        self._size -= 1
                    raise
            else:
                # Pool already at max; block until one is returned.
                graph = self._q.get()

        try:
            yield graph
        finally:
            self._q.put(graph)


_graph_pool: "OrderedDict[tuple[str, str, str], GraphPool]" = OrderedDict()
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
    delegating LangChain callback baked into every graph. The reporter is
    bound to the current thread for the duration of propagate() so the
    singleton callback can dispatch to it.

    Graph instances come from a per-(provider, deep, quick) pool so single-
    tap and parallel queue runs both benefit from caching. Pool grows on
    demand up to GRAPH_POOL_MAX_PER_KEY; further parallel runs queue until
    an instance is returned. No `dedicated` flag — pool handles both cases.

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

    pool = _get_or_create_pool(config)
    set_reporter(reporter)
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

    LRU-evicts the oldest key when GRAPH_CACHE_MAX is exceeded — that drops
    all instances for that key (each pinned an LLM client + ChromaDB).
    """
    key = (
        config.get("llm_provider", ""),
        config.get("deep_think_llm", ""),
        config.get("quick_think_llm", ""),
    )

    def _builder():
        logger.info("Building TradingAgentsGraph for %s", key)
        return TradingAgentsGraph(
            debug=Config.TA_DEBUG,
            config=config,
            callbacks=[delegating_progress_callback],
        )

    with _pool_mutex:
        pool = _graph_pool.get(key)
        if pool is None:
            pool = GraphPool(max_size=GRAPH_POOL_MAX_PER_KEY, builder=_builder)
            _graph_pool[key] = pool
            while len(_graph_pool) > GRAPH_CACHE_MAX:
                evicted_key, _ = _graph_pool.popitem(last=False)
                logger.info("Evicting graph pool for %s (LRU)", evicted_key)
        else:
            _graph_pool.move_to_end(key)
        return pool
