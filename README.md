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

## Install

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/IvanWng97/TradingAgents-Telegram/main/start.sh)
```

Prompts for your bot token, Telegram user ID, Telegraph token (auto-creates one if you skip), and an LLM provider + key. Drops a configured `.env` + [`docker-compose.yml`](./docker-compose.yml) into `./tradingagents-telegram`, pulls the [prebuilt image](https://hub.docker.com/r/ivanwng97/tradingagents-telegram), and prints the `docker-compose up -d` to run next.

Prefer to do it by hand? See [`docs/MANUAL_INSTALL.md`](./docs/MANUAL_INSTALL.md).

### First-time setup in Telegram

After `docker-compose up -d`, message your bot — `/start` walks you through the in-Telegram setup (`/config` → `/add` → `/watch`). If `/start` replies *"Not authorized. Your user ID is …"* the auth gate rejected you: add that ID to `ALLOWED_USER_IDS` in `.env`, then `docker-compose up -d` to restart and retry.

> ⚠️ **Leaving `ALLOWED_USER_IDS` empty makes the bot open to anyone who finds your bot handle, and they will burn your LLM tokens.** Set it.

### Cost expectations

Each analysis runs ~12 LLM calls across the agent pipeline. Per-ticker rough estimates: `deepseek-v4` ≈ $0.01, `gpt-4o` ≈ $0.20, `claude-sonnet-4` ≈ $0.40. Tapping `✅ Done` on a 10-ticker selection can easily hit single-digit dollars in minutes — pick your provider accordingly. The `/digest` filter exists partly so you can run cheap providers across a long watchlist daily without surprise bills.

### Upgrade later

```bash
docker-compose pull && docker-compose up -d
```

The image is rebuilt automatically by a daily GitHub Action whenever upstream [`tradingagents`](https://github.com/TauricResearch/TradingAgents) advances; the cron skips the build when the SHA hasn't changed, so you only pull a new image when there's actually new upstream code.

## Commands

Discover them in Telegram via the Menu button next to the input field, `/`-autocomplete, or `/help`. The full reference lives in `/help`; the canonical set is registered in `app.py:BOT_COMMANDS`.

## Configuration

All env vars — required secrets and tuning knobs — live in [`docs/CONFIGURATION.md`](./docs/CONFIGURATION.md). The four required secrets are pre-templated in [`.env.example`](./.env.example).

## Troubleshooting

Common startup / runtime issues and fixes: [`docs/TROUBLESHOOTING.md`](./docs/TROUBLESHOOTING.md).

## More

- [`docs/MANUAL_INSTALL.md`](./docs/MANUAL_INSTALL.md) — manual Docker setup if you'd rather not run `start.sh`.
- [`docs/CONFIGURATION.md`](./docs/CONFIGURATION.md) — env vars (required + tuning) and supported LLM providers.
- [`docs/DEVELOPMENT.md`](./docs/DEVELOPMENT.md) — local dev setup, pre-commit, smoke tests.
- [`docs/TODO.md`](./docs/TODO.md) — roadmap items not yet started.
- [`CLAUDE.md`](./CLAUDE.md) — architecture notes, key contracts, and current limitations.
