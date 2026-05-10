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
├── digest.py              # /digest picker rendering: tz/hour grids + status line + next_fire math
├── cache.py               # same-day result cache: skip the LLM run on a tap-tap-tap
├── history.py             # disk-readers for past analyses (/history)
├── telegraph_client.py    # sanitize + publish (Telegraph instance is lazy)
├── validation.py          # yfinance-backed validate_ticker + class-share rewrite
├── handlers/
│   ├── commands.py        # /start /help /add /del /watch /list /config /digest /history /status
│   └── callbacks.py       # inline-button dispatcher (prefix-based)
└── storage/
    ├── _base.py           # JsonStorage (atomic + fsync writes, async wrapper)
    ├── watchlist.py       # WatchlistStorage(JsonStorage)
    ├── user_config.py     # UserConfigStorage(JsonStorage)
    └── __init__.py        # exports process-wide singletons
```

Top-level: `pyproject.toml` (deps), `Dockerfile`, `docker-compose.yml`, `.env`, `data/` (runtime state, gitignored), `docs/` (TROUBLESHOOTING / DEVELOPMENT / TODO — the README delegates to these), `.github/workflows/` (lint + Docker build with SHA-check skip + CodeQL + on-demand Claude review).

## Architecture (for code reviewers)

This section gives reviewers — human or LLM — the structural context needed to spot cross-file regressions before "Key contracts" goes deep on each subsystem.

### Request lifecycle (manual `/watch` tap)

1. Telegram delivers Update → PTB queue. `concurrent_updates=True` ensures cancel taps aren't queued behind in-flight analyses.
2. **Auth gate** (`auth.py:authorize`, group=-1) — drops Update if user not in `ALLOWED_USER_IDS`. Fail-closed when `effective_user` is missing and an allowlist is set.
3. **Handler dispatch** — commands match by name; callbacks dispatch by prefix (order-sensitive where overlapping: `cancel_analysis:` before `cancel:`, `digest_cancel:` before `digest:`).
4. Picker accumulates selection in `chat_data["watch_selection"]`; Done routes through `_handle_done` → `_run_analysis_for_ticker` per ticker.
5. **Cache lookup** (`cache.lookup`) keyed on `(provider, deep, quick, ticker, date_iso, rounds, effort)` — hits short-circuit before semaphore acquire, reuse persisted `telegraph_url`, skip the progress flow.
6. **Cancel registry** — write `chat_data["analysis_cancels"][run_id] = {"event": threading.Event, "async_event": asyncio.Event, "message_id": None}` on entry (`message_id` filled after `send_photo`); pop in `finally`. Cancel callback is `cancel_analysis:<run_id>` (UUID, not message_id).
7. **Concurrency gate** — `asyncio.wait([sem.acquire(), cancel_async.wait()])` races slot acquisition vs queued-cancel. Above-cap users see `⏳ Queued`.
8. **GraphPool acquire** — keyed on `(provider, deep, quick, rounds, effort)`; per-key cap matches the semaphore so the pool's blocking-queue branch is unreachable.
9. **`to_thread(propagate)`** — LangGraph runs; tradingagents threads our `BaseCallbackHandler` into LLM kwargs.
10. **Per-step progress** — `on_chat_model_start` → `ProgressReporter._dispatch` → `editMessageCaption` (re-attaches cancel keyboard; Telegram drops `reply_markup` otherwise). `cancel_event` is checked **before** every step; raising `CancelledByUserError` aborts the in-flight LLM call (requires `raise_error=True` on the handler — LangChain swallows handler exceptions by default).
11. **Race-close checks** — three: post-`to_thread`, post-Telegraph publish, before final caption edit. A late tap discards instead of overwriting.
12. **Cache store** (`cache.store`) — full `final_state` (LangChain messages coerced via `_json_default`); atomic write (tempfile + fsync + rename); sweep stale-date siblings for the same `(config, ticker)`.
13. **Telegraph publish** via `to_thread(create_page)`.
14. **Final caption edit** + pool release + cancel registry pop.

Digest fan-out (`/digest` JobQueue fire) diverges at steps 1+4: `run_user_digest` intersects `digest.tickers` with the live watchlist (auto-prunes), sends a header carrying `❌ Cancel digest` (one shared cancel_event for in-flight + `Task.cancel()` for pending), fans out via `_analyze_one_for_digest`. On `Forbidden` (user blocked the bot): auto-disable + cancel JobQueue job.

### State ownership

| State | Location | Lifetime | Persistence | Writer |
|---|---|---|---|---|
| Watchlist | `data/watchlist.json` | Forever | Atomic + fsync | `WatchlistStorage` |
| User config (LLM + digest) | `data/user_config.json` | Forever | Atomic + fsync | `UserConfigStorage` |
| Same-day cache entries | `data/result_cache/<slug>/<TICKER>/<date>.json` | Until next-date sweep | Atomic per-file + fsync | `cache.store` |
| Graph instance pool | `analysis._graph_pool` | Process (LRU at `GRAPH_CACHE_MAX`) | Memory | `analysis.acquire` |
| Ticker validation cache | `validation._CACHE` | Process (5min TTL, 1024 FIFO) | Memory | `validate_ticker` |
| `chat_data["watch_selection"]` / `["watch_page"]` / `["watch_mode"]` | PTB chat memory | Until Done/Cancel | Memory | Picker entry + callbacks |
| `chat_data["analysis_cancels"]` | PTB chat memory | Per analysis | Memory | `_run_analysis_for_ticker` |
| `chat_data["digest_running:<uid>"]` | PTB chat memory | Per fan-out | Memory | `run-now` callback |
| `bot_data["start_time"]` / `["analysis_count"]` | PTB bot memory | Process | Memory | `_post_init` / analysis entry |
| TradingAgents disk logs | `TRADINGAGENTS_RESULTS_DIR/<TICKER>/…` | Forever | tradingagents-managed | tradingagents lib |
| TradingAgents data cache | `TRADINGAGENTS_CACHE_DIR/…` | Forever | tradingagents-managed | tradingagents lib |

Storage singletons (`watchlist_storage`, `user_config_storage`) are imported from `tg_bot.storage` — never construct your own.

### Cross-cutting invariants

These constraints span multiple files and aren't enforceable by any single test — verify before merging changes that touch the listed surfaces.

1. **Cache key tuple ⊃ GraphPool key tuple.** Cache key = `(provider, deep, quick, ticker, date, rounds, effort)`; pool key = `(provider, deep, quick, rounds, effort)`. They share the config quintuple — adding a graph-baked knob requires extending **both** keys (and the cache slug in `cache._slug`), or two users on different new-knob values silently share an instance configured wrong. Reviewers have missed this; check both at once.
2. **`concurrent_updates=True` + `raise_error=True` are co-load-bearing for cancellation.** Without `concurrent_updates`, the cancel-button update queues behind the in-flight analysis. Without `raise_error` on the progress callback, LangChain swallows `CancelledByUserError`. Either alone breaks cancel.
3. **`editMessageCaption` must re-attach `reply_markup`.** Telegram drops the cancel keyboard when not re-sent. `ProgressReporter` re-attaches on every caption edit — new edit paths must do the same.
4. **MarkdownV2 escaping discipline.** Variable content → `escape_markdown(version=2)`. URL link targets in `[text](url)` → `formatters.escape_md_v2_url`. Code spans → no escape. Mixing produces broken renders or `Bad Request: can't parse entities` at runtime.
5. **Cancel registry race-close.** Three checks (post-`to_thread`, post-Telegraph, pre-final-edit) ensure a late tap discards rather than overwriting "Cancelling…" with success. New analysis paths must replicate the pattern.
6. **`final_state` persists whole.** Cache stores the full dict; `_json_default` coerces LangChain messages through `model_dump → dict → {__type__, content} → repr`. New nested object types in `final_state` must survive that fallback chain or the write fails (and the tempfile is `unlink`'d, not retained).
7. **Watchlist pickers share `chat_data["watch_*"]` across modes.** `/watch` and `/refresh` both populate `watch_selection` / `watch_page` / `watch_mode`. `watch_mode` is set by the entry command, threaded through every re-render handler, popped on Done. New picker variants must follow the same shape.
8. **Storage mutations go through `_save_async`.** Atomic + fsync via tempfile + `os.replace`. Bypassing risks mid-write corruption on crash.
9. **Digest schedule: storage is source of truth.** `JobQueue` is rebuilt from `user_config.json` at every `_post_init`. Don't add jobs without persisting the schedule first, or a restart silently drops them.
10. **Ticker storage normalization.** Watchlists are uppercase + sorted on read; `validate_ticker` rewrites class-share dot forms (`BRK.B → BRK-B`). Lookups against storage must `.upper()` first.

## Run / deploy

| | Command |
|---|---|
| Install dev | `pip install -e .` (then `chflags nohidden .venv/lib/python3.14/site-packages/*.pth` once on macOS — see "macOS .pth quirk" below) |
| Run locally | `python -m tg_bot` (CWD must contain `.env` and `data/`) |
| Build & deploy | `docker-compose up -d --build` |
| Update | `git pull && docker-compose up -d --build` (data/ persists via bind mount) |

`TG_BOT_DATA_DIR` env var overrides the default `data/` path.

## Commands

| Command | Behavior |
|---|---|
| `/start`, `/help` | Welcome / help text |
| `/add NVDA AAPL` | Bulk-add tickers. Each is yfinance-validated in parallel via `validation.validate_ticker`; class-share dot forms (`BRK.B`) auto-correct to dash form (`BRK-B`); invalid symbols are rejected with a hint. |
| `/add` (no args) | Prompts with `ForceReply`; reply text is parsed as ticker(s) by `add_via_reply` (also validated). |
| `/del NVDA AAPL` | Bulk-remove |
| `/del` (no args) | Inline-button picker; each ❌ tap removes immediately, `✅ Done` closes the picker |
| `/watch`, `/list` | Paginated select-mode watchlist (`WATCHLIST_PAGE_SIZE = 9`, 3×3 grid). Tap any ticker → ✅ prefix and `Done (N)` counter increments. `✓ Select all` / `✗ Clear` are bulk actions across ALL pages. `← Prev` / `Next →` step pages — selection persists across pages. `✅ Done` runs the selected ticker(s); `❌ Cancel` dismisses. Single ticker uses the cached graph (cheap init); 2+ tickers run in parallel via `asyncio.gather`, each pulling its own graph instance from the pool. |
| `/config` | Five-step flow: provider → deep model → quick model → debate rounds (1/2/3) → reasoning effort (default/low/medium/high). Effort step is skipped for providers without a thinking knob (deepseek/qwen/glm/ollama/xai). Each step has `❌ Cancel` that restores a snapshot of the prior `(provider, deep, quick, rounds, effort)` quintuple |
| `/digest` | Single-screen picker for a daily watchlist run. First-time users land on the tz picker (10 IANA zones); returning users see a 6×4 hour grid with ✅ on the active hour. `🌍 Time zone` swaps to the tz picker, `📋 Tickers (N/M)` swaps to a paginated multi-select filter (per-tap save, ✓ Select all / ✗ Clear), `▶ Run now` triggers an immediate fan-out, `🔕 Off` disables (preserves hour + tz + tickers for one-tap re-enable). Hour selection captures `chat_id` from the live update so the JobQueue callback can send unsolicited messages later. New users start with `tickers=[]` (must opt in); legacy enabled-digest users get backfilled to their full watchlist on first `_post_init` after upgrade. |
| `/history` (no args) | Inline picker of all tickers with saved history, with `❌ Cancel` to dismiss |
| `/history NVDA` | Inline picker of recent analysis dates with `← Back` (returns to ticker picker) and `❌ Cancel` |
| `/history NVDA 2026-04-15` | Direct lookup by date — publishes the saved analysis to Telegraph (final view also has `← Back` to the date picker) |
| `/refresh NVDA` | Direct fast-path: drop today's cached result for `NVDA` (current user's full cache key) and run a fresh analysis. |
| `/refresh` (no args) | Renders the same paginated multi-select picker as `/watch`, with `🔄 Refresh (N)` as the Done button. Tapping Done invalidates today's cache for each selected ticker before launching `_run_analysis_for_ticker`, so the analyses all miss the cache and pay for fresh LLM runs. Mirrors the `/del NVDA` vs `/del` two-form pattern. |
| `/status` | Diagnostic snapshot — process uptime, total analyses run since boot (`bot_data["analysis_count"]`), graph pool size from `analysis.pool_stats()`, and the requesting user's `(provider, deep, quick)` LLM config |

`set_my_commands` in `app.py:_post_init` exposes these as Telegram's native Menu button + `/`-autocomplete. `_post_init` also stamps `bot_data["start_time"] = time.time()` so `/status` can compute uptime.

## Key contracts

- **Storage singletons** live in `tg_bot.storage` — both handler modules import the same `watchlist_storage` / `user_config_storage` so writes are immediately visible everywhere. Don't construct your own `WatchlistStorage()` / `UserConfigStorage()` in handlers.
- **Watchlists are stored sorted.** `WatchlistStorage._load` sorts each user's list (case-folded dedup → `sorted()`) on read, and `add_ticker` re-sorts after appending, so `/watch`, `/digest` ticker picker, `/del`, and `/history` ticker list all see stable alphabetical order without sorting at the call site. Existing pre-sort saves are upgraded transparently on the next `add_ticker`. `remove_ticker` preserves order naturally.
- **Storage mutations are async + atomic + durable.** `add_ticker`, `remove_ticker`, `set_llm_provider`, `set_llm_model`, `clear` are `async def` and call `await self._save_async()`. `_save` writes to a tempfile in the same directory, `flush + fsync`s the file descriptor, then `os.replace`s into place — survives mid-write power loss. Read methods (`get_*`) stay sync — pure in-memory dict reads.
- **Per-user config** lives in `data/user_config.json`, keyed by stringified Telegram user_id, holding `llm_provider` + `deep_think_llm` + `quick_think_llm` + `max_debate_rounds` (1/2/3, default 1) + `effort_level` ("low"/"medium"/"high"/None, default None) and an optional `digest: {enabled, hour_local, tz, chat_id, tickers: list[str]}` block. `tickers` is the digest filter — explicit opt-in, default `[]` for new users, fan-out skips with a reminder when empty. **`set_llm_provider` wipes only deep/quick** (provider-specific); rounds/effort are graph-level + provider-agnostic vocabulary that survives provider switches. `clear(user_id)` pops *all* LLM keys (provider/deep/quick/rounds/effort) so a `/config` Cancel from a clean state rolls everything back; non-LLM blocks (e.g., digest) are preserved. The user entry is dropped only when nothing else is left in it. `build_user_config` reads rounds + effort and maps the latter to the provider-specific config key (`openai_reasoning_effort` / `anthropic_effort` / `google_thinking_level`); for providers outside `PROVIDERS_WITH_EFFORT` the effort value is silently ignored at run time.
- **Digest schedule** is the source of truth in `user_config.json`; PTB's `JobQueue.run_daily` is just an in-memory schedule reconstructed from storage at every `_post_init`. `register_digest_job(application, user_id, digest)` cancels any existing job under name `digest:<user_id>` and re-creates with `time=dt_time(hour_local, tzinfo=ZoneInfo(tz))`, so an hour or tz change replaces the schedule rather than duplicating it. DST is handled by `ZoneInfo`. JobQueue callback routes through `run_user_digest`, which intersects `digest.tickers` with the live watchlist (auto-prunes tickers the user removed since picking — empty result → reminder message, no fan-out), sends a header (with `❌ Cancel digest` button), fans out via `_analyze_one_for_digest` (each ticker holds the global `_run_semaphore`, so the digest interleaves naturally with manual `/watch` runs), edits the header progressively as steps fire (single-flight throttle at `_DIGEST_PROGRESS_INTERVAL`), and replaces it with a summary at completion. On `Forbidden` (user blocked the bot) the digest auto-disables and the JobQueue job is cancelled. The cancel button sets a shared `cancel_event` (threading) so in-flight tickers raise `CancelledByUserError` at the next LLM-call boundary, and `Task.cancel()`s pending tickers so they unwind without acquiring a slot. `▶ Run now` is guarded by `chat_data["digest_running:<user_id>"]` so a re-tap during an active fan-out doesn't spawn N parallel digests. Backward compat: legacy saves with `tickers` absent (key missing) are treated as "all watchlist" at fan-out time AND backfilled to the actual watchlist on first `_post_init` startup, after which every enabled digest has the field present.
- **Auth gate** (`auth.py:authorize`) runs at `group=-1` for every Update; raises `ApplicationHandlerStop` for users not in `Config.ALLOWED_USER_IDS`. Empty list = open to all and is logged at WARNING level on startup. Updates without an `effective_user` (channel_post / my_chat_member / some inline_query variants) fail closed when an allowlist is set, pass through when it's empty.
- **`TRADINGAGENTS_AVAILABLE`** flag gates analysis; bot still loads if tradingagents fails to import.
- **Graph pool.** `analysis.py:_graph_pool` keeps a `GraphPool` per `(provider, deep, quick, rounds, effort)` key — the 5-tuple matches the cache slug knobs so two users on different rounds/effort don't share an instance baked with the wrong config. Each pool grows lazily up to `Config.MAX_CONCURRENT_ANALYSES` `TradingAgentsGraph` instances; `pool.acquire()` returns the first free one (or builds a new one outside the mutex if under cap, or blocks on `queue.Queue.get()` if at cap — but the per-key cap matches the global asyncio semaphore so the blocking branch is unreachable). Each instance is single-use-at-a-time because the graph mutates `self.ticker / self.curr_state` during `propagate()`, but the pool gives us both parallelism (different instances run concurrently) and caching (instances are returned to the pool on completion). Don't bypass `_get_or_create_pool`. Key-level LRU at `GRAPH_CACHE_MAX` (default 4) — evicting drops the entire pool for that key.
- **Concurrent updates** (`app.py`): `Application.builder().concurrent_updates(True)` is **load-bearing for cancellation**. With the default single-worker dispatcher, an in-flight analysis handler (awaiting `to_thread`) blocks the queue, so a Cancel-button update sits there until the analysis returns — exactly when we no longer need it. Don't drop this flag.
- **Per-step progress + cooperative cancel.** `progress.py:delegating_progress_callback` is a singleton `BaseCallbackHandler` attached to every graph via `callbacks=[...]` in the constructor. **`raise_error = True` on this class is load-bearing**: LangChain's callback manager swallows handler exceptions by default, defeating our raise-to-cancel strategy. With it enabled, raising `CancelledByUserError` from `_dispatch` aborts the about-to-fire LLM call and bubbles up through langgraph → `propagate()` → `to_thread`. TradingAgents passes our callbacks into the LLM kwargs, so we receive `on_chat_model_start` / `on_llm_start` events — **not** `on_chain_start`. LangGraph propagates the surrounding node name as `metadata["langgraph_node"]` on every nested LLM call. Per-run target (`ProgressReporter`) lives in `threading.local()` set by `run_trading_analysis` around `propagate()`. `ProgressReporter.cancel_event: threading.Event` is checked in `_dispatch` BEFORE every step update; the reporter also re-attaches the ❌ Cancel keyboard on every caption edit (Telegram drops `reply_markup` if not re-sent on `editMessageCaption`).
- **Cancel registry.** `chat_data["analysis_cancels"]` is `{run_id: {"event": threading.Event, "async_event": asyncio.Event, "message_id": int|None}}` — populated by `_run_analysis_for_ticker` on entry (UUID `run_id` chosen before `send_photo`, `message_id` backfilled after), popped in `finally`. The Cancel button's `cancel_analysis:<run_id>` callback looks up the entry and `set()`s both events (`event` aborts in-flight LLM calls via the progress callback; `async_event` wakes a queued semaphore wait). Three race-close checks in the analysis path (post-`to_thread`, post-Telegraph publish, plus the in-pipeline raise) ensure a late tap still discards the result instead of overwriting "Cancelling…" with a success caption.
- **Ticker validation** (`validation.py:validate_ticker`): `_apply_add` calls this in parallel via `asyncio.gather` for each input token. yfinance's `Ticker(symbol).history(period="1d")` is the authoritative check — same source tradingagents uses. Class-share dot forms (`BRK.B`) are auto-rewritten to dash form (`BRK-B`) when the original lookup is empty. yfinance's stderr noise on misses is silenced via `logging.getLogger("yfinance").setLevel(CRITICAL)`. 5-min in-process TTL cache, FIFO-evicted at 1024 entries so an open bot can't be OOM'd via `/add JUNK1 JUNK2 …`.
- **Watchlist UX (paginated select-mode, shared by `/watch` and `/refresh`).** Both `/watch` and `/refresh` (no-args form) render the same paginated toggle keyboard via `build_watchlist_response(..., mode=...)` (`WATCHLIST_PAGE_SIZE = 9`, 3×3). Selection state lives in `chat_data["watch_selection"]: set[str]` and persists across pages; current page lives in `chat_data["watch_page"]: int`, reset on each fresh invocation. **`chat_data["watch_mode"]` is `"watch"` or `"refresh"`** — set by the entry command, threaded through every re-render handler (`_handle_select_toggle`, `_handle_select_bulk`, `_handle_page_nav`) so paging/toggling preserves the refresh styling, and consumed (`pop`'d) by `_handle_done`. Refresh mode differs only in (a) header copy, (b) the Done button label (`🔄 Refresh (N)` vs `✅ Done (N)`), and (c) `_handle_done` invalidates today's cache entry for each selected ticker via `result_cache.invalidate(...)` before launching the analyses. Bulk actions `wsel:all` / `wsel:clear` operate on the entire watchlist regardless of current page. Pagination via `wpage:prev` / `wpage:next` (and `wpage:noop` for the central page indicator). The `runall:go` callback (Done button — same in both modes) reads the global selection and dispatches: 1 ticker → `_run_analysis_for_ticker(...)` direct (cached graph from pool); 2+ tickers → `asyncio.gather(*runs)` for parallel runs.
- **Same-day result cache.** `cache.py` is a filesystem-backed cache keyed on `(provider, deep, quick, ticker, date_iso, rounds, effort)` — no `user_id`, so two users on the same `/config` (including the same rounds + effort knobs) share results within a day, and a manual `/watch` followed by the digest's auto-fire on the same ticker only pays for one LLM run. Layout: `<TG_BOT_DATA_DIR>/result_cache/<config_slug>/<TICKER>/<date>.json`. **Slug shape:** `<provider>__<deep>__<quick>` for default users; `__r{n}` is appended only when rounds≠1 and `__e{level}` only when effort is set, so default-config users keep their existing cache slot when this feature ships (no orphaning). Atomic writes (tempfile + fsync + rename, same pattern as `JsonStorage._save`); the tempfile is `unlink`'d on any write exception so partial files can't accumulate. Each entry persists `{final_state, signal, telegraph_url, generated_at}` — `generated_at` is an ISO UTC timestamp the formatter shows on cache-hit renders so users see when the cached decision was made (not the moment they tapped). The whole `final_state` dict is stored (not a slim shape) so future renderer changes don't require re-running every cached ticker. `final_state` carries LangChain message objects (`HumanMessage`, `AIMessage`) that aren't JSON-native; `cache._json_default()` coerces them via `model_dump` → `dict` → `{__type__, content}` → `{__type__, repr}` so a single odd object never sinks the whole write. Hits in `_run_analysis_for_ticker` short-circuit before the semaphore acquire — no progress flow, no Telegraph re-publish; the cached `telegraph_url` is reused. `/refresh <TICKER>` invalidates today's entry then runs through the miss path. Lazy eviction: a fresh date for the same `(config, ticker)` sweeps stale-date siblings on store.
- **Telegraph publishing is offloaded.** `telegraph_client.publish_to_telegraph` wraps the SDK's blocking `create_page` in `asyncio.to_thread` so the event loop stays responsive during the network round-trip.
- **All Telegram messages use `parse_mode="MarkdownV2"`** consistently. Variable content goes through `telegram.helpers.escape_markdown(version=2)` (or sits inside `` `…` `` code spans which need no escaping). For URLs inside link targets `[text](url)` use `formatters.escape_md_v2_url` instead — `escape_markdown` over-escapes, breaking the link. Don't mix legacy `Markdown` back in.
- **Callback dispatch** is prefix-based: `provider:`, `deep:`, `quick:`, `rounds:` (1/2/3), `effort:` (`none` for default, else low/medium/high), `multi:`, `wsel:` (bulk select-all/clear), `wpage:` (pagination prev/next/noop), `runall:`, `digest:` (digest picker — sub-actions `hour:HH`, `tz:<IANA>`, `tzpick`, `hourpick`, `off`, `run`, `tickerpick`, `tt:<TICKER>`, `ttall`, `ttclear`, `ttpage:{prev,next,noop}`), `digest_cancel:<msg_id>` (abort an in-flight fan-out), `del:`, `cancel:`, `cancel_analysis:`, `hist:`, `hist_t:`, `hist_back:`. Order matters where prefixes overlap (`cancel_analysis:` is checked before `cancel:`, `digest_cancel:` before `digest:`). Stay under Telegram's 64-byte `callback_data` limit.
- **Reply-driven `/add`.** `commands.ADD_PROMPT` is matched verbatim against `update.message.reply_to_message.text` so `add_via_reply` only fires on replies to our actual prompt, not random replies to bot messages.
- **Caption rendering.** `format_short_message` builds the post-analysis MarkdownV2 caption: signal emoji (`BUY` 🟢, `OVERWEIGHT` 🟩, `HOLD` 🟡, `UNDERWEIGHT` 🟥, `SELL` 🔴), ticker, signal verb, then a 2–3-sentence summary from `formatters.extract_summary(final_trade_decision, max_len=280)` (clips lead paragraph), then UTC timestamp + Telegraph link.
- **Telegraph CONTENT_TOO_BIG**: `format_analysis_result_markdown` emits only `final_trade_decision` + `trader_investment_plan`. Adding more `final_state` sections risks blowing the cap — truncate or drop sections, don't just append.
- **finviz cache-busting**: `chart.py:finviz_chart_url` appends `&_=<unix_ts>` so Telegram's CDN doesn't serve a stale cached photo.
- **History command** (`/history <ticker> [YYYY-MM-DD]`) reads tradingagents' on-disk JSON logs at `<results_dir>/<TICKER>/TradingAgentsStrategy_logs/full_states_log_<date>.json`. `history.normalize_ticker` regex-validates `^[A-Z0-9]+(?:[.\-][A-Z0-9]+)*$` to block path traversal — alnum tokens separated by single `.` or `-` only, so `..`, `.A`, `A.`, `A..B` are all rejected. The earlier `[A-Z0-9.\-]+` accepted `..` because `.` is a literal inside a character class. The date arg must round-trip through `date.fromisoformat` before joining the path. The date picker has a `← Back` button to return to the ticker picker; the rendered analysis view has a `← Back` to the date picker. Dispatched via the `hist_back:` prefix.

## Environment variables

```
TELEGRAM_BOT_TOKEN=<required>
TELEGRAPH_ACCESS_TOKEN=<required for Telegraph publishing>
ALLOWED_USER_IDS=123,456              # empty = open (logged at WARNING)
TG_BOT_DATA_DIR=data                  # default
TG_BOT_TA_DEBUG=                      # set "1"/"true" to enable TradingAgentsGraph(debug=True)
TG_BOT_MAX_CONCURRENT_ANALYSES=3      # bot-wide concurrency cap; runs above this show "⏳ Queued" until a slot frees up. Also sizes the per-key graph instance pool.
TRADINGAGENTS_RESULTS_DIR=...         # /history reads from here; defaults to ~/.tradingagents/logs
TRADINGAGENTS_CACHE_DIR=...           # tradingagents data cache; defaults to ~/.tradingagents/cache
# Plus provider keys: OPENAI_API_KEY / DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / etc.
```

**Docker persistence caveat.** `TRADINGAGENTS_RESULTS_DIR` and `TRADINGAGENTS_CACHE_DIR` default to paths under `~/.tradingagents`, which is ephemeral inside the container. To persist `/history` data and avoid re-fetching yfinance on every restart, set them to paths under `/app/data/` (which is bind-mounted) — for example `TRADINGAGENTS_RESULTS_DIR=/app/data/ta-logs`.

## Workflow when making changes

A change in this repo usually touches more than just code — these surfaces drift in parallel and there's no automated check that catches the drift, so verify each pass before declaring done.

1. **Verify the code works.** `.venv/bin/python -m ruff check src/ scripts/ && ruff format --check` and the six smoke suites: `smoke_concurrent.py` (11 orchestration scenarios) + `smoke_parallel.py` (parallelism wall-time check) + `smoke_digest.py` (57) + `smoke_cache.py` (14) + `smoke_user_config.py` (16) + `smoke_watchlist.py` (4) — 100+ scenarios total. **All scripts repoint `TG_BOT_DATA_DIR` to a fresh tempdir in their setup** so the same-day result cache from one test (or one run) doesn't short-circuit another that shares ticker names. For UI-affecting changes, also restart the bot and exercise the change in Telegram — smoke tests verify code correctness, not feature correctness.
2. **If user-visible behavior changed**, sync four places that drift independently:
   - `README.md` — Commands table + Features bullets.
   - `CLAUDE.md` — Commands table + Key contracts + a "Recently fixed (May 2026)" entry for non-trivial changes.
   - `src/tg_bot/handlers/commands.py:start` — onboarding nudge text.
   - `src/tg_bot/handlers/commands.py:help_cmd` and `app.py:BOT_COMMANDS` — slash-menu copy + the canonical command list.
3. **If env-var surface changed**, sync `.env.example` and `docs/CONFIGURATION.md`. Provider keys also need a row in `analysis.py:PROVIDER_ENV_KEYS` so the LLM precheck can flag a missing key.
4. **If a handler contract, dataflow, or command surface changed**, smoke coverage moves in lockstep — non-optional, treat "no smoke scenario" as a merge blocker. Two directions:
   - **Existing fixtures may need updates** to bypass new gates or arm new preconditions. Learned twice: the H4 auth fail-closed change required updating channel-post tests; the LLM precheck broke 7 fan-out tests until we added a global bypass in the smoke runner.
   - **New behavior needs a new scenario.** A new callback prefix, storage field, command, dataflow path, or invariant from the Architecture section each need at least one assertion in `scripts/smoke_*.py` (extend an existing suite or start a new one for a wholly new subsystem). Every recent PR shipped one — PR #14 added `smoke_cache.py`, PR #15 added `smoke_user_config.py` + 4 scenarios in `smoke_cache.py`, PR #16 added `smoke_watchlist.py`. Don't break the pattern.
5. **Commit messages must end with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`** — the system prompt mandates this and the GitHub UI uses it to render Claude as a co-author.

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
- **`docker-build.yml`** — push to main, daily cron, or manual dispatch builds and dual-pushes to Docker Hub (`ivanwng97/tradingagents-telegram`) AND GitHub Container Registry (`ghcr.io/ivanwng97/tradingagents-telegram`). Same multi-arch manifest, both registries, single push step (`docker/metadata-action` emits tags for both image names). GHCR login uses `secrets.GITHUB_TOKEN` (no extra secret) and the workflow's `packages: write` permission. Trivy CRITICAL/HIGH SARIF upload. The cron run resolves upstream `tradingagents` `HEAD` SHA via `git ls-remote` and skips the build (via `actions/cache@v5`) when the SHA matches the previous build, so unchanged days don't burn CI minutes. Push events use `cache-from: type=gha` for fast iteration; schedule + manual dispatch force `--no-cache` so the `pip install` layer re-resolves tradingagents.
- **`codeql.yml`** — `security-extended` Python analysis on push/PR/weekly cron.
- **`claude.yml`** — on-demand Claude review via `@claude` mention in PR comments / review threads. The auto-on-every-PR variant (`claude-code-review.yml`) was removed because the marketplace plugin's installation path is broken upstream.
- **`dependabot.yml`** — weekly bumps for pip / github-actions / docker.

## Known limitations

- `openrouter` and `azure` providers have no model catalog; their selection step short-circuits with a notice and analysis falls back to `DEFAULT_CONFIG` models. Custom-model-ID input UI is not yet wired.
- `send_photo` / Telegraph publish have no explicit timeouts; if finviz or Telegraph hangs, PTB's defaults apply (~5s) and the user sees no progress beyond the "Analyzing…" caption.
- `Config.TA_DEBUG` is read once at process start; toggling `TG_BOT_TA_DEBUG` requires a bot restart (and would still only affect graphs built *after* the restart since cached entries carry their `debug` flag from init time).
- **Cancel during pool-wait isn't instant**: when a queue size exceeds `Config.MAX_CONCURRENT_ANALYSES`, the overflow runs `queue.Queue.get()` for a free instance — `cancel_event` isn't checked there, so the run only aborts after acquiring an instance and reaching its first step boundary. The per-key cap matches the global asyncio semaphore, so the blocking branch is unreachable in practice — runs are gated upstream at the semaphore.
- **In-flight LLM call still completes after Cancel**: cancel is cooperative at LLM-call boundaries; we can't kill an HTTP request already on the wire. The current call's tokens are paid for, but no further steps run.
- **Build parallelism inside `_builder()`**: building N graphs simultaneously isn't N× faster — LangChain LLM client init and ChromaDB setup serialize partially on internal locks + GIL.
- `asyncio.to_thread` uses Python's default thread pool (`min(32, cpu_count + 4)`) — beyond that, parallel runs queue at the executor layer regardless of the graph pool size.
- No structured logging / correlation id. Smoke coverage exists (`scripts/smoke_*.py`) but no pytest suite. Graceful shutdown is wired (`_post_stop` signals every in-flight cancel + 2s drain).

## macOS .pth quirk

Python 3.14's `site.py` skips any `.pth` file marked with the macOS `UF_HIDDEN` filesystem flag. Files inside `~/Desktop` (especially with iCloud Desktop sync enabled) tend to inherit this flag. `pip install -e .` writes `__editable__.tg_bot-0.1.0.pth` which then gets ignored, so `python -m tg_bot` fails with `No module named tg_bot`.

Fix:
```bash
chflags nohidden .venv/lib/python3.14/site-packages/*.pth
```

Apply once after each fresh install. Docker (Linux) is unaffected.

## Recently fixed (May 2026)

This section is reviewer-context for the most recent non-trivial changes — older entries graduate into the body sections above (Architecture / Key contracts / Known limitations) and are dropped from here. Git log carries the full history; this is curated narrative.

- **Unified `/watch` + `/refresh` picker UX.** `/refresh` (no args) now renders the same paginated multi-select keyboard as `/watch`, with the Done button labeled `🔄 Refresh (N)` and a "drops today's cached result" hint in the header. Tapping Done invalidates today's cache for each selected ticker before launching the analyses, so multi-ticker refresh becomes one tap instead of N invocations of the legacy `/refresh NVDA` form. `chat_data["watch_mode"]` (`"watch"` | `"refresh"`) is set by the entry command, threaded through every re-render handler so paging/toggling preserves the styling, and consumed by `_handle_done`. Direct `/refresh NVDA` (single-ticker fast path) is unchanged. New `scripts/smoke_watchlist.py` covers picker rendering for both modes, keyboard-structure parity, and the empty-watchlist short-circuit.
- **`/config` quality knobs: `max_debate_rounds` + `effort_level`.** Two new picker steps appended (provider → deep → quick → **rounds (1/2/3)** → **effort (default/low/medium/high)**) so users can dial up tradingagents' debate-loop depth and provider thinking effort. Effort step is auto-skipped for providers without a knob (`deepseek`, `qwen`, `glm`, `ollama`, `xai`). Stored under `max_debate_rounds` + `effort_level` in `user_config.json`; both survive `set_llm_provider` switches (only deep/quick clear). `build_user_config` resolves effort to the provider-specific config key (`openai_reasoning_effort` / `anthropic_effort` / `google_thinking_level`). Cache slug extends with `__r{n}` (only when ≠1) and `__e{level}` (only when set), keeping default users on their existing cache slot. **GraphPool key extended in lockstep** to `(provider, deep, quick, rounds, effort)` — the cache disambiguates output but the pool key needs the same shape, otherwise two users on different rounds/effort silently share a graph instance baked with the wrong config (a reviewer missed this in the first pass — now Invariant #1). Caption shows the cached `generated_at` on hits + a `via <provider> · <deep>/<quick> [· r{n}] [· e={level}]` line.
- **Same-day result cache + `/refresh`.** `cache.py` skips the LLM run when an identical analysis already happened today (key = `(provider, deep, quick, ticker, date_iso, rounds, effort)`, no `user_id` — two users on the same `/config` share results within a day). Filesystem-backed under `<TG_BOT_DATA_DIR>/result_cache/<config_slug>/<TICKER>/<date>.json`. `_run_analysis_for_ticker` and `_analyze_one_for_digest` both check before acquiring the semaphore — hits skip the Telegraph re-publish (cached `telegraph_url` reused) and the progress flow. `/refresh <TICKER>` drops today's entry. The whole `final_state` is persisted (not a slim shape) so future renderer tweaks don't require re-running every cached ticker; LangChain message objects coerced via `cache._json_default()`, tempfile `unlink`'d on any write failure.
- **Daily digest scheduler.** `/digest` opens a single-screen picker — 6×4 hour grid + `🌍 Time zone` swap-grid for 10 IANA zones, `▶ Run now` for ad-hoc fan-out, `🔕 Off` to disable (preserves hour + tz + tickers). Persisted in `user_config.json` under `digest: {enabled, hour_local, tz, chat_id, tickers: list[str]}`; PTB `JobQueue.run_daily` is reconstructed from storage at `_post_init`. The `tickers` filter is explicit-opt-in (new users get `tickers=[]` and must enable; legacy enabled-digest users get backfilled to their full watchlist on first restart). Fan-out reuses `_run_semaphore` and intersects the filter with the live watchlist (auto-prunes deleted tickers). On `Forbidden` (user blocked the bot): auto-disable + JobQueue cancel. `tzdata` PyPI dep added because `python:3.14-slim` lacks the IANA db.
- **Code-review batch** (post-digest). Auth gate fails closed for updates without `effective_user` when an allowlist is set (channel_post / my_chat_member otherwise leak through). Three near-duplicate Telegraph URL escapes consolidated into `formatters.escape_md_v2_url`. Validation cache bounded to 1024 entries (FIFO) so `/add JUNK1 JUNK2 …` can't OOM the bot. `MAX_CONCURRENT_ANALYSES` clamped to ≥1 (`Semaphore(0)` would brick). `extract_summary` default bumped 200 → 280. `progress.resolve_step` re-looks up "tools"-suffixed nodes so `news_analyst tools` still resolves. Digest `▶ Run now` re-tap guarded by `chat_data["digest_running:<uid>"]`.
- **Telegram resilience**: `AIORateLimiter` middleware throttles outgoing calls under per-bot/per-chat caps. HTTP timeouts raised to 30s read/write/pool, 15s connect (PTB defaults of ~5s were getting hit by Telegram's slow finviz URL fetches). `send_photo` retries once on `TimedOut` with 1s backoff. Cancel button attached via `send_photo(reply_markup=...)` directly — eliminates the second API call that was getting rate-limit-dropped.
- **Graceful shutdown** via `Application.builder().post_stop(_post_stop)`. SIGTERM/SIGINT iterates every chat's `analysis_cancels` registry, sets both `event` and `async_event` for every entry, then sleeps 2s for handlers to render "❌ Cancelled" captions before exit. Matters for `docker-compose up -d --build` rollouts.
- **`/status` command** + `analysis.pool_stats()` helper. Tracks uptime via `bot_data["start_time"]` (set in `_post_init`) and a global `bot_data["analysis_count"]` incremented at the top of every `_run_analysis_for_ticker` invocation. Surfaces graph pool stats (key count + total instances) for diagnosing pool exhaustion.
