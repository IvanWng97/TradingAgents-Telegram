# Configuration

Every runtime knob lives in `.env` (Docker loads via `env_file:`; local runs pick it up via `python-dotenv`). The bot is designed for single-tenant / single-operator use — `.env` is the **single source of truth** for the active LLM provider + model pair, and every user of the bot shares that one configuration. The four required secrets are pre-templated in [`.env.example`](../.env.example) — copy that as `.env` and fill them in if you're doing a manual install.

## Environment variables

### Bot secrets + access control

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | From [@BotFather](https://t.me/BotFather). |
| `TELEGRAPH_ACCESS_TOKEN` | yes | For Telegraph publishing. Generate via `curl 'https://api.telegra.ph/createAccount?short_name=YourBot&author_name=YourBot'`. |
| `ALLOWED_USER_IDS` | strongly recommended | Comma-separated. Empty = open to anyone (logged at WARNING on startup). The bot replies with the requesting user's Telegram ID on rejection so you can whitelist them. |

### LLM provider + model (overlay onto tradingagents' `DEFAULT_CONFIG`)

Upstream `tradingagents` reads these env vars at library-import time via its `_ENV_OVERRIDES` pass — they replace fields in `DEFAULT_CONFIG` without code changes. Restart the bot to pick up changes.

| Variable | Required | Notes |
|---|---|---|
| `TRADINGAGENTS_LLM_PROVIDER` | yes | One of `openai`, `anthropic`, `google`, `xai`, `deepseek`, `qwen`, `qwen-cn`, `glm`, `glm-cn`, `minimax`, `minimax-cn`, `openrouter`, `ollama`. |
| `TRADINGAGENTS_DEEP_THINK_LLM` | yes | The "deep think" model — agents use this for thesis + risk + research. Heavier / slower / pricier. |
| `TRADINGAGENTS_QUICK_THINK_LLM` | yes | The "quick think" model — agents use this for fast tool-call orchestration. Cheaper / faster. |
| Provider API key | yes (1) | The key matching `TRADINGAGENTS_LLM_PROVIDER`: `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, `DASHSCOPE_API_KEY` (qwen / qwen-cn), `ZHIPU_API_KEY` (glm / glm-cn — renamed upstream in v0.2.5 from `ZHIPUAI_API_KEY`), `MINIMAX_API_KEY` (minimax / minimax-cn — added in v0.2.5), `OPENROUTER_API_KEY`. Ollama needs no key. Sibling provider keys can stay blank — only the one matching `TRADINGAGENTS_LLM_PROVIDER` is read at run time. The bot startup logs a WARNING if the key for the configured provider is missing. |
| `TRADINGAGENTS_MAX_DEBATE_ROUNDS` | no | `1`/`2`/`3`, default `1`. Higher = more nuanced thesis (~1.5–2× cost). |
| `TRADINGAGENTS_OPENAI_REASONING_EFFORT` | no | `low`/`medium`/`high`. Applied only when provider is `openai`. Local overlay (upstream doesn't expose this). |
| `TRADINGAGENTS_ANTHROPIC_EFFORT` | no | `low`/`medium`/`high`. Applied only when provider is `anthropic`. Local overlay. |
| `TRADINGAGENTS_GOOGLE_THINKING_LEVEL` | no | `low`/`medium`/`high`. Applied only when provider is `google`. Local overlay. |

### Bot tuning + persistence

| Variable | Required | Notes |
|---|---|---|
| `TG_BOT_MAX_CONCURRENT_ANALYSES` | no | Max simultaneous analyses across the bot. Default `3`, clamped to ≥1. Acts as a FIFO queue — selections beyond this show "⏳ Queued" until a slot frees, cancellable while waiting. Also sizes the graph-instance pool. Higher = more parallelism, more memory (~50–200 MB per cached graph). |
| `TG_BOT_TA_DEBUG` | no | `1`/`true` enables `TradingAgentsGraph(debug=True)` (verbose, dev only). Default off. |
| `TG_BOT_DATA_DIR` | no | Default `data`. Storage location for `watchlist.json` + `user_config.json`. |
| `TRADINGAGENTS_RESULTS_DIR` | recommended in Docker | `/history` reads from here. Defaults to `~/.tradingagents/logs` (ephemeral inside containers). Set to `/app/data/ta-logs` to persist via the bind mount. |
| `TRADINGAGENTS_CACHE_DIR` | recommended in Docker | yfinance cache. Defaults to `~/.tradingagents/cache` (ephemeral). Set to `/app/data/ta-cache` to skip re-downloads on every restart. |

## Supported LLM providers

Set `TRADINGAGENTS_LLM_PROVIDER` to one of:

- **First-class** (tradingagents catalog ships canonical deep + quick model pairs): `openai`, `google`, `anthropic`, `xai`, `deepseek`, `qwen`, `qwen-cn`, `glm`, `glm-cn`, `minimax`, `minimax-cn`, `ollama`, `openrouter`.
- **Custom-only** (no catalog defaults; configure the deep/quick model IDs manually via `TRADINGAGENTS_DEEP_THINK_LLM` / `TRADINGAGENTS_QUICK_THINK_LLM`): `azure`.

The full upstream model catalog lives in `tradingagents.llm_clients.model_catalog.get_model_options(provider, mode)` — `start.sh` writes the first entry per provider as the canonical default. Edit `TRADINGAGENTS_DEEP_THINK_LLM` / `TRADINGAGENTS_QUICK_THINK_LLM` in `.env` to switch to a different model from the catalog.
