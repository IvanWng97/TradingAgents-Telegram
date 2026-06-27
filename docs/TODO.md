# TODO

Roadmap items not yet started. Tracked here while small; promote to GitHub Issues once an item picks up enough scope to need discussion or assignment.

## Accuracy tracker (`/track`)

Snapshot every saved analysis's signal + entry price, then sample the price periodically and surface the delta. Lets users see whether the bot's calls actually pan out — turns the bot from a black box into a track record.

**Sketch.**

- On every successful analysis (manual `/watch` or `/digest`), store a record under `<TG_BOT_DATA_DIR>/track/<user_id>/<date>.json`: `{ticker, signal, entry_price, entry_ts, provider, deep, quick}`. `entry_price` = yfinance previous-close at run time (already fetched by tradingagents).
- Daily cron (PTB `JobQueue.run_daily`, market-close ET) iterates open records, fetches current close via yfinance, writes `current_price` + `pct_change` back. Auto-close records after 30 days.
- `/track` (no args) → "Last 14 days: 9 BUY (+1.8% avg) · 3 HOLD · 2 SELL (-0.4% avg) · hit-rate 64%". `/track NVDA` → per-ticker history.
- Track only — no new LLM calls. Independent of the same-day cache (records keyed on `(user_id, date, ticker)`, not on the cache key).

**Why.**

Right now there's no feedback loop between the bot's verdicts and what the market actually did. Adding accuracy data does three things: builds user trust, surfaces when a provider is consistently wrong (so they switch), and gives us a metric that's not "did the LLM finish without crashing".

**Out of scope.** No backtesting against historical entry points. No leaderboard across users (privacy). No "auto-trade if confidence > X" — that crosses the disclaimer line.


## Watchlist export / import (`/export`, `/import`)

Backup and portability: serialize a user's watchlist to JSON so it can be saved, shared, or re-applied to a fresh bot instance. Mirrors the "data isn't trapped" principle the rest of the bot already follows (watchlist.json on disk, telegraph URLs in cache, etc.).

**Sketch.**

- `/export` — sends the user's watchlist + digest config as a JSON document via `chat.send_document`. Filename: `<user_id>_watchlist_<YYYY-MM-DD>.json`. Shape: `{"version": 1, "watchlist": ["NVDA", ...], "digest": {"enabled": ..., "hour_local": ..., "tz": ..., "tickers": [...], "email": "..."}}`. No timestamps or LLM-config in the payload (process-wide; user shouldn't carry it across).
- `/import` — bare command opens a ForceReply prompt asking for a JSON file (mirroring `/email`'s ForceReply pattern). Accept either an uploaded `.json` document or pasted JSON text. Validate `version`, validate each ticker via `validate_ticker`, report `✅ Imported: NVDA, AAPL · ❓ Invalid: XXXX · ⏭️ Skipped (already in watchlist): TSLA`. Digest config merges into the existing one (additive: new tickers added to enrolled set; existing schedule preserved unless explicitly overridden).
- Single-user scope today (Telegram user id is the key); the JSON does NOT include any cross-user state. Future multi-user instances can reuse the same shape.

**Why.**

Two use cases the architect review on PR #75 flagged would benefit: (1) migrating between bot instances when the operator restarts on a fresh Docker host without persistent `data/` mount; (2) sharing a watchlist between accounts (e.g., personal + work Telegram accounts maintained by the same person). Today both require manual `/add NVDA AAPL ...` line-by-line.

**Out of scope.** No CSV/XLSX support (JSON is enough). No partial export (whole user state or nothing). No history/cache export (that's separate — `data/result_cache/` is server-side and isn't user-shareable anyway).


## Native Rich Messages (Bot API 10.1) — blocked on upstream PTB

Migrate the analysis output from the Telegraph round-trip to native Telegram **Rich Messages** (`sendRichMessage` / `InputRichMessage`, Bot API 10.1, released 2026-06-11). Block-based formatting — `RichBlockTable`, `RichBlockList`, `RichBlockDetails` (collapsible), `RichBlockThinking`, `RichBlockPhoto` — could render the fundamentals/market tables, per-agent rationale, and the finviz chart **inline in Telegram**, potentially shrinking or removing the Telegraph dependency and the caption table-stripping the 1024-char budget forces today.

**Blocked — do not build yet.** `python-telegram-bot` doesn't expose these: installed 22.7 targets Bot API 9.5; latest 22.8 (2026-06-12) targets 10.0. Native support is tracked in [PTB#5261](https://github.com/python-telegram-bot/python-telegram-bot/issues/5261) under the **v23** milestone (due ~2026-09-01), but maintainers **explicitly paused** it pending an internal refactor — the rushed send-side PRs (#5263 / #5275) were closed because the refactor would force a redo. Revisit when PTB ships v23 with `Bot.send_rich_message`.

**Why wait, not raw-HTTP now.** A raw `sendRichMessage` shim over PTB's transport works (a downstream project does exactly this), but the `RichBlock*` field shapes aren't fully documented yet and the API is ~2 weeks old — too unstable to pin production rendering to. Once PTB exposes typed support, scope the `rendering/` migration: `format_analysis_result_markdown` (rich blocks instead of Telegraph HTML), the caption builders in `formatters.py`, and the two sanitizers all touch this.

**Out of scope (until adopted).** No raw-HTTP shim in production; no partial migration — Telegraph stays the canonical full-report surface until Rich Messages can fully replace it.
