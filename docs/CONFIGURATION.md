# Configuration

Every runtime knob lives in `.env` (Docker loads via `env_file:`; local runs pick it up via `python-dotenv`). The four required secrets are pre-templated in [`.env.example`](../.env.example) — copy that as `.env` and fill them in if you're doing a manual install.

## Environment variables

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | From [@BotFather](https://t.me/BotFather). |
| `TELEGRAPH_ACCESS_TOKEN` | yes | For Telegraph publishing. Generate via `curl 'https://api.telegra.ph/createAccount?short_name=YourBot&author_name=YourBot'`. |
| `ALLOWED_USER_IDS` | strongly recommended | Comma-separated. Empty = open to anyone (logged at WARNING on startup). The bot replies with the requesting user's Telegram ID on rejection so you can whitelist them. |
| Provider keys | yes (≥1) | `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `XAI_API_KEY`, `DASHSCOPE_API_KEY` (qwen), `ZHIPU_API_KEY` (glm — renamed upstream in tradingagents v0.2.5 from `ZHIPUAI_API_KEY`), `MINIMAX_API_KEY` (minimax — added in v0.2.5). Fill in as many as you have — the active one is whichever you select via `/config`; the others sit unused. At least one is required so `/config` has something to work with. Ollama needs no key. **China endpoints:** `DASHSCOPE_CN_API_KEY` / `ZHIPU_CN_API_KEY` / `MINIMAX_CN_API_KEY` are mainland variants — only one of each pair is needed. |
| `OLLAMA_BASE_URL` | no | Defaults to local `http://localhost:11434/v1`. Set to point at a remote Ollama server (e.g. `http://gpu-host:11434/v1`). |
| `TG_BOT_MAX_CONCURRENT_ANALYSES` | no | Max simultaneous analyses across the bot. Default `3`, clamped to ≥1. Acts as a FIFO queue — selections beyond this show "⏳ Queued" until a slot frees, cancellable while waiting. Also sizes the graph-instance pool. Higher = more parallelism, more memory (~50–200 MB per cached graph). |
| `TG_BOT_TA_DEBUG` | no | `1`/`true` enables `TradingAgentsGraph(debug=True)` (verbose, dev only). Default off. |
| `TG_BOT_DATA_DIR` | no | Default `data`. Storage location for `watchlist.json` + `user_config.json`. |
| `TRADINGAGENTS_RESULTS_DIR` | recommended in Docker | `/history` reads from here. Defaults to `~/.tradingagents/logs` (ephemeral inside containers). Set to `/app/data/ta-logs` to persist via the bind mount. |
| `TRADINGAGENTS_CACHE_DIR` | recommended in Docker | yfinance cache. Defaults to `~/.tradingagents/cache` (ephemeral). Set to `/app/data/ta-cache` to skip re-downloads on every restart. |

## tradingagents config overlay (`TRADINGAGENTS_*`)

The upstream tradingagents lib (v0.2.5+) reads a handful of `TRADINGAGENTS_*` env vars at import time and overlays them onto `DEFAULT_CONFIG` — useful for forcing a process-wide override that the bot's per-user `/config` UI doesn't expose. Each variable's value is coerced to the type of its `DEFAULT_CONFIG` default, so plain strings work for bools (`"true"`/`"1"`) and ints (`"3"`).

| Variable | Default | Notes |
|---|---|---|
| `TRADINGAGENTS_MAX_DEBATE_ROUNDS` | `1` | Bull-vs-bear debate rounds per analysis. The bot's `/config` already exposes this per-user; the env-var forces a process-wide floor. |
| `TRADINGAGENTS_MAX_RISK_ROUNDS` | `1` | Risk-management discussion rounds (added in v0.2.5). Not yet wired into the bot's `/config` UI — set here if you want longer risk debate. |
| `TRADINGAGENTS_OUTPUT_LANGUAGE` | `English` | Language for analyst reports + final decision. Internal agent debate stays English regardless (LLM reasoning quality). |
| `TRADINGAGENTS_CHECKPOINT_ENABLED` | `false` | LangGraph state checkpointing — when on, a crashed run can resume from the last successful node. Adds disk overhead; default off. |
| `TRADINGAGENTS_BENCHMARK_TICKER` | (none) | Force a specific alpha benchmark for the reflection layer. Default uses `benchmark_map` auto-detection (SPY for US, ^NSEI for `.NS`, ^HSI for `.HK`, etc.). |
| `TRADINGAGENTS_LLM_PROVIDER` / `_DEEP_THINK_LLM` / `_QUICK_THINK_LLM` | per-provider | `/config` is usually preferred; these are for headless deployments without an interactive `/config` session. |

## Supported LLM providers

Set via `/config` after the bot is up:

- **Built-in model catalogs** (deep + quick model picker): `openai`, `google`, `anthropic`, `xai`, `deepseek`, `qwen`, `glm`, `minimax`, `ollama`, `openrouter`.
- **Custom model IDs only** (selection UI not yet wired — fall back to `DEFAULT_CONFIG`): `azure`.

OpenRouter's catalog ships a curated starter list (free `meta-llama/llama-3.3-70b-instruct:free`, plus paid `openai/gpt-4o-mini`, `anthropic/claude-3.5-sonnet`, `deepseek/deepseek-r1`, etc.) — see `src/tg_bot/analysis.py:_OPENROUTER_MODELS`. tradingagents' OpenAIClient routes openrouter requests to `https://openrouter.ai/api/v1` and reads `OPENROUTER_API_KEY`.
