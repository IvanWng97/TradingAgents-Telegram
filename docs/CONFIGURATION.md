# Configuration

Every runtime knob lives in `.env` (Docker loads via `env_file:`; local runs pick it up via `python-dotenv`). The four required secrets are pre-templated in [`.env.example`](../.env.example) — copy that as `.env` and fill them in if you're doing a manual install.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | From [@BotFather](https://t.me/BotFather). |
| `TELEGRAPH_ACCESS_TOKEN` | yes | For Telegraph publishing. Generate via `curl 'https://api.telegra.ph/createAccount?short_name=YourBot&author_name=YourBot'`. |
| `ALLOWED_USER_IDS` | strongly recommended | Comma-separated. Empty = open to anyone (logged at WARNING on startup). The bot replies with the requesting user's Telegram ID on rejection so you can whitelist them. |
| Provider keys | yes (≥1) | `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, `DASHSCOPE_API_KEY` (qwen), `ZHIPU_API_KEY` (glm — renamed upstream in tradingagents v0.2.5 from `ZHIPUAI_API_KEY`), `MINIMAX_API_KEY` (minimax — added in v0.2.5). Fill in as many as you have — the active one is whichever you select via `/config`; the others sit unused. At least one is required so `/config` has something to work with. Ollama needs no key. |
| `TG_BOT_MAX_CONCURRENT_ANALYSES` | no | Max simultaneous analyses across the bot. Default `3`, clamped to ≥1. Acts as a FIFO queue — selections beyond this show "⏳ Queued" until a slot frees, cancellable while waiting. Also sizes the graph-instance pool. Higher = more parallelism, more memory (~50–200 MB per cached graph). |
| `TG_BOT_TA_DEBUG` | no | `1`/`true` enables `TradingAgentsGraph(debug=True)` (verbose, dev only). Default off. |
| `TG_BOT_DATA_DIR` | no | Default `data`. Storage location for `watchlist.json` + `user_config.json`. |
| `TRADINGAGENTS_RESULTS_DIR` | recommended in Docker | `/history` reads from here. Defaults to `~/.tradingagents/logs` (ephemeral inside containers). Set to `/app/data/ta-logs` to persist via the bind mount. |
| `TRADINGAGENTS_CACHE_DIR` | recommended in Docker | yfinance cache. Defaults to `~/.tradingagents/cache` (ephemeral). Set to `/app/data/ta-cache` to skip re-downloads on every restart. |

## Supported LLM providers

Set via `/config` after the bot is up:

- **Built-in model catalogs** (deep + quick model picker): `openai`, `google`, `anthropic`, `xai`, `deepseek`, `qwen`, `glm`, `minimax`, `ollama`, `openrouter`.
- **Custom model IDs only** (selection UI not yet wired — fall back to `DEFAULT_CONFIG`): `azure`.

OpenRouter's catalog ships a curated starter list (free `meta-llama/llama-3.3-70b-instruct:free`, plus paid `openai/gpt-4o-mini`, `anthropic/claude-3.5-sonnet`, `deepseek/deepseek-r1`, etc.) — see `src/tg_bot/analysis.py:_OPENROUTER_MODELS`. tradingagents' OpenAIClient routes openrouter requests to `https://openrouter.ai/api/v1` and reads `OPENROUTER_API_KEY`.
