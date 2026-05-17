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

## TradingAgents upstream sync — surface new config knobs

Audit upstream [`.env.example`](https://github.com/TauricResearch/TradingAgents/blob/main/.env.example) against our own. Upstream adds config knobs over time (provider keys, model overrides, debate-round defaults, cache paths) that we currently bake into `DEFAULT_CONFIG` or leave un-exposed. Each pass: diff upstream vs `.env.example` + `pipeline/analysis.py:build_user_config`, surface anything that lets users tune behavior we hardcode today.

**Why.** Keeps the bot's surface area in sync with what users would see if they ran `tradingagents` directly. Also catches new provider integrations we'd otherwise miss until someone files an issue.

**Out of scope.** Auto-syncing upstream — manual audit each `upstream-tag-watch.yml` ping is fine while the bot is small. The workflow already opens an `upstream-audit` issue per upstream release — this TODO is the "what to check during that audit" companion.

## Skip digest on market-closed days

Currently `JobQueue.run_daily` fires every day, including weekends + holidays — wasted LLM credits for digests with no new market data to analyze.

**Sketch.**

- **Phase 1 (weekday skip):** pass `days=(0,1,2,3,4)` to `run_daily` in `_post_init`'s schedule rebuild. One-line change, no new dep, tz-aware via the user's existing `tz` field. Job literally doesn't fire on Sat/Sun.
- **Phase 2 (market holidays):** add `pandas_market_calendars` dep; check `nyse.schedule(today, today).empty` at the top of `run_user_digest`. Skips Thanksgiving / Christmas / July 4 / etc. Default to NYSE; thread a per-ticker → exchange map later if non-US tickers need it.

**Why.** Saves LLM credits and reduces noise (a digest with "no new market data" is just clutter that trains users to ignore the morning ping).

**Out of scope.** Per-ticker market awareness for the initial cut (BTC-USD trades 24/7, `RELIANCE.NS` on NSE calendar, `0700.HK` on HKEX). Default NYSE covers ~90% of typical watchlists; the rest get a slightly-over-eager digest, not a wrong one.

## yfinance vs alpha_vantage — data-source toggle (decision captured)

Note: this entry was originally drafted as "yfinance vs Alpaca" based on a misremembered fact. The actual toggle inside `tradingagents` is **yfinance vs alpha_vantage** (per the `data_vendors` dict in `default_config.py:97`, with options `alpha_vantage` or `yfinance` for each of `core_stock_apis` / `technical_indicators` / `fundamental_data` / `news_data`). Alpaca is a brokerage API and isn't a tradingagents data option.

- **yfinance:** unlimited free calls, 200ms-2s per call, occasional 429s, no SLA. Default everywhere in `tradingagents`.
- **alpha_vantage:** real SLA, structured JSON, but free tier is **25 requests/day, 5/min** — exhausted within a single multi-analyst analysis run. Paid tier is $49.99/mo minimum.

**Bot's bottleneck is LLM calls (minutes per analysis), not data fetch.** `validation.py` runs yfinance once per ticker-add (cached 5min, FIFO-evicted at 1024 entries) — ~500ms, invisible against analyses that take multiple minutes. A vendor swap wouldn't move the user-visible latency needle.

**Decision.** Stay on yfinance default. Exposing a toggle is cheap (a single `TRADINGAGENTS_DATA_VENDOR` env var that overlays the `data_vendors` dict) but useful only for users with Alpha Vantage paid tier — narrow audience. Revisit if (a) yfinance reliability degrades to user-visible breakage (sustained 429s, broken validation for >1 day), OR (b) a user requests the Alpha Vantage toggle explicitly.

**Out of scope.** Per-tool overrides via `tool_vendors` (tradingagents supports it but the use case is even narrower). Real-time price feeds via a separate API like Alpaca/Polygon — would require new code paths, not a tradingagents config change; defer until a product feature needs real-time.
