<div align="center">

# TradingAgents Telegram Bot

<br>

<img src="assets/logo.png" alt="TradingAgents Telegram Bot" width="160">

<br>

[![Lint](https://github.com/IvanWng97/TradingAgents-Telegram/actions/workflows/lint.yml/badge.svg)](https://github.com/IvanWng97/TradingAgents-Telegram/actions/workflows/lint.yml)
[![Docker](https://github.com/IvanWng97/TradingAgents-Telegram/actions/workflows/docker-build.yml/badge.svg)](https://github.com/IvanWng97/TradingAgents-Telegram/actions/workflows/docker-build.yml)
[![CodeQL](https://github.com/IvanWng97/TradingAgents-Telegram/actions/workflows/codeql.yml/badge.svg)](https://github.com/IvanWng97/TradingAgents-Telegram/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

</div>

Telegram bot wrapping the [TradingAgents](https://github.com/TauricResearch/TradingAgents) framework. Curate a watchlist via Telegram, tap one or more tickers, and the bot runs `TradingAgentsGraph.propagate(...)` for each — replying with a finviz chart, the trade decision, a short summary, and a Telegraph link to the full analysis. Per-step pipeline progress streams back into the message caption while the analysis runs.

## 🚀 Features

- 🤖 **Multi-Agent Analysis**: Wraps [TradingAgents](https://github.com/TauricResearch/TradingAgents) — analyst → researcher → trader → risk-manager LLM agents collaborate on each ticker, output is a finviz chart + decision + Telegraph link.
- 📋 **Watchlist Management**: Curate tickers via `/add`, `/del`, `/watch`. Each is yfinance-validated; class-share dot forms (`BRK.B`) auto-correct to dash form (`BRK-B`).
- ⚡ **Parallel Execution**: Tap multiple tickers and they run in parallel, gated by `TG_BOT_MAX_CONCURRENT_ANALYSES`. Overflow shows `⏳ Queued` until a slot frees.
- ⏳ **Live Progress + Cancel**: Per-step pipeline progress streams back into the Telegram message caption. Each in-flight analysis carries a ❌ Cancel button — cancellation is checked at every LLM-call boundary so the abort is near-instant.
- 🌅 **Daily Digest**: `/digest` schedules a recurring run of a curated subset of your watchlist at any hour + IANA timezone — paginated multi-select picker, one summary message per day with a Telegraph link per ticker.
- 📜 **Analysis History**: `/history` browses every saved analysis by ticker → date and republishes to Telegraph on demand, with `← Back` round-trip navigation.
- 🤹 **Multi-LLM Provider**: OpenAI, DeepSeek, Anthropic, Google, xAI, Qwen, GLM, Ollama — per-user `/config` picks provider + deep-think + quick-think model independently.
- 🛡 **Production-grade**: `ALLOWED_USER_IDS` allowlist, atomic + `fsync` per-user JSON storage, multi-arch Docker image (`amd64` + `arm64`) daily-rebuilt to track upstream, CodeQL `security-extended` + Trivy scanning, provenance + SBOM attestations.

## Demo

Sample published analysis: [BRK-B — 2026-05-05](https://telegra.ph/BRK-B-Analysis-05-05-2). Each `Run` from `/watch` produces a Telegraph page like this — chart + decision + multi-agent rationale.

<div align="center">

| `/add` + `/watch` analysis | `/digest` daily summary | Telegraph Instant View |
|:---:|:---:|:---:|
| <img src="assets/screenshots/watch.jpg" width="260" alt="Watchlist analysis with finviz chart, signal emoji, summary, and View Full Report link"> | <img src="assets/screenshots/digest.jpg" width="260" alt="Daily Digest message listing each ticker with signal emoji and a Telegraph instant-view preview"> | <img src="assets/screenshots/telegraph.jpg" width="260" alt="Telegraph page rendered in Telegram's Instant View — full multi-agent analysis with chart"> |

</div>

## Deploy on a VPS — Docker (recommended)

A prebuilt image is published to Docker Hub at [`ivanwng97/tradingagents-telegram:latest`](https://hub.docker.com/r/ivanwng97/tradingagents-telegram), so you don't need to clone the repo or build locally.

### One-line install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/IvanWng97/TradingAgents-Telegram/main/start.sh)
```

The script asks for the four things you need (bot token, your Telegram user ID, Telegraph token — auto-creates one if you don't have it, and your LLM provider + key), drops a configured `.env` + `docker-compose.yml` into `./tradingagents-telegram`, pulls the image, and prints the `docker compose up -d` you run next.

Prefer to do it by hand? See [Manual install](#manual-install) below.

### First-time setup in Telegram

Once `docker compose logs -f` shows `Application started`, find your bot in Telegram (the handle BotFather gave you) and run through these:

1. **`/start`** — confirms the bot is reachable.
   - If you see *"Not authorized. Your user ID is `913259200`."* — the auth gate rejected you. Add that ID to `ALLOWED_USER_IDS` in `.env` (comma-separated for multiple users), then `docker compose up -d` to restart the bot. Retry `/start`.
2. **`/config`** — pick your LLM provider, then a deep-think model, then a quick-think model. The deep model handles the heavy reasoning (researcher, risk-judge); the quick model runs the cheaper tool-calling steps. Without this step every analysis falls back to `DEFAULT_CONFIG` (currently `o4-mini`), which only works if you set `OPENAI_API_KEY`.
3. **`/add NVDA AAPL`** — add a couple of tickers. Each is yfinance-validated; class-share dot forms (`BRK.B`) auto-correct to dash form (`BRK-B`).
4. **`/watch`** — tap a ticker and `✅ Done` to run your first analysis. Per-step progress streams into the message caption; expect 1–3 min depending on provider.
5. **(Optional) `/digest`** — schedule a daily auto-run. Pick a time zone, an hour, then tap `📋 Tickers` to opt-in which symbols should run each day (new users start with an empty filter — must opt in).

> ⚠️ **Leaving `ALLOWED_USER_IDS` empty makes the bot open to anyone who finds your bot handle, and they will burn your LLM tokens.** Auth is the only thing standing between a stranger and your provider bill — set it.

### Cost expectations

Each analysis runs ~12 LLM calls across the agent pipeline. Per-ticker rough estimates: `deepseek-v4` ≈ $0.01, `gpt-4o` ≈ $0.20, `claude-sonnet-4` ≈ $0.40. Tapping `✅ Done` on a 10-ticker selection can easily hit single-digit dollars in minutes — pick your provider accordingly. The `/digest` filter exists partly so you can run cheap providers across a long watchlist daily without surprise bills.

### Upgrade later

```bash
docker compose pull && docker compose up -d
```

The image is rebuilt automatically by a daily GitHub Action whenever upstream [`tradingagents`](https://github.com/TauricResearch/TradingAgents) advances; the cron skips the build when the SHA hasn't changed, so you only pull a new image when there's actually new upstream code.

### Manual install

If you don't want to run `start.sh`, do it yourself:

| Secret | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | DM [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts. |
| `TELEGRAPH_ACCESS_TOKEN` | `curl 'https://api.telegra.ph/createAccount?short_name=YourBot&author_name=YourBot'` — copy the `access_token` from the JSON response. |
| Your Telegram user ID | DM [@userinfobot](https://t.me/userinfobot) any message — it replies with your numeric ID instantly. |
| One LLM provider key | Pick whichever provider you'll use (DeepSeek, OpenAI, Anthropic, Google, xAI, Qwen, GLM, Ollama). You'll select the matching provider via `/config` after the bot is up. |

```bash
mkdir tradingagents-telegram && cd tradingagents-telegram

# Compose file + env template (no build needed — points at the prebuilt image)
curl -O https://raw.githubusercontent.com/IvanWng97/TradingAgents-Telegram/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/IvanWng97/TradingAgents-Telegram/main/.env.example -o .env

# Open .env, fill in the four secrets above
$EDITOR .env

docker compose pull
docker compose up -d
docker compose logs -f
```

`docker-compose.yml` bind-mounts `./data` into the container so watchlists, LLM settings, and (with the env vars above) `/history` data and the yfinance cache survive restarts. `.env` is loaded via `env_file:` and is never baked into the image.

Want to run from source instead? See [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md).

## Commands

| Command | Description |
|---|---|
| `/start`, `/help` | Welcome / help text |
| `/add NVDA AAPL TSLA` | Bulk-add tickers. Each is validated against yfinance — invalid symbols are rejected with a hint, US class-share dot forms (`BRK.B`) auto-correct to dash form (`BRK-B`). |
| `/add` (no args) | Bot prompts; reply with the ticker(s) you want to add |
| `/del NVDA AAPL` | Bulk-remove |
| `/del` (no args) | Inline-button picker — tap ❌ on each ticker, `✅ Done` to close |
| `/watch`, `/list` | Paginated select-mode keyboard (9 per page). Tap any ticker to toggle (✅ prefix), use `✓ Select all` / `✗ Clear` for bulk, then `✅ Done (N)` runs the selected ones. Single ticker uses the cached graph; 2+ run in parallel up to `TG_BOT_MAX_CONCURRENT_ANALYSES`. Each in-flight analysis has its own ❌ Cancel button. |
| `/config` | Three-step picker: provider → deep model → quick model. `❌ Cancel` at any step rolls back to your prior settings |
| `/digest` | Schedule a daily run of a curated subset of your watchlist. Single-screen picker: 24-hour grid + 10 IANA time zones (Pacific, Eastern, UTC, GMT/BST, JST, …) + a `📋 Tickers (N/M)` filter screen with a 3×3 paginated multi-select. One summary message per day, one Telegraph link per ticker. `▶ Run now` triggers an ad-hoc fan-out; `🔕 Off` disables (preserves hour, tz, and tickers for one-tap re-enable). New users start with an empty filter and must opt in. Auto-disables itself if you block the bot. |
| `/history` (no args) | Inline-button picker of all tickers with saved analyses |
| `/history NVDA` | Inline-button picker of recent analysis dates. `← Back` returns to the ticker picker. |
| `/history NVDA 2026-04-15` | Direct lookup — publishes that day's saved analysis to Telegraph |
| `/status` | Operational snapshot: uptime, # analyses since boot, graph-pool size, your current LLM config |

The Telegram client also exposes a Menu button next to the input field with the same commands (populated via `set_my_commands`), and `/`-autocomplete works after typing the first letter or two.

## Configuration

Set in `.env` (Docker loads via `env_file:`, local picks up via `python-dotenv`):

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `TELEGRAPH_ACCESS_TOKEN` | yes | for Telegraph publishing |
| `ALLOWED_USER_IDS` | strongly recommended | comma-separated; empty = open to anyone (logged at WARNING). Bot replies with the user's Telegram ID on rejection so you can whitelist them. |
| Provider keys | yes (one) | `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, etc. — must match the provider selected via `/config` |
| `TG_BOT_MAX_CONCURRENT_ANALYSES` | no | Max simultaneous analyses across the bot. Default `3`. Acts as a FIFO queue — selections beyond this show "⏳ Queued" until a slot frees, cancellable while waiting. Also sizes the graph-instance pool. Higher = more parallelism, more memory (~50–200 MB per cached graph). |
| `TG_BOT_TA_DEBUG` | no | `1`/`true` enables `TradingAgentsGraph(debug=True)` (verbose, dev only). Default off. |
| `TG_BOT_DATA_DIR` | no | default `data` |
| `TRADINGAGENTS_RESULTS_DIR` | recommended in Docker | `/history` reads from here. Defaults to `~/.tradingagents/logs` (ephemeral in containers). Set to `/app/data/ta-logs` to persist via the bind mount. |
| `TRADINGAGENTS_CACHE_DIR` | recommended in Docker | yfinance cache. Defaults to `~/.tradingagents/cache` (ephemeral). Set to `/app/data/ta-cache` to skip re-downloads on every restart. |

Supported LLM providers (set via `/config`): `openai`, `google`, `anthropic`, `xai`, `deepseek`, `qwen`, `glm`, `ollama` (have built-in model catalogs); `openrouter`, `azure` (require manual model IDs — UI not yet wired).

## Troubleshooting

Common startup / runtime issues and fixes: [`docs/TROUBLESHOOTING.md`](./docs/TROUBLESHOOTING.md).

## More

- [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md) — local dev setup, pre-commit, smoke tests.
- [`docs/TODO.md`](./docs/TODO.md) — roadmap items not yet started.
- [`CLAUDE.md`](./CLAUDE.md) — architecture notes, key contracts, and current limitations.
