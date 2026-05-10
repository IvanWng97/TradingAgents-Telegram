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
