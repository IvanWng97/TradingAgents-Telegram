"""LLM pricing reference data + dollar-cost estimation.

Split out of `pipeline/analysis.py` per architect review of PR #76: pricing
data is observability-cold-path reference material, not part of the LLM
execution wiring. Keeping it in `analysis.py` mixed two concerns — the
LLM config check + provider env keys (hot path) and a manual price
snapshot (cold path) — that drift on different cycles.

The price table is a **manual point-in-time snapshot**, not an authoritative
source — provider pricing changes constantly upstream and we don't track
those updates automatically. The fallback in `estimate_token_cost_usd` is
to return None when no entry exists, so a stale or missing entry degrades
to tokens-only rendering in `/status` rather than reporting wrong dollar
figures. The bot also surfaces raw input/output token totals so the
operator can always do the math themselves.
"""

from __future__ import annotations


# USD per 1M tokens for (provider, model) pairs. Pairs not in this dict
# fall through to tokens-only rendering. Keys are lower-cased on lookup,
# so spelling case in `.env` doesn't matter. Pricing as of late 2025
# (input → output, USD per 1M tokens):
LLM_PRICE_USD_PER_M: dict[tuple[str, str], tuple[float, float]] = {
    # OpenAI
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("openai", "gpt-4.1"): (3.00, 12.00),
    ("openai", "gpt-4.1-mini"): (0.40, 1.60),
    ("openai", "o1"): (15.00, 60.00),
    ("openai", "o1-mini"): (1.10, 4.40),
    ("openai", "o3-mini"): (1.10, 4.40),
    # Anthropic
    ("anthropic", "claude-opus-4-5"): (15.00, 75.00),
    ("anthropic", "claude-sonnet-4-5"): (3.00, 15.00),
    ("anthropic", "claude-haiku-4-5"): (1.00, 5.00),
    # Google
    ("google", "gemini-2.5-pro"): (1.25, 10.00),
    ("google", "gemini-2.5-flash"): (0.30, 2.50),
    # DeepSeek (per official pricing, cache-miss rate)
    ("deepseek", "deepseek-chat"): (0.27, 1.10),
    ("deepseek", "deepseek-reasoner"): (0.55, 2.19),
}


def estimate_token_cost_usd(
    provider: str,
    deep_model: str,
    quick_model: str,
    input_tokens: int,
    output_tokens: int,
) -> float | None:
    """Estimate USD cost for the bot-wide token totals. Returns None when
    no price entry exists for either model — caller renders tokens-only.

    Pricing model: assume input tokens are split 50/50 between deep and
    quick (a coarse approximation — the deep model handles the synthesis
    rounds while quick handles analyst calls + tools, but the actual
    split varies per ticker). Output tokens follow the same split. Good
    enough for an order-of-magnitude check; the operator should treat
    the rendered figure as ±30%.

    Returns the total estimated cost in USD. The caller decides how to
    format it (typically 2 decimal places).
    """
    deep_price = LLM_PRICE_USD_PER_M.get((provider.lower(), deep_model.lower()))
    quick_price = LLM_PRICE_USD_PER_M.get((provider.lower(), quick_model.lower()))
    # Need at least one model price to estimate; missing one means we
    # extrapolate the other across all tokens — better than nothing but
    # noted in the docstring as ±30%.
    if deep_price is None and quick_price is None:
        return None
    effective_in = (deep_price or quick_price)[0]
    effective_out = (deep_price or quick_price)[1]
    if deep_price is not None and quick_price is not None:
        # Average for 50/50 split.
        effective_in = (deep_price[0] + quick_price[0]) / 2
        effective_out = (deep_price[1] + quick_price[1]) / 2
    return (input_tokens / 1_000_000) * effective_in + (
        output_tokens / 1_000_000
    ) * effective_out
