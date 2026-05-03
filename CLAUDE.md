# TradingAgents-Telegram — Architecture Reference

Telegram bot wrapping the [TradingAgents](https://github.com/TauricResearch/TradingAgents) library. Users curate a watchlist via Telegram, tap a ticker, and the bot runs `TradingAgentsGraph.propagate(...)` and posts a finviz chart + Telegraph link with the verdict. Per-step pipeline progress is streamed back into the message caption while the analysis runs.

## Layout (`src/` package)

```
src/tg_bot/
├── __init__.py            # loads .env once on package import (load_dotenv)
├── __main__.py            # `python -m tg_bot`
├── app.py                 # Application builder, BOT_COMMANDS, main()
├── auth.py                # authorize() TypeHandler at group=-1
├── config.py              # Config class — env-driven
├── analysis.py            # run_trading_analysis + graph cache + model catalog
├── chart.py               # finviz_chart_url (with cache-buster)
├── formatters.py          # format_short_message, format_analysis_result_markdown
├── progress.py            # ProgressReporter + LangChain BaseCallbackHandler
├── history.py             # disk-readers for past analyses (/history)
├── telegraph_client.py    # sanitize + publish (Telegraph instance is lazy)
├── handlers/
│   ├── commands.py        # /start /help /add /del /watch /list /config /history
│   └── callbacks.py       # inline-button dispatcher (prefix-based)
└── storage/
    ├── _base.py           # JsonStorage (atomic + fsync writes, async wrapper)
    ├── watchlist.py       # WatchlistStorage(JsonStorage)
    ├── user_config.py     # UserConfigStorage(JsonStorage)
    └── __init__.py        # exports process-wide singletons
```

Top-level: `pyproject.toml` (deps), `Dockerfile`, `docker-compose.yml`, `.env`, `data/` (runtime state, gitignored), `.github/workflows/` (lint + Docker build with SHA-check skip).

## Run / deploy

| | Command |
|---|---|
| Install dev | `pip install -e .` (then `chflags nohidden .venv/lib/python3.14/site-packages/*.pth` once on macOS — see "macOS .pth quirk" below) |
| Run locally | `python -m tg_bot` (CWD must contain `.env` and `data/`) |
| Build & deploy | `docker compose up -d --build` |
| Update | `git pull && docker compose up -d --build` (data/ persists via bind mount) |

`TG_BOT_DATA_DIR` env var overrides the default `data/` path.

## Commands

| Command | Behavior |
|---|---|
| `/start`, `/help` | Welcome / help text |
| `/add NVDA AAPL` | Bulk-add tickers in one shot |
| `/add` (no args) | Prompts with `ForceReply`; reply text is parsed as ticker(s) by `add_via_reply` |
| `/del NVDA AAPL` | Bulk-remove |
| `/del` (no args) | Inline-button picker; each ❌ tap removes immediately, `✅ Done` closes the picker |
| `/watch`, `/list` | Watchlist with clickable ticker buttons (tap → analyze) |
| `/config` | Three-step flow: provider → deep model → quick model. Each step has `❌ Cancel` that restores a snapshot of the prior `(provider, deep, quick)` triple |
| `/history NVDA` | Inline picker of recent analysis dates |
| `/history NVDA 2026-04-15` | Direct lookup by date — publishes the saved analysis to Telegraph |

`set_my_commands` in `app.py:_post_init` exposes these as Telegram's native Menu button + `/`-autocomplete.

## Key contracts

- **Storage singletons** live in `tg_bot.storage` — both handler modules import the same `watchlist_storage` / `user_config_storage` so writes are immediately visible everywhere. Don't construct your own `WatchlistStorage()` / `UserConfigStorage()` in handlers.
- **Storage mutations are async + atomic + durable.** `add_ticker`, `remove_ticker`, `set_llm_provider`, `set_llm_model`, `clear` are `async def` and call `await self._save_async()`. `_save` writes to a tempfile in the same directory, `flush + fsync`s the file descriptor, then `os.replace`s into place — survives mid-write power loss. Read methods (`get_*`) stay sync — pure in-memory dict reads.
- **Per-user config** lives in `data/user_config.json`, keyed by stringified Telegram user_id, holding `llm_provider` + `deep_think_llm` + `quick_think_llm`. `set_llm_provider` wipes deep/quick (provider-specific). `clear(user_id)` removes the whole entry — used by the `/config` cancel-rollback when there was no prior provider.
- **Auth gate** (`auth.py:authorize`) runs at `group=-1` for every Update; raises `ApplicationHandlerStop` for users not in `Config.ALLOWED_USER_IDS`. Empty list = open to all and is logged at WARNING level on startup.
- **`TRADINGAGENTS_AVAILABLE`** flag gates analysis; bot still loads if tradingagents fails to import.
- **Graph caching.** `analysis.py:_graph_cache` memoizes `TradingAgentsGraph` by `(provider, deep, quick)` to avoid the expensive re-init on every run. Each cached entry carries a `threading.Lock` because the graph mutates `self.ticker` / `self.curr_state` during `propagate()` — calls on the *same* instance must be serialized; different keys still run in parallel via PTB's worker thread pool. Don't bypass `_get_or_create_graph` to build a graph directly.
- **Per-step progress.** `progress.py:delegating_progress_callback` is a singleton `BaseCallbackHandler` attached to every cached graph via `callbacks=[...]` in the constructor. TradingAgents passes those into the LLM kwargs, so we receive `on_chat_model_start` / `on_llm_start` events — **not** `on_chain_start`. LangGraph propagates the surrounding node name as `metadata["langgraph_node"]` on every nested LLM call, which is what we extract. Per-run target (`ProgressReporter`) lives in a `threading.local()` set by `run_trading_analysis` around `propagate()`. The reporter dedupes on `_last_step` and bridges back onto the asyncio loop via `asyncio.run_coroutine_threadsafe`.
- **Telegraph publishing is offloaded.** `telegraph_client.publish_to_telegraph` wraps the SDK's blocking `create_page` in `asyncio.to_thread` so the event loop stays responsive during the network round-trip.
- **All Telegram messages use `parse_mode="MarkdownV2"`** consistently. Variable content goes through `telegram.helpers.escape_markdown(version=2)` (or sits inside `` `…` `` code spans which need no escaping). Don't mix legacy `Markdown` back in.
- **Callback dispatch** is prefix-based: `provider:`, `deep:`, `quick:`, `info:`, `del:`, `cancel:`, `hist:`. Stay under Telegram's 64-byte `callback_data` limit (longest current value ≈ 42 bytes).
- **Reply-driven `/add`.** `commands.ADD_PROMPT` is matched verbatim against `update.message.reply_to_message.text` so `add_via_reply` only fires on replies to our actual prompt, not random replies to bot messages.
- **Telegraph CONTENT_TOO_BIG**: `format_analysis_result_markdown` emits only `final_trade_decision` + `trader_investment_plan`. Adding more `final_state` sections risks blowing the cap — truncate or drop sections, don't just append.
- **finviz cache-busting**: `chart.py:finviz_chart_url` appends `&_=<unix_ts>` so Telegram's CDN doesn't serve a stale cached photo.
- **History command** (`/history <ticker> [YYYY-MM-DD]`) reads tradingagents' on-disk JSON logs at `<results_dir>/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json`. `history.normalize_ticker` regex-validates `[A-Z0-9.\-]+` to block path traversal, and the date arg must round-trip through `date.fromisoformat` before joining the path.

## Environment variables

```
TELEGRAM_BOT_TOKEN=<required>
TELEGRAPH_ACCESS_TOKEN=<required for Telegraph publishing>
ALLOWED_USER_IDS=123,456              # empty = open (logged at WARNING)
ADMIN_USER_IDS=123                    # currently parsed but unused
TG_BOT_DATA_DIR=data                  # default
TG_BOT_TA_DEBUG=                      # set "1"/"true" to enable TradingAgentsGraph(debug=True)
TRADINGAGENTS_RESULTS_DIR=...         # /history reads from here; defaults to ~/.tradingagents/logs
TRADINGAGENTS_CACHE_DIR=...           # tradingagents data cache; defaults to ~/.tradingagents/cache
# Plus provider keys: OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / etc.
```

**Docker persistence caveat.** `TRADINGAGENTS_RESULTS_DIR` and `TRADINGAGENTS_CACHE_DIR` default to paths under `~/.tradingagents`, which is ephemeral inside the container. To persist `/history` data and avoid re-fetching yfinance on every restart, set them to paths under `/app/data/` (which is bind-mounted) — for example `TRADINGAGENTS_RESULTS_DIR=/app/data/ta-logs`.

## Conventions

- Don't reintroduce a `utils.py` junk drawer. New helpers go in a focused module.
- Storage mutations must use the existing `_save` / `_save_async` (atomic + fsync). New storage classes inherit `JsonStorage`.
- Blocking calls (`telegraph.create_page`, `propagate`, file I/O) belong inside `asyncio.to_thread` — PTB runs handlers on one event loop.
- All Telegram message bodies use MarkdownV2 with `escape_markdown(version=2)` for variable content.
- Don't bake `.env` into the Docker image; `.dockerignore` excludes it. Compose loads it via `env_file:`.
- TradingAgents is a pip dep (`tradingagents @ git+...` in `pyproject.toml`); no sys.path hacks.

## CI/CD

`.github/workflows/`:
- **`lint.yml`** — Ruff check + format on push/PR.
- **`docker-build.yml`** — push to main, daily cron, or manual dispatch builds and pushes to Docker Hub. Trivy CRITICAL/HIGH SARIF upload. The cron run resolves upstream `tradingagents` `HEAD` SHA via `git ls-remote` and skips the build (via `actions/cache@v4`) when the SHA matches the previous build, so unchanged days don't burn CI minutes. Push events use `cache-from: type=gha` for fast iteration; schedule + manual dispatch force `--no-cache` so the `pip install` layer re-resolves tradingagents.
- **`dependabot.yml`** — weekly bumps for pip / github-actions / docker.

## Known limitations

- `openrouter` and `azure` providers have no model catalog; their selection step short-circuits with a notice and analysis falls back to `DEFAULT_CONFIG` models. Custom-model-ID input UI is not yet wired.
- `send_photo` / Telegraph publish have no explicit timeouts; if finviz or Telegraph hangs, PTB's defaults apply (~5s) and the user sees no progress beyond the "Analyzing…" caption.
- `Config.TA_DEBUG` is read once at process start; toggling `TG_BOT_TA_DEBUG` requires a bot restart (and would still only affect graphs built *after* the restart since cached entries carry their `debug` flag from init time).
- Cached `TradingAgentsGraph` instances are never evicted; the cache grows unbounded (realistically capped at ~60 entries from the catalog combinatorics, so not pressing).
- `config_cmd`'s snapshot in `context.user_data["llm_snapshot"]` is not popped on the success path (only on Cancel); harmless but inconsistent.
- No tests, no structured logging / correlation id, no graceful shutdown hooks.

## macOS .pth quirk

Python 3.14's `site.py` skips any `.pth` file marked with the macOS `UF_HIDDEN` filesystem flag. Files inside `~/Desktop` (especially with iCloud Desktop sync enabled) tend to inherit this flag. `pip install -e .` writes `__editable__.tg_bot-0.1.0.pth` which then gets ignored, so `python -m tg_bot` fails with `No module named tg_bot`.

Fix:
```bash
chflags nohidden .venv/lib/python3.14/site-packages/*.pth
```

Apply once after each fresh install. Docker (Linux) is unaffected.

## Recently fixed (May 2026)

- `_save` is now durable (fsync between write and rename), not just atomic. A crash between rename and OS flush can no longer leave an empty file.
- `analysis.py` no longer logs the entire `final_state` at INFO — that single line was tens of KB per run. Replaced with `Analysis complete for <ticker> — signal=<x>`; full state moved to `logger.debug`.
- Empty `ALLOWED_USER_IDS` logs at **WARNING** (was INFO), with an explicit "your LLM tokens are at risk" line so the open-bot risk surfaces in production.
- Per-step progress reporting (`progress.py`) hooked via LLM-level callback events with `metadata["langgraph_node"]` extraction. `on_chain_start` doesn't fire here because tradingagents passes our callbacks into LLM constructor kwargs.
- Cancel coverage extended: `❌ Cancel` on the `/config` provider keyboard, `✅ Done` on the `/del` picker, snapshot/restore semantics so an accidental provider tap can be rolled back.
- `tradingagents` URL in `pyproject.toml` is unpinned (HEAD-tracking). Daily GitHub Action cron checks for new upstream commits and only rebuilds when the SHA changes; manual dispatch forces rebuild.
- All Telegram messages are MarkdownV2 with proper escaping. Variable content runs through `telegram.helpers.escape_markdown(version=2)`.
- Telegraph publish failures surface as `"⚠️ Full report unavailable (Telegraph publish failed)."` in the caption instead of a silently missing link.
- `TradingAgentsGraph(debug=…)` reads from `Config.TA_DEBUG` (env: `TG_BOT_TA_DEBUG`); defaults to `False` for prod.
