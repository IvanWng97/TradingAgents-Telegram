# TradingAgents-Telegram — Architecture Reference

Telegram bot wrapping the [TradingAgents](https://github.com/TauricResearch/TradingAgents) library. Users curate a watchlist via Telegram, tap a ticker, and the bot runs `TradingAgentsGraph.propagate(...)` and posts a finviz chart + Telegraph link with the verdict. Per-step pipeline progress is streamed back into the message caption while the analysis runs.

## Layout (`src/` package)

```
src/tg_bot/
├── __init__.py            # loads .env once on package import (load_dotenv)
├── __main__.py            # `python -m tg_bot`
├── app.py                 # Application builder (concurrent_updates=True), BOT_COMMANDS, main()
├── auth.py                # authorize() TypeHandler at group=-1
├── config.py              # Config class — env-driven
├── analysis.py            # run_trading_analysis + GraphPool + model catalog
├── chart.py               # finviz_chart_url (with cache-buster)
├── formatters.py          # format_short_message, extract_summary, format_analysis_result_markdown
├── progress.py            # ProgressReporter + cancel-aware LangChain BaseCallbackHandler
├── history.py             # disk-readers for past analyses (/history)
├── telegraph_client.py    # sanitize + publish (Telegraph instance is lazy)
├── validation.py          # yfinance-backed validate_ticker + class-share rewrite
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
| `/add NVDA AAPL` | Bulk-add tickers. Each is yfinance-validated in parallel via `validation.validate_ticker`; class-share dot forms (`BRK.B`) auto-correct to dash form (`BRK-B`); invalid symbols are rejected with a hint. |
| `/add` (no args) | Prompts with `ForceReply`; reply text is parsed as ticker(s) by `add_via_reply` (also validated). |
| `/del NVDA AAPL` | Bulk-remove |
| `/del` (no args) | Inline-button picker; each ❌ tap removes immediately, `✅ Done` closes the picker |
| `/watch`, `/list` | Select-mode watchlist (always — no separate "multi-select" toggle). Tap any ticker → ✅ prefix and `Done (N)` counter increments; `✅ Done` runs the selected ticker(s); `❌ Cancel` dismisses. Single ticker uses the cached graph (cheap init); 2+ tickers run in parallel via `asyncio.gather`, each pulling its own graph instance from the pool. |
| `/config` | Three-step flow: provider → deep model → quick model. Each step has `❌ Cancel` that restores a snapshot of the prior `(provider, deep, quick)` triple |
| `/history` (no args) | Inline picker of all tickers with saved history, with `❌ Cancel` to dismiss |
| `/history NVDA` | Inline picker of recent analysis dates with `← Back` (returns to ticker picker) and `❌ Cancel` |
| `/history NVDA 2026-04-15` | Direct lookup by date — publishes the saved analysis to Telegraph (final view also has `← Back` to the date picker) |
| `/status` | Diagnostic snapshot — process uptime, total analyses run since boot (`bot_data["analysis_count"]`), graph pool size from `analysis.pool_stats()`, and the requesting user's `(provider, deep, quick)` LLM config |

`set_my_commands` in `app.py:_post_init` exposes these as Telegram's native Menu button + `/`-autocomplete. `_post_init` also stamps `bot_data["start_time"] = time.time()` so `/status` can compute uptime.

## Key contracts

- **Storage singletons** live in `tg_bot.storage` — both handler modules import the same `watchlist_storage` / `user_config_storage` so writes are immediately visible everywhere. Don't construct your own `WatchlistStorage()` / `UserConfigStorage()` in handlers.
- **Storage mutations are async + atomic + durable.** `add_ticker`, `remove_ticker`, `set_llm_provider`, `set_llm_model`, `clear` are `async def` and call `await self._save_async()`. `_save` writes to a tempfile in the same directory, `flush + fsync`s the file descriptor, then `os.replace`s into place — survives mid-write power loss. Read methods (`get_*`) stay sync — pure in-memory dict reads.
- **Per-user config** lives in `data/user_config.json`, keyed by stringified Telegram user_id, holding `llm_provider` + `deep_think_llm` + `quick_think_llm`. `set_llm_provider` wipes deep/quick (provider-specific). `clear(user_id)` removes the whole entry — used by the `/config` cancel-rollback when there was no prior provider.
- **Auth gate** (`auth.py:authorize`) runs at `group=-1` for every Update; raises `ApplicationHandlerStop` for users not in `Config.ALLOWED_USER_IDS`. Empty list = open to all and is logged at WARNING level on startup.
- **`TRADINGAGENTS_AVAILABLE`** flag gates analysis; bot still loads if tradingagents fails to import.
- **Graph pool.** `analysis.py:_graph_pool` keeps a `GraphPool` per `(provider, deep, quick)` key. Each pool grows lazily up to `GRAPH_POOL_MAX_PER_KEY` (default 5) `TradingAgentsGraph` instances; `pool.acquire()` returns the first free one (or builds a new one outside the mutex if under cap, or blocks on `queue.Queue.get()` if at cap). Each instance is single-use-at-a-time because the graph mutates `self.ticker / self.curr_state` during `propagate()`, but the pool gives us both parallelism (different instances run concurrently) and caching (instances are returned to the pool on completion). Don't bypass `_get_or_create_pool`. Key-level LRU at `GRAPH_CACHE_MAX` (default 4) — evicting drops the entire pool for that key.
- **Concurrent updates** (`app.py`): `Application.builder().concurrent_updates(True)` is **load-bearing for cancellation**. With the default single-worker dispatcher, an in-flight analysis handler (awaiting `to_thread`) blocks the queue, so a Cancel-button update sits there until the analysis returns — exactly when we no longer need it. Don't drop this flag.
- **Per-step progress + cooperative cancel.** `progress.py:delegating_progress_callback` is a singleton `BaseCallbackHandler` attached to every graph via `callbacks=[...]` in the constructor. **`raise_error = True` on this class is load-bearing**: LangChain's callback manager swallows handler exceptions by default, defeating our raise-to-cancel strategy. With it enabled, raising `CancelledByUserError` from `_dispatch` aborts the about-to-fire LLM call and bubbles up through langgraph → `propagate()` → `to_thread`. TradingAgents passes our callbacks into the LLM kwargs, so we receive `on_chat_model_start` / `on_llm_start` events — **not** `on_chain_start`. LangGraph propagates the surrounding node name as `metadata["langgraph_node"]` on every nested LLM call. Per-run target (`ProgressReporter`) lives in `threading.local()` set by `run_trading_analysis` around `propagate()`. `ProgressReporter.cancel_event: threading.Event` is checked in `_dispatch` BEFORE every step update; the reporter also re-attaches the ❌ Cancel keyboard on every caption edit (Telegram drops `reply_markup` if not re-sent on `editMessageCaption`).
- **Cancel registry.** `chat_data["analysis_cancels"]` is `{message_id: threading.Event}` — populated by `_run_analysis_for_ticker` on entry, popped in `finally`. The Cancel button's `cancel_analysis:<message_id>` callback looks up the event and `set()`s it. Three race-close checks in the analysis path (post-`to_thread`, post-Telegraph publish, plus the in-pipeline raise) ensure a late tap still discards the result instead of overwriting "Cancelling…" with a success caption.
- **Ticker validation** (`validation.py:validate_ticker`): `_apply_add` calls this in parallel via `asyncio.gather` for each input token. yfinance's `Ticker(symbol).history(period="1d")` is the authoritative check — same source tradingagents uses. Class-share dot forms (`BRK.B`) are auto-rewritten to dash form (`BRK-B`) when the original lookup is empty. yfinance's stderr noise on misses is silenced via `logging.getLogger("yfinance").setLevel(CRITICAL)`. 5-min in-process TTL cache.
- **Watchlist UX (unified select-mode).** `/watch` always renders as a toggle keyboard (no separate "single-tap" vs "multi-select" mode). Selection state lives in `chat_data["watch_selection"]: set[str]`. The `runall:go` callback (Done button) reads the selection and dispatches:  1 ticker → `_run_analysis_for_ticker(...)` direct (uses cached graph from pool); 2+ tickers → `asyncio.gather(*runs)` for parallel runs. Stale `info:<ticker>` callbacks from older chat history still work via `_handle_info` — kept for back-compat but new renders don't generate them.
- **Telegraph publishing is offloaded.** `telegraph_client.publish_to_telegraph` wraps the SDK's blocking `create_page` in `asyncio.to_thread` so the event loop stays responsive during the network round-trip.
- **All Telegram messages use `parse_mode="MarkdownV2"`** consistently. Variable content goes through `telegram.helpers.escape_markdown(version=2)` (or sits inside `` `…` `` code spans which need no escaping). Don't mix legacy `Markdown` back in.
- **Callback dispatch** is prefix-based: `provider:`, `deep:`, `quick:`, `info:`, `multi:`, `runall:`, `del:`, `cancel:`, `cancel_analysis:`, `hist:`, `hist_t:`, `hist_back:`. Order matters where prefixes overlap (`cancel_analysis:` is checked before `cancel:`). Stay under Telegram's 64-byte `callback_data` limit.
- **Reply-driven `/add`.** `commands.ADD_PROMPT` is matched verbatim against `update.message.reply_to_message.text` so `add_via_reply` only fires on replies to our actual prompt, not random replies to bot messages.
- **Caption rendering.** `format_short_message` builds the post-analysis MarkdownV2 caption: signal emoji (`BUY` 🟢, `OVERWEIGHT` 🟩, `HOLD` 🟡, `UNDERWEIGHT` 🟥, `SELL` 🔴), ticker, signal verb, then a 2–3-sentence summary from `formatters.extract_summary(final_trade_decision, max_len=280)` (clips lead paragraph), then UTC timestamp + Telegraph link.
- **Telegraph CONTENT_TOO_BIG**: `format_analysis_result_markdown` emits only `final_trade_decision` + `trader_investment_plan`. Adding more `final_state` sections risks blowing the cap — truncate or drop sections, don't just append.
- **finviz cache-busting**: `chart.py:finviz_chart_url` appends `&_=<unix_ts>` so Telegram's CDN doesn't serve a stale cached photo.
- **History command** (`/history <ticker> [YYYY-MM-DD]`) reads tradingagents' on-disk JSON logs at `<results_dir>/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json`. `history.normalize_ticker` regex-validates `[A-Z0-9.\-]+` to block path traversal, and the date arg must round-trip through `date.fromisoformat` before joining the path. The date picker has a `← Back` button to return to the ticker picker; the rendered analysis view has a `← Back` to the date picker. Dispatched via the `hist_back:` prefix.

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
- `config_cmd`'s snapshot in `context.user_data["llm_snapshot"]` is not popped on the success path (only on Cancel); harmless but inconsistent.
- **Cancel during pool-wait isn't instant**: when a queue size exceeds `GRAPH_POOL_MAX_PER_KEY`, the overflow runs `queue.Queue.get()` for a free instance — `cancel_event` isn't checked there, so the run only aborts after acquiring an instance and reaching its first step boundary. For typical 1–5 ticker queues with default pool size 5 this never hits.
- **In-flight LLM call still completes after Cancel**: cancel is cooperative at LLM-call boundaries; we can't kill an HTTP request already on the wire. The current call's tokens are paid for, but no further steps run.
- **Build parallelism inside `_builder()`**: building N graphs simultaneously isn't N× faster — LangChain LLM client init and ChromaDB setup serialize partially on internal locks + GIL.
- `asyncio.to_thread` uses Python's default thread pool (`min(32, cpu_count + 4)`) — beyond that, parallel runs queue at the executor layer regardless of the graph pool size.
- No tests, no structured logging / correlation id, no graceful shutdown hooks.

## macOS .pth quirk

Python 3.14's `site.py` skips any `.pth` file marked with the macOS `UF_HIDDEN` filesystem flag. Files inside `~/Desktop` (especially with iCloud Desktop sync enabled) tend to inherit this flag. `pip install -e .` writes `__editable__.tg_bot-0.1.0.pth` which then gets ignored, so `python -m tg_bot` fails with `No module named tg_bot`.

Fix:
```bash
chflags nohidden .venv/lib/python3.14/site-packages/*.pth
```

Apply once after each fresh install. Docker (Linux) is unaffected.

## Recently fixed (May 2026)

- **`/status` command** + `analysis.pool_stats()` helper. Tracks uptime via `bot_data["start_time"]` (set in `_post_init`) and a global `bot_data["analysis_count"]` incremented at the top of every `_run_analysis_for_ticker` invocation. Surfaces graph pool stats (key count + total instances) for diagnosing pool exhaustion.
- **Mid-analysis cancellation.** Each progress message now carries a ❌ Cancel button. Tapping it sets a `threading.Event` checked at every LLM-call boundary in `progress._dispatch`; raising `CancelledByUserError` aborts the pipeline. Required `concurrent_updates=True` on the PTB Application (otherwise the cancel update queues behind the in-flight handler) and `raise_error = True` on the callback handler (otherwise LangChain swallows the exception). Multiple race-close checks in the analysis flow ensure a late tap discards the result.
- **Graph pool replaces single-instance cache.** `GraphPool` per `(provider, deep, quick)` allows N parallel runs to each hold their own instance (no lock contention) while still caching across runs. Builds happen outside the mutex so cold-start parallelism scales. Single-tap and queue paths both go through the pool.
- **Watchlist UX simplified to one mode.** `/watch` is always select-mode: tap to toggle (✅ prefix), `Done` runs the selected ticker(s). Internal logic decides cached graph (1 ticker) vs parallel via `asyncio.gather` (N tickers). Old "single-tap" + "multi-select toggle" duality is gone.
- **Per-ticker yfinance validation on `/add`.** `validation.validate_ticker` runs in parallel via `asyncio.gather` for each input. Class-share dot forms (`BRK.B`) auto-rewrite to dash form (`BRK-B`); invalid tickers report a hint instead of silently joining the watchlist. yfinance logger is silenced.
- **`/history` round-trip nav.** Date picker has `← Back` to the ticker picker; rendered analysis has `← Back` to the date picker. Dispatched via `hist_back:` prefix.
- **5-emoji signal map** in `format_short_message`: `BUY`, `OVERWEIGHT`, `HOLD`, `UNDERWEIGHT`, `SELL`. Caption now also includes a 2–3-sentence summary clipped from `final_trade_decision`'s lead paragraph (`extract_summary`, ~280 chars).
- **Cancel coverage extended** on every multi-step picker: `❌ Cancel` on `/config` provider keyboard (with snapshot/restore rollback), `/watch`, `/history` ticker + date pickers; `✅ Done` on `/del`.
- **`logger.exception` over `traceback.print_exc()`** in the analysis error path — uses the configured logging stack instead of writing raw to stderr.
- `_save` is now durable (fsync between write and rename), not just atomic. A crash between rename and OS flush can no longer leave an empty file.
- `analysis.py` no longer logs the entire `final_state` at INFO — that single line was tens of KB per run. Replaced with `Analysis complete for <ticker> — signal=<x>`; full state moved to `logger.debug`.
- Empty `ALLOWED_USER_IDS` logs at **WARNING** (was INFO), with an explicit "your LLM tokens are at risk" line so the open-bot risk surfaces in production.
- Per-step progress reporting (`progress.py`) hooked via LLM-level callback events with `metadata["langgraph_node"]` extraction. `on_chain_start` doesn't fire here because tradingagents passes our callbacks into LLM constructor kwargs.
- `tradingagents` URL in `pyproject.toml` is unpinned (HEAD-tracking). Daily GitHub Action cron checks for new upstream commits and only rebuilds when the SHA changes; manual dispatch forces rebuild.
- All Telegram messages are MarkdownV2 with proper escaping. Variable content runs through `telegram.helpers.escape_markdown(version=2)`.
- Telegraph publish failures surface as `"⚠️ Full report unavailable (Telegraph publish failed)."` in the caption instead of a silently missing link.
- `TradingAgentsGraph(debug=…)` reads from `Config.TA_DEBUG` (env: `TG_BOT_TA_DEBUG`); defaults to `False` for prod.
