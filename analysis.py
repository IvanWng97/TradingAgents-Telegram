"""
TradingAgentsGraph analysis functions.
"""
import logging
from datetime import date

logger = logging.getLogger(__name__)

# Import TradingAgentsGraph - tradingagents is treated as an external module
TRADINGAGENTS_AVAILABLE = False
TradingAgentsGraph = None
DEFAULT_CONFIG = None

try:
    from tradingagents.graph.trading_graph import TradingAgentsGraph as _TradingAgentsGraph
    from tradingagents.default_config import DEFAULT_CONFIG as _DEFAULT_CONFIG
    from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS as _MODEL_OPTIONS
    TradingAgentsGraph = _TradingAgentsGraph
    DEFAULT_CONFIG = _DEFAULT_CONFIG
    MODEL_OPTIONS = _MODEL_OPTIONS
    TRADINGAGENTS_AVAILABLE = True
except ImportError as e:
    logger.warning(f"TradingAgents not available. Error: {e}")
    MODEL_OPTIONS = {}


def get_model_options(provider: str, mode: str):
    """Return [(label, model_id), ...] for provider+mode, excluding 'custom' sentinels.

    Returns an empty list for providers not in the catalog (openrouter, azure).
    """
    options = MODEL_OPTIONS.get(provider, {}).get(mode, [])
    return [(label, value) for label, value in options if value != "custom"]


def has_model_catalog(provider: str) -> bool:
    """Whether the provider has a built-in model catalog."""
    return bool(MODEL_OPTIONS.get(provider))


def _resolve_models(user_id, user_config_storage, provider):
    """Return (deep_model, quick_model) for this user+provider, falling back to
    the catalog's first entry if the user hasn't picked yet. (None, None) for
    providers without a catalog (caller should leave DEFAULT_CONFIG models)."""
    deep = user_config_storage.get_llm_model(user_id, "deep")
    quick = user_config_storage.get_llm_model(user_id, "quick")
    if not deep:
        deep_options = get_model_options(provider, "deep")
        deep = deep_options[0][1] if deep_options else None
    if not quick:
        quick_options = get_model_options(provider, "quick")
        quick = quick_options[0][1] if quick_options else None
    return deep, quick


def run_trading_analysis(ticker: str, user_id: str, user_config_storage):
    """Run TradingAgentsGraph analysis for a ticker.

    Args:
        ticker: Stock ticker symbol
        user_id: User ID for getting user config
        user_config_storage: UserConfigStorage instance

    Returns:
        Tuple of (final_state, signal) or (None, None) if not available
    """
    if not TRADINGAGENTS_AVAILABLE:
        return None, None

    # Get user's LLM provider config, fallback to default
    user_provider = user_config_storage.get_llm_provider(user_id)
    config = DEFAULT_CONFIG.copy()
    if user_provider:
        config["llm_provider"] = user_provider
        deep_model, quick_model = _resolve_models(user_id, user_config_storage, user_provider)
        if deep_model:
            config["deep_think_llm"] = deep_model
        if quick_model:
            config["quick_think_llm"] = quick_model
        if not (deep_model and quick_model):
            logger.warning(
                "No catalog models for provider %r; using DEFAULT_CONFIG models "
                "(%s / %s) — the provider's API may reject these.",
                user_provider, config["deep_think_llm"], config["quick_think_llm"],
            )

    ta = TradingAgentsGraph(debug=True, config=config)
    final_state, signal = ta.propagate(company_name=ticker, trade_date=date.today())

    # Log the final_state for debugging
    logger.info(f"Final state for {ticker}: {final_state}")

    return final_state, signal
