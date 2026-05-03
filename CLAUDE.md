# TradingAgents-Telegram — Architecture Reference

Telegram bot wrapping the [TradingAgents](https://github.com/TauricResearch/TradingAgents) library. Users add tickers to a watchlist via Telegram, tap a ticker, and the bot runs `TradingAgentsGraph.propagate(...)` and posts a chart + Telegraph link with the verdict.

## Layout (`src/` package)

```
src/tg_bot/
├── __init__.py            # loads .env once on package import (load_dotenv)
├── __main__.py            # `python -m tg_bot`
├── app.py                 # Application builder, BOT_COMMANDS, main()
├── auth.py                # authorize() TypeHandler at group=-1
├── config.py              # Config class — env-driven
├── analysis.py            # run_trading_analysis + model catalog accessors
├── chart.py               # finviz_chart_url (with cache-buster)
├── formatters.py          # format_short_message, format_analysis_result_markdown
├── telegraph_client.py    # sanitize + publish (Telegraph instance is lazy)
├── handlers/
│   ├── commands.py        # /start /help /add /del /watch /list /config
│   └── callbacks.py       # inline-button dispatcher (prefix-based)
└── storage/
    ├── _base.py           # JsonStorage with atomic tempfile+os.replace writes
    ├── watchlist.py       # WatchlistStorage(JsonStorage)
    ├── user_config.py     # UserConfigStorage(JsonStorage)
    └── __init__.py        # exports process-wide singletons
```

Top-level: `pyproject.toml` (deps), `Dockerfile`, `docker-compose.yml`, `.env`, `data/` (runtime state, gitignored).

## Run / deploy

| | Command |
|---|---|
| Install dev | `pip install -e .` |
| Run locally | `python -m tg_bot` (CWD must contain `.env` and `data/`) |
| Build & deploy | `docker compose up -d --build` |
| Update | `git pull && docker compose up -d --build` (data/ persists via bind mount) |

`TG_BOT_DATA_DIR` env var overrides the default `data/` path.

## Key contracts

- **Storage singletons** live in `tg_bot.storage` — both handler modules import the same `watchlist_storage` / `user_config_storage` so writes are immediately visible everywhere. Don't construct your own `WatchlistStorage()` / `UserConfigStorage()` in handlers.
- **Storage mutations are async.** `add_ticker`, `remove_ticker`, `set_llm_provider`, `set_llm_model` are `async def` and call `await self._save_async()` (which `asyncio.to_thread`s the atomic write). Handlers must `await` them. Read methods (`get_*`) stay sync — pure in-memory dict reads.
- **Per-user config** lives in `data/user_config.json`, keyed by stringified Telegram user_id, holding `llm_provider` + `deep_think_llm` + `quick_think_llm`. Provider switch wipes the model fields (they're provider-specific).
- **Auth gate** (`auth.py:authorize`) runs at `group=-1` for every Update; raises `ApplicationHandlerStop` for users not in `Config.ALLOWED_USER_IDS`. Empty list = open to all.
- **`TRADINGAGENTS_AVAILABLE`** flag gates analysis; bot still loads if tradingagents fails to import.
- **Graph caching.** `analysis.py:_graph_cache` memoizes `TradingAgentsGraph` by `(provider, deep, quick)` to avoid the expensive re-init on every run. Each cached entry carries a `threading.Lock` because the graph mutates `self.ticker` / `self.curr_state` during `propagate()` — calls on the *same* instance must be serialized; different keys still run in parallel via PTB's worker thread pool. Don't bypass `_get_or_create_graph` to build a graph directly.
- **Telegraph publishing is offloaded.** `telegraph_client.publish_to_telegraph` wraps the SDK's blocking `create_page` in `asyncio.to_thread` so the event loop stays responsive during the network round-trip.
- **Callback dispatch** is prefix-based: `provider:`, `deep:`, `quick:`, `info:`. Stay under Telegram's 64-byte `callback_data` limit (longest current value ≈ 42 bytes).
- **Telegraph CONTENT_TOO_BIG**: `format_analysis_result_markdown` emits only `final_trade_decision` + `trader_investment_plan`. Adding more `final_state` sections risks blowing the cap — truncate or drop sections, don't just append.
- **finviz cache-busting**: `chart.py:finviz_chart_url` appends `&_=<unix_ts>` so Telegram's CDN doesn't serve a stale cached photo.
- **History command** (`/history <ticker> [YYYY-MM-DD]`) reads tradingagents' on-disk JSON logs at `<results_dir>/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json`. `results_dir` defaults to `~/.tradingagents/logs` and is configurable via `TRADINGAGENTS_RESULTS_DIR`. **Docker caveat**: that path is ephemeral by default — set `TRADINGAGENTS_RESULTS_DIR=/app/data/ta-logs` and bind-mount it (or include `data/ta-logs` in the existing `data/` mount) for history to survive container restarts.

## Environment variables

```
TELEGRAM_BOT_TOKEN=<required>
TELEGRAPH_ACCESS_TOKEN=<required for Telegraph publishing>
ALLOWED_USER_IDS=123,456              # empty = open
ADMIN_USER_IDS=123                    # currently parsed but unused
TG_BOT_DATA_DIR=data                  # default
# Plus provider keys: OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / etc.
```

## Conventions

- Don't reintroduce a `utils.py` junk drawer. New helpers go in a focused module.
- Storage mutations must use the existing `_save()` (atomic write). New storage classes inherit `JsonStorage`.
- Blocking calls (`telegraph.create_page`, `propagate`) belong inside `asyncio.to_thread` — PTB runs handlers on one event loop.
- Don't bake `.env` into the Docker image; `.dockerignore` excludes it. Compose loads it via `env_file:`.
- TradingAgents is a pip dep (`tradingagents @ git+...` in `pyproject.toml`); no sys.path hacks.

## Known limitations

- `openrouter` and `azure` providers have no model catalog; their selection step short-circuits with a notice and analysis falls back to `DEFAULT_CONFIG` models. Custom-model-ID input UI is not yet wired.
- `send_photo` / Telegraph publish have no explicit timeouts; if finviz or Telegraph hangs, PTB's defaults apply (~5s) and the user sees no progress beyond the "Analyzing…" caption.
- `Config.TA_DEBUG` is read once at process start; toggling `TG_BOT_TA_DEBUG` requires a bot restart (and would still only affect graphs built *after* the restart since cached entries carry their `debug` flag from init time).
- Cached `TradingAgentsGraph` instances are never evicted; the cache grows unbounded (realistically capped at ~60 entries from the catalog combinatorics, so not pressing).
- No tests, no structured logging / correlation id, no graceful shutdown hooks.

## Recently fixed (May 2026)

- All Telegram messages now use `parse_mode="MarkdownV2"` consistently. Variable content goes through `telegram.helpers.escape_markdown(version=2)` (or sits inside `` `…` `` code spans which need no escaping). Don't mix legacy `Markdown` back in.
- Telegraph publish failures surface as `"⚠️ Full report unavailable (Telegraph publish failed)."` in the caption instead of a silently missing link.
- `tradingagents` is pinned to a commit SHA in `pyproject.toml`. Bumping is an explicit edit; floating HEAD is not.
- `TradingAgentsGraph(debug=…)` now reads from `Config.TA_DEBUG` (env: `TG_BOT_TA_DEBUG`); defaults to `False` for prod.
