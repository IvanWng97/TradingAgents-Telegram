# TradingAgents-Telegram — Architecture Reference

Telegram bot wrapping the [TradingAgents](https://github.com/TauricResearch/TradingAgents) library. Users curate a watchlist via Telegram, tap a ticker, and the bot runs `TradingAgentsGraph.propagate(...)` and posts a finviz chart + Telegraph link with the verdict. Per-step pipeline progress is streamed back into the message caption while the analysis runs.

## Layout

`src/tg_bot/` — package, organized as 4 subpackages + a small number of single-concept files at parent level. Each subpackage has a documented membership rule; the parent-level files are leaf primitives that don't fit into any single subpackage's concept.

```
src/tg_bot/
├── __init__.py
├── __main__.py                  # python -m tg_bot
├── app.py                       # PTB Application + BOT_COMMANDS + lifecycle hooks
├── config.py                    # env-driven Config (read once at process start)
├── auth.py                      # authorize TypeHandler (group=-1 gate)
├── validation.py                # yfinance ticker check + shared TICKER_RE
├── history.py                   # disk reader for past tradingagents logs
├── digest.py                    # /digest picker UI builder (no I/O, no globals)
│
├── storage/                     # JSON persistence — terminal in the dep DAG
│   ├── _base.py                 # JsonStorage (atomic + fsync)
│   ├── watchlist.py             # WatchlistStorage singleton
│   └── user_config.py           # UserConfigStorage singleton (digest schedule only)
│
├── handlers/                    # PTB handlers — register dispatch surfaces
│   ├── commands.py              # slash commands (/add, /watch, /list, etc.)
│   ├── callbacks.py             # prefix-dispatched callback handlers
│   ├── analysis_runner.py       # manual /watch + digest fan-out orchestration
│   └── pickers.py               # shared keyboard/response builders for commands+callbacks
│
├── pipeline/                    # LLM execution hot path
│   ├── analysis.py              # run_trading_analysis + GraphPool + .env overlay (build_config)
│   ├── cache.py                 # same-day result cache, keyed by AnalysisConfigKey
│   ├── config_key.py            # AnalysisConfigKey (frozen) — slug/caption/title
│   └── progress.py              # cancel-aware LangChain callback
│
└── rendering/                   # data → user-visible output
    ├── formatters.py            # HTML + MarkdownV2 captions (Invariant #4)
    ├── telegraph_client.py      # Telegraph publish (offloaded + resilient)
    └── chart.py                 # finviz URL builder
```

**Subpackage membership rules:**

- `storage/` — owns a `JsonStorage` subclass or its singleton. Terminal in the dependency DAG: imports no `tg_bot.*` modules upward.
- `handlers/` — registers PTB handlers (CommandHandler, CallbackQueryHandler, TypeHandler) OR is the direct callback implementation for a registered handler (e.g. `pickers.py` builds the keyboards that handlers return). NOT "anything called by a handler" — that's too broad and creates junk-drawer drift.
- `pipeline/` — in the hot path between "user taps ticker" and "LLM result ready." Modules that build/run/cache LangGraph propagation.
- `rendering/` — output is a string/bytes meant for Telegram or Telegraph. Pure transforms from data structures to wire format.

**Parent-level files are leaf primitives** — each is a single, self-documenting concept that doesn't sit naturally inside any subpackage:
- `app.py`, `__init__.py`, `__main__.py` — entry/wiring.
- `config.py` — env config primitive, consumed everywhere.
- `auth.py` — single TypeHandler function (registered in app.py); not a handler dispatcher in the `handlers/` sense.
- `validation.py` — yfinance + regex primitive; called by handlers AND by `history.py`'s path-traversal guard.
- `history.py` — disk reader; called by handlers and by analysis_runner for the `.md` export path.
- `digest.py` — pure picker UI builder (no I/O); called by `commands.py` and `callbacks.py`.

Two prior reorgs were considered and rejected: (1) creating a `bot/` or `telegram/` subpackage to absorb the parent-level leaves — every name was either stutter-y (`tg_bot.bot.auth`) or arbitrary (`infra/`, `support/`), and the architect+reviewer pass independently agreed the leaves don't share a real concept. (2) Pushing the leaves into `handlers/` — would widen `handlers/` from "registers handlers" to "anything called by a handler," which is the exact junk-drawer drift the membership rule prevents. The flat-leaves-at-parent layout is the explicit choice.

**Subpackage CLAUDE.md files** (Claude auto-loads them when working in those subtrees — subsystem-specific contracts live alongside the code, NOT here):

- `src/tg_bot/storage/CLAUDE.md` — storage singletons, atomic+fsync, `user_config.json` schema, corrupt-JSON recovery, multi-instance write caveat
- `src/tg_bot/handlers/CLAUDE.md` — commands surface + bot-internal mechanics, callback dispatch (textual if-elif chain), picker UX, cancel registry, digest fan-out + trampolining, `_try_acquire_nonblocking`, `/history` republish behavior
- `src/tg_bot/pipeline/CLAUDE.md` — `AnalysisConfigKey`, single lazy `GraphPool`, `.env` overlay (`build_config` + `_apply_local_effort_overlay`), cache hygiene + corrupt-file path, progress callback + `threading.local` blind spot, `from_config` effort first-truthy-wins
- `src/tg_bot/rendering/CLAUDE.md` — caption HTML, Telegraph publish + 4 resilience layers, 7-section packer, two sanitizers, `_normalize_nested_bullets` state machine, `escape_md_v2_url` carve-out, finviz defaults

Top-level: `pyproject.toml`, `Dockerfile`, `docker-compose.yml`, `.env`, `data/` (runtime state, gitignored), `docs/` (DEVELOPMENT / TROUBLESHOOTING / CONFIGURATION / MANUAL_INSTALL / TODO), `.github/workflows/` (lint + Docker build + CodeQL + on-demand Claude review).

## Architecture (for code reviewers)

This section gives reviewers — human or LLM — the structural context needed to spot cross-file regressions before subpackage CLAUDE.md files go deep on each subsystem.

### Request lifecycle (manual `/watch` tap)

1. Telegram delivers Update → PTB queue. `concurrent_updates=True` ensures cancel taps aren't queued behind in-flight analyses.
2. **Auth gate** (`auth.py:authorize`, group=-1) — drops Update if user not in `ALLOWED_USER_IDS`. Fail-closed when `effective_user` is missing and an allowlist is set.
3. **Handler dispatch** — commands match by name; callbacks dispatch by prefix (order-sensitive where overlapping: `cancel_analysis:` before `cancel:`, `digest_cancel:` before `digest:`).
4. Picker accumulates selection in `chat_data["watch_selection"]`; Done routes through `_handle_done` → `_run_analysis_for_ticker` per ticker.
5. **Cache lookup** (`cache.lookup(key, ticker, date_iso)`) where `key = AnalysisConfigKey.from_config(build_config())` — `build_config()` reads tradingagents' env-overlaid `DEFAULT_CONFIG` (single source of truth across all users) — hits short-circuit before semaphore acquire, reuse persisted `telegraph_url`, skip the progress flow.
6. **Cancel registry** — write `chat_data["analysis_cancels"][run_id] = {"cancel_event": threading.Event, "async_event": asyncio.Event, "message_id": None}` on entry (`message_id` filled after `send_photo`); pop in `finally`. Cancel callback is `cancel_analysis:<run_id>` (UUID, not message_id).
7. **Concurrency gate** — `asyncio.wait([sem.acquire(), cancel_async.wait()])` races slot acquisition vs queued-cancel. Above-cap users see `⏳ Queued`.
8. **GraphPool acquire** — single lazy `GraphPool` built once per process (config is process-wide, sourced from `.env`); its `max_size` equals `Config.MAX_CONCURRENT_ANALYSES`, matching the asyncio semaphore so the pool's blocking-queue branch is unreachable.
9. **`to_thread(propagate)`** — LangGraph runs; tradingagents threads our `BaseCallbackHandler` into LLM kwargs.
10. **Per-step progress** — `on_chat_model_start` → `ProgressReporter._dispatch` → `editMessageCaption` (re-attaches cancel keyboard; Telegram drops `reply_markup` otherwise). `cancel_event` is checked **before** every step; raising `CancelledByUserError` aborts the in-flight LLM call (requires `raise_error=True` on the handler — LangChain swallows handler exceptions by default).
11. **Race-close checks** — three: post-`to_thread`, post-Telegraph publish, before final caption edit. A late tap discards instead of overwriting.
12. **Telegraph publish** via `to_thread(edit_page)` if `/refresh` (uses cached URL's path), else `to_thread(create_page)`. Title comes from `AnalysisConfigKey.telegraph_title(ticker)` so the URL embeds the config.
13. **Cache store** (`cache.store(key, ticker, date, ...)`) — full `final_state` (LangChain messages coerced via `_json_default`); atomic write (tempfile + fsync + rename); stale-date siblings swept *after* the rename succeeds. The store no-ops when `telegraph_url` is falsy (cache-hygiene gate at the write site).
14. **Final caption edit** + pool release + cancel registry pop.

Digest fan-out (`/digest` JobQueue fire) diverges at steps 1+4: `run_user_digest` intersects `digest.tickers` with the live watchlist (auto-prunes), sends a header carrying `❌ Cancel digest` (one shared cancel_event for in-flight + `Task.cancel()` for pending), fans out via `_analyze_one_for_digest`. On `Forbidden` (user blocked the bot): auto-disable + cancel JobQueue job.

### State ownership

| State | Location | Lifetime | Persistence | Writer |
|---|---|---|---|---|
| Watchlist | `data/watchlist.json` | Forever | Atomic + fsync | `WatchlistStorage` |
| User config (digest only) | `data/user_config.json` | Forever | Atomic + fsync | `UserConfigStorage` |
| Active LLM config | `.env` (`TRADINGAGENTS_*` + per-provider effort overlay) → `build_config()` | Process (re-read on every call; effectively constant) | Memory | tradingagents `_apply_env_overrides` at import + local `_apply_local_effort_overlay` |
| Same-day cache entries | `data/result_cache/<slug>/<TICKER>/<date>.json` | Until next-date sweep | Atomic per-file + fsync | `cache.store` |
| Graph instance pool | `analysis._graph_pool` (single lazy `GraphPool`) | Process (built once, never evicted) | Memory | `analysis._get_or_create_pool` (double-checked locking) |
| Ticker validation cache | `validation._CACHE` | Process (5min TTL, 1024 FIFO) | Memory | `validate_ticker` |
| `chat_data["watch_selection"]` / `["watch_page"]` / `["watch_mode"]` | PTB chat memory | Until Done/Cancel | Memory | Picker entry + callbacks |
| `chat_data["analysis_cancels"]` | PTB chat memory | Per analysis | Memory | `_run_analysis_for_ticker` |
| `chat_data["digest_cancels"]` | PTB chat memory | Per fan-out | Memory | `run_user_digest` (keyed by digest header message_id) |
| `chat_data["digest_running:<uid>"]` | PTB chat memory | Per fan-out | Memory | `run-now` callback |
| `chat_data["digest_tickers_page"]` | PTB chat memory | Until picker close | Memory | `/digest` ticker picker pagination |
| `bot_data["start_time"]` / `["analysis_count"]` | PTB bot memory | Process | Memory | `_post_init` (start_time) / `_run_analysis_for_ticker` post-`send_photo` success (analysis_count) |
| TradingAgents disk logs | `TRADINGAGENTS_RESULTS_DIR/<TICKER>/…` | Forever | tradingagents-managed | tradingagents lib |
| TradingAgents data cache | `TRADINGAGENTS_CACHE_DIR/…` | Forever | tradingagents-managed | tradingagents lib |

Storage singletons (`watchlist_storage`, `user_config_storage`) are imported from `tg_bot.storage` — never construct your own.

### Data flow at a glance

Quick reference for tracing how data moves between modules. Detailed contracts live in the relevant subpackage CLAUDE.md; this is the map.

| Flow | Source → transform → sink |
|---|---|
| **Config identity** | `.env` (`TRADINGAGENTS_*` env vars) → tradingagents' `_apply_env_overrides` overlays onto `DEFAULT_CONFIG` at lib import + `_apply_local_effort_overlay` adds the per-provider effort knobs → `build_config()` (pipeline/analysis.py) returns a fresh copy → `AnalysisConfigKey.from_config(config)` (pipeline/config_key.py) → emits `slug()` for cache, `caption()` for caption "via" line, `telegraph_title(ticker)` for Telegraph page title. Identity is process-wide (one config across all users); the single lazy `GraphPool` is built from this once. **Invariant #1** keeps slug/caption/title aligned. |
| **Cache** | `cache.lookup(key, ticker, today_iso())` → on hit: render directly + skip LLM. On miss: run LLM → publish Telegraph → `cache.store(key, ticker, date, final_state, signal, telegraph_url)`. `store` enforces the hygiene gate (falsy URL → no-op). |
| **Ticker** | User text → `validate_ticker` (yfinance + `TICKER_RE` from validation.py — same regex `history.py` uses for path-traversal protection) → `watchlist_storage.add_ticker` (uppercase, sorted, atomic write). Picker reads watchlist → `chat_data["watch_selection"]` → `_run_analysis_for_ticker` per ticker. |
| **Cancel** | `cancel_analysis:<run_id>` callback → `chat_data["analysis_cancels"][run_id]["cancel_event"].set()` (threading) + `["async_event"].set()` (asyncio). Threading event is checked in `ProgressReporter._dispatch` before every LLM call (with `raise_error=True` on the handler to bubble up `CancelledByUserError`); asyncio event races `sem.acquire()` for queued runs. Three race-close checks in the analysis path discard late-arriving Cancel taps. Digest uses the same `cancel_event` field name in its own `digest_cancels` registry. |
| **Digest** | `user_config.json` (`digest: {enabled, hour_local, tz, chat_id, tickers}`) is the source of truth → `_post_init` rebuilds `JobQueue.run_daily` from storage on every startup → fires `run_user_digest` → intersects `digest.tickers` ∩ live watchlist → fans out via `_analyze_one_for_digest` (shares the global semaphore with manual `/watch`). |
| **Telegraph publish** | `final_state` → `format_analysis_result_markdown` (7-section packer, drops trailing sections until rendered HTML ≤ 40K) → `markdown.markdown(extensions=["tables"])` → `<img src=chart_url/>` prepended → `publish_to_telegraph(key.telegraph_title(ticker), html, edit_path=path_of_prior_url_or_None)`. The publish layer tries `edit_page` first if `edit_path` set (transient retry then None; non-transient → fall through to `create_page`); else `create_page` directly. |

### Cross-cutting invariants

These constraints span multiple files and aren't enforceable by any single test — verify before merging changes that touch the listed surfaces.

1. **Cache key tuple ⊃ AnalysisConfigKey.** Cache key = `(AnalysisConfigKey, ticker, date_iso)`. `AnalysisConfigKey` (in `pipeline/config_key.py`) is the **single source of truth** for the three surfaces that identify a config: `slug()` → cache directory, `caption()` → caption "via" line, `telegraph_title(ticker)` → Telegraph page title. The graph pool is a single lazy instance (config is process-wide via `.env`), so adding a knob is a three-edit operation: (a) add the field to `AnalysisConfigKey`, (b) update `from_config` to populate it from the resolved tradingagents config — if the knob is provider-specific (like the effort level today), also register the per-provider key name in `pipeline/analysis.py:EFFORT_KEY_BY_PROVIDER` so `from_config` can resolve it across providers (a uniformly-named knob skips this sub-step), (c) update `slug()` / `caption()` / `telegraph_title()` to emit the field where it should appear. New behavior needs a scenario in `tests/test_config_key.py` (see `tests/CLAUDE.md`). If you add a field and forget any emitter, the slug/caption/title tests fail loudly. If you ever re-introduce multi-config support, the pool collapses back to a dict keyed on `AnalysisConfigKey` — the dataclass is already hashable for this.
2. **`concurrent_updates=True` + `raise_error=True` are co-load-bearing for cancellation.** Without `concurrent_updates`, the cancel-button update queues behind the in-flight analysis. Without `raise_error` on the progress callback, LangChain swallows `CancelledByUserError`. Either alone breaks cancel.
3. **In-flight `editMessageCaption` must re-attach `reply_markup`.** Telegram drops the cancel keyboard when not re-sent. `ProgressReporter` re-attaches on every per-step caption edit so the ❌ Cancel button stays alive across the analysis. The cancel-ack edit (`_queued_cancel_edit`) intentionally passes `reply_markup=None` to drop the button once cancellation is acknowledged — that's the one path that *should* clear it.
4. **Two parse_modes — MarkdownV2 (default) and HTML (analysis-output captions only).**
   - **MarkdownV2** for everything except the analysis-output captions: `/help`, `/status`, `/digest` pickers, `/history` ticker/date pickers, transient analysis captions (`Analyzing…`/`Queued`/`Cancelling`), error messages. Variable content → `escape_markdown(version=2)`; URL link targets in `[text](url)` → `rendering/formatters.escape_md_v2_url`; code spans → no escape.
   - **HTML** for the three analysis-output surfaces: `format_short_message` (final `/watch` caption + cache-hit caption), `commands.build_history_response` (`/history` republish caption), and the digest progress/summary header. These render LLM-produced markdown (`**bold**`, `## headers`, `[links](url)`) via `markdown_to_telegram_html` so the inline formatting actually renders instead of escaping to literal `**bold**`. The summary content is wrapped in `<blockquote expandable>` for the collapse/expand affordance, which only exists in HTML mode. Variable content → `html.escape`; URL hrefs → `html.escape(url, quote=True)`; LLM markdown → `markdown_to_telegram_html` (runs `markdown.markdown` then `sanitize_html_for_telegram` to whitelist Telegram's allowed tag set and drop tables/imgs entirely so they don't blow the 1024-char caption budget).
   - Mixing — passing MarkdownV2-escaped text with `parse_mode="HTML"` or vice versa — produces broken renders or `Bad Request: can't parse entities` at runtime. Per-message `parse_mode` is the single source of truth for which escape rule applies.
5. **Cancel registry race-close.** Two analysis paths exist and check cancellation differently:
   - **Manual `/watch` (`_run_analysis_for_ticker`)**: two flag-checks (post-`to_thread` + post-Telegraph publish) plus one exception boundary (`CancelledByUserError` raised from `ProgressReporter._dispatch` and caught in the analysis handler). Three race-closes total.
   - **Digest fan-out (`_analyze_one_for_digest`)**: same exception boundary, but the second flag-check is **pre-publish** rather than post-publish (the path raises `CancelledByUserError` before Telegraph is touched if cancel fires while `to_thread(propagate)` was on the wire). A separate post-completion check lives in the fan-out wrapper `_wrapped` inside `run_user_digest` — it discards the row's result if `cancel_event` is set after `_analyze_one_for_digest` returns.
   New analysis paths must replicate the manual or digest pattern explicitly; don't mix them. Updating either path's cancellation surface means updating this invariant in lockstep.
6. **`final_state` persists whole.** Cache stores the full dict; `_json_default` coerces LangChain messages through `model_dump → dict → {__type__, content} → repr`. New nested object types in `final_state` must survive that fallback chain or the write fails (and the tempfile is `unlink`'d, not retained).
7. **Watchlist pickers share `chat_data["watch_*"]` across modes.** `/watch` and `/refresh` both populate `watch_selection` / `watch_page` / `watch_mode`. `watch_mode` is set by the entry command, threaded through every re-render handler, popped on Done. New picker variants must follow the same shape.
8. **Storage mutations go through `_save_async`.** Atomic + fsync via tempfile + `os.replace`. Bypassing risks mid-write corruption on crash.
9. **Digest schedule: storage is source of truth.** `JobQueue` is rebuilt from `user_config.json` at every `_post_init`. Don't add jobs without persisting the schedule first, or a restart silently drops them.
10. **Ticker storage normalization.** Watchlists are uppercase + sorted on read; `validate_ticker` rewrites class-share dot forms (`BRK.B → BRK-B`). Lookups against storage must `.upper()` first.

## Run / deploy

| | Command |
|---|---|
| Install dev | `pip install -e .` (macOS: see `docs/DEVELOPMENT.md` for the `.pth` flag fix) |
| Run locally | `python -m tg_bot` (CWD must contain `.env` and `data/`) |
| Build & deploy | `docker-compose up -d --build` |
| Update | `git pull && docker-compose up -d --build` (data/ persists via bind mount) |

`TG_BOT_DATA_DIR` env var overrides the default `data/` path.

## Key contracts (parent-level leaves)

Subpackage-specific contracts live alongside the code in `src/tg_bot/<subpkg>/CLAUDE.md`. The bullets below cover only the parent-level leaf files whose contracts don't fit any subpackage.

- **Auth gate** (`auth.py:authorize`) runs at `group=-1` for every Update; raises `ApplicationHandlerStop` for users not in `Config.ALLOWED_USER_IDS`. Empty list = open to all and is logged at WARNING level on startup. Updates without an `effective_user` (channel_post / my_chat_member / some inline_query variants) fail closed when an allowlist is set, pass through when it's empty.
- **Concurrent updates** (`app.py`): `Application.builder().concurrent_updates(True)` is **load-bearing for cancellation** (Invariant #2). With the default single-worker dispatcher, an in-flight analysis handler (awaiting `to_thread`) blocks the queue, so a Cancel-button update sits there until the analysis returns — exactly when we no longer need it. Don't drop this flag.
- **Ticker validation** (`validation.py:validate_ticker`): `_apply_add` calls this in parallel via `asyncio.gather` for each input token. yfinance's `Ticker(symbol).history(period="1d")` is the authoritative check — same source tradingagents uses. Class-share dot forms (`BRK.B`) are auto-rewritten to dash form (`BRK-B`) when the original lookup is empty. yfinance's stderr noise on misses is silenced via `logging.getLogger("yfinance").setLevel(CRITICAL)`. 5-min in-process TTL cache, FIFO-evicted at 1024 entries so an open bot can't be OOM'd via `/add JUNK1 JUNK2 …`.
- **History command** (`/history <ticker> [YYYY-MM-DD]`) reads tradingagents' on-disk JSON logs at `<results_dir>/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json`. `history.normalize_ticker` regex-validates `^[A-Z0-9]+(?:[.\-][A-Z0-9]+)*$` to block path traversal — alnum tokens separated by single `.` or `-` only, so `..`, `.A`, `A.`, `A..B` are all rejected. The earlier `[A-Z0-9.\-]+` accepted `..` because `.` is a literal inside a character class. The date arg must round-trip through `date.fromisoformat` before joining the path. The date picker has a `← Back` button to return to the ticker picker; the rendered analysis view has a `← Back` to the date picker. Dispatched via the `hist_back:` prefix.

## Environment variables

```
TELEGRAM_BOT_TOKEN=<required>
TELEGRAPH_ACCESS_TOKEN=<required for Telegraph publishing>
ALLOWED_USER_IDS=123,456              # empty = open (logged at WARNING)
TG_BOT_DATA_DIR=data                  # default
TG_BOT_TA_DEBUG=                      # set "1"/"true" to enable TradingAgentsGraph(debug=True)
TG_BOT_MAX_CONCURRENT_ANALYSES=3      # bot-wide concurrency cap; runs above this show "⏳ Queued" until a slot frees up. Also sizes the graph instance pool.
# Active LLM config — process-wide via tradingagents' _ENV_OVERRIDES at lib import.
TRADINGAGENTS_LLM_PROVIDER=deepseek
TRADINGAGENTS_DEEP_THINK_LLM=deepseek-v4-pro
TRADINGAGENTS_QUICK_THINK_LLM=deepseek-v4-flash
TRADINGAGENTS_MAX_DEBATE_ROUNDS=1     # optional; 1/2/3
# Per-provider thinking-effort knob — local overlay (upstream doesn't expose these).
# Derived from `EFFORT_KEY_BY_PROVIDER`: add a provider there → env var name follows.
TRADINGAGENTS_OPENAI_REASONING_EFFORT=    # low/medium/high; only applies when provider=openai
TRADINGAGENTS_ANTHROPIC_EFFORT=           # low/medium/high; anthropic only
TRADINGAGENTS_GOOGLE_THINKING_LEVEL=      # low/medium/high; google only
TRADINGAGENTS_RESULTS_DIR=...         # /history reads from here; defaults to ~/.tradingagents/logs
TRADINGAGENTS_CACHE_DIR=...           # tradingagents data cache; defaults to ~/.tradingagents/cache
# Plus provider keys for the chosen TRADINGAGENTS_LLM_PROVIDER: OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / etc.
```

**Docker persistence caveat.** `TRADINGAGENTS_RESULTS_DIR` and `TRADINGAGENTS_CACHE_DIR` default to paths under `~/.tradingagents`, which is ephemeral inside the container. To persist `/history` data and avoid re-fetching yfinance on every restart, set them to paths under `/app/data/` (which is bind-mounted) — for example `TRADINGAGENTS_RESULTS_DIR=/app/data/ta-logs`.

## Workflow when making changes

A change in this repo usually touches more than just code — these surfaces drift in parallel and there's no automated check that catches the drift, so verify each pass before declaring done.

1. **Verify the code works.** `.venv/bin/python -m ruff check src/ tests/ && ruff format --check` and `.venv/bin/python -m pytest tests/` (CI runs the same command via `.github/workflows/smoke.yml`). See `tests/CLAUDE.md` for fixture conventions (`FakeBot` / `FakeContext` / `set_smoke_data_dir`), the `TG_BOT_DATA_DIR` module-import-time gotcha, conftest setup, and the "new behavior needs a scenario" discipline rule. For UI-affecting changes, restart the bot and exercise in Telegram — tests verify code correctness, not feature correctness.
2. **If user-visible behavior changed**, sync four places that drift independently:
   - `README.md` — Commands table + Features bullets.
   - Root `CLAUDE.md` (Architecture / Invariants / Recently fixed) AND the relevant nested `src/tg_bot/<subpkg>/CLAUDE.md` (Commands surface lives in `handlers/CLAUDE.md`; subsystem-specific contracts live with the subsystem). Subsystem changes touch the nested file; cross-cutting changes touch root.
   - `src/tg_bot/handlers/commands.py:start` — onboarding nudge text.
   - `src/tg_bot/handlers/commands.py:help_cmd` and `app.py:BOT_COMMANDS` — slash-menu copy + the canonical command list.
3. **If env-var surface changed**, sync `.env.example`, `docs/CONFIGURATION.md`, and `start.sh`. Provider keys also need a row in `pipeline/analysis.py:PROVIDER_ENV_KEYS` so the LLM precheck can flag a missing key. New per-provider effort knobs go in `EFFORT_KEY_BY_PROVIDER` (auto-derives the env-var name); see `tests/test_pipeline_analysis.py` for the round-trip pin.
4. **If a handler contract, dataflow, or command surface changed**, test coverage moves in lockstep — treat "no scenario" as a merge blocker. Discipline details (per-PR pattern, fixture-update vs. new-scenario decision, drift audit cadence) live in `tests/CLAUDE.md`.
5. **If any `CLAUDE.md` (root or nested) is touched**, run a Discovery → Quality Assessment → Targeted Updates audit before merge — review all sibling CLAUDE.md files for cross-drift, scope creep, and bullets that grew past their useful density. The `claude-md-management` Claude Code plugin automates this if installed; otherwise do the four phases manually: (a) find every CLAUDE.md, (b) score each for bullet density (>3 lines per concept = extract), cross-drift (same fact stated in >1 file), and scope creep (implementation detail vs. architecture signal), (c) write a per-file report, (d) apply targeted diffs. Don't hand-edit a CLAUDE.md and skip the audit pass.
6. **Commit messages must end with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`** — the system prompt mandates this and the GitHub UI uses it to render Claude as a co-author.

## Conventions

- Don't reintroduce a `utils.py` junk drawer. New helpers go in a focused module.
- Storage mutations must use the existing `_save` / `_save_async` (atomic + fsync). New storage classes inherit `JsonStorage`.
- Blocking calls (`telegraph.create_page`, `propagate`, file I/O) belong inside `asyncio.to_thread` — PTB runs handlers on one event loop.
- Per-message `parse_mode` is the single source of truth for escaping — see Invariant #4 for the MarkdownV2 vs. HTML carve-out. Variable content under MarkdownV2 → `escape_markdown(version=2)` (including inside code spans — backticks in values would break the span). Under HTML → `html.escape`.
- Don't bake `.env` into the Docker image; `.dockerignore` excludes it. Compose loads it via `env_file:`.
- TradingAgents is a pip dep (`tradingagents @ git+...` in `pyproject.toml`); no sys.path hacks.

## CI/CD

`.github/workflows/`:

- `lint.yml` — Ruff.
- `smoke.yml` — `pip install -e ".[dev]"` + `pytest tests/ -v --tb=short --junitxml=junit.xml` on every PR + push to main; uploads `junit.xml` as a 7-day-retention artifact.
- `codeql.yml` — `security-extended` Python, weekly cron.
- `claude.yml` — on-demand `@claude` review in PR threads.
- `claude-review.yml` — Claude review on every PR `opened` / `reopened` / `ready_for_review` / `synchronize`; skips dependabot only. `cancel-in-progress: true` coalesces push bursts — in-flight reviews are cancelled by each new push, so a burst of pushes produces one review at the end of the quiescent burst. Use `/claude-review` to re-trigger manually.
- `drift-audit.yml` — weekly Mondays 09:00 UTC. Explore-style audit of test coverage vs. CLAUDE.md invariants + handler surface; opens a `drift-audit` issue only when findings exist.
- `upstream-tag-watch.yml` — daily. Detects new `TauricResearch/TradingAgents` releases via the Releases API (36h window + dedup by issue title), fires Claude to audit each new tag against the bot's integration surface, opens an `upstream-audit` issue per release.
- `docker-build.yml` — dual-pushes multi-arch images to Docker Hub + GHCR. Daily cron resolves upstream `tradingagents` HEAD via `git ls-remote` and skips the build when the SHA hasn't changed.
- `dependabot.yml` — weekly bumps.

## Known limitations

- **LLM config is process-wide**: every user of the bot shares the active `TRADINGAGENTS_LLM_PROVIDER` + deep / quick / rounds / effort set in `.env`. Multi-tenant per-user config is intentionally not supported — bot is single-operator by design. Re-introducing it means resurrecting `build_user_config` + `UserConfigStorage`'s LLM keys + a `/config` picker, and collapsing the single lazy `GraphPool` back to the per-`AnalysisConfigKey` dict-of-pools with the LRU floor; the dataclass + cache key shapes already support this.
- **LLM config changes require a bot restart** — `DEFAULT_CONFIG` is read once at tradingagents-lib import time via `_apply_env_overrides`; editing `.env` does NOT hot-swap the active config.
- `send_photo` / Telegraph publish have no explicit timeouts; if finviz or Telegraph hangs, PTB's defaults apply (~5s) and the user sees no progress beyond the "Analyzing…" caption.
- `Config.TA_DEBUG` is read once at process start; toggling `TG_BOT_TA_DEBUG` requires a bot restart (and would still only affect graphs built *after* the restart since cached entries carry their `debug` flag from init time).
- **In-flight LLM call still completes after Cancel**: cancel is cooperative at LLM-call boundaries; we can't kill an HTTP request already on the wire. The current call's tokens are paid for, but no further steps run.
- **Build parallelism inside `_builder()`**: building N graphs simultaneously isn't N× faster — LangChain LLM client init and ChromaDB setup serialize partially on internal locks + GIL.
- `asyncio.to_thread` uses Python's default thread pool (`min(32, cpu_count + 4)`) — beyond that, parallel runs queue at the executor layer regardless of the graph pool size.
- No structured logging / correlation id. Graceful shutdown is wired (`_post_stop` signals every in-flight cancel + 2s drain).
- **`_try_acquire_nonblocking` (`handlers/analysis_runner.py:153-176`) reads CPython-private `sem._value` / `sem._waiters`**. The `AttributeError` fallback degrades to blocking-acquire, so a future CPython release reshaping these attributes won't crash — but every run will appear ⏳ Queued even when slots are free. Symptom: elevated queue-time metrics with no error log. Watch for this on Python version bumps; fix is to either follow the upstream attribute rename or rebuild the helper on `asyncio.Semaphore.locked()` + a counter.
- **`run_user_digest` `status` dict relies on cooperative asyncio scheduling**. `status` is a plain dict mutated from concurrent tasks (`_wrapped` per ticker, `_on_step` callbacks, `_render` reads). Safe today because all mutations happen at `await` boundaries on the single event loop — but if anyone ever refactors the digest path to use `asyncio.to_thread` for status updates, mutations would interleave without a lock and we'd silently lose rows. Document boundary, not bug: if you add thread-offload to the digest fan-out, add an `asyncio.Lock` around `status` writes first.

## Recently fixed

Curated narrative for the latest non-trivial PR. Older entries graduate into the body sections above (Architecture / Key contracts (parent-level leaves) / nested `src/tg_bot/*/CLAUDE.md` / Known limitations) on the next PR cycle — git log carries the full history.

- **Config flow refactor: `/config` command removed; `.env` is now the single source of truth for LLM config.** Previously: each user picked provider + deep + quick + rounds + effort via a five-step `/config` picker; settings stored in `data/user_config.json` per user; `build_user_config(user_id)` resolved them at run time; `_graph_pool` was a dict-of-pools keyed on `AnalysisConfigKey` with an LRU floor at `GRAPH_CACHE_MAX = max(8, 2 * MAX_CONCURRENT_ANALYSES)` to absorb per-user config churn. Now: the active config is read from `.env` via tradingagents' upstream `_apply_env_overrides` (provider / deep / quick / rounds via `TRADINGAGENTS_LLM_PROVIDER` / `TRADINGAGENTS_DEEP_THINK_LLM` / `TRADINGAGENTS_QUICK_THINK_LLM` / `TRADINGAGENTS_MAX_DEBATE_ROUNDS`) plus a small local `_apply_local_effort_overlay` for the per-provider effort knobs upstream doesn't expose (`TRADINGAGENTS_OPENAI_REASONING_EFFORT` / `TRADINGAGENTS_ANTHROPIC_EFFORT` / `TRADINGAGENTS_GOOGLE_THINKING_LEVEL`). `build_config()` returns a fresh copy of the env-overlaid `DEFAULT_CONFIG`; `_graph_pool` collapses to a single lazy `GraphPool` (double-checked locking) since only one config exists per process; cache slug + Telegraph title + "via" caption still flow through `AnalysisConfigKey.from_config(build_config())`. `UserConfigStorage` is stripped to digest-only. `/status` reads from `build_config()`; startup logs a WARNING via `check_llm_configured()` if the configured provider's API key is missing. Pinning: 17 new scenarios in `tests/test_pipeline_analysis.py` (env-overlay round-trip, `build_config` fresh-copy + populated keys, `check_llm_configured` per-provider + Ollama edge case, upstream `PROVIDER_ENV_KEYS` alignment) plus a cross-provider first-truthy-wins effort-resolution pin in `tests/test_config_key.py`. See `start.sh` for the canonical first-catalog-entry model writes per provider (now includes `qwen-cn` / `glm-cn` / `minimax` / `minimax-cn` and the `ZHIPUAI_API_KEY` → `ZHIPU_API_KEY` rename from upstream v0.2.5).
