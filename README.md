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

- 🤖 **Multi-Agent Analysis**: Wraps [TradingAgents](https://github.com/TauricResearch/TradingAgents) — analyst → researcher → trader → risk-manager LLM agents collaborate on each ticker.
- 📋 **Watchlist Management**: Curate tickers via `/add`, `/del`, `/watch`. Each is yfinance-validated; class-share dot forms (`BRK.B`) auto-correct to dash form (`BRK-B`).
- ⚡ **Parallel Execution**: Tap multiple tickers and they run in parallel, gated by `TG_BOT_MAX_CONCURRENT_ANALYSES`. Overflow shows `⏳ Queued` until a slot frees.
- ⏳ **Live Progress Streaming**: Per-step pipeline progress streams back into the Telegram message caption while the analysis runs.
- 🛑 **Cooperative Cancel**: Each in-flight analysis carries a ❌ Cancel button; cancellation is checked at every LLM-call boundary so the abort is near-instant.
- 🌅 **Daily Digest**: `/digest` schedules a recurring run of your full watchlist at any hour + IANA timezone, posting one summary message per day with a Telegraph link per ticker.
- 📜 **Analysis History**: `/history` browses every saved analysis by ticker → date and republishes to Telegraph on demand, with `← Back` round-trip navigation.
- 🤹 **Multi-LLM Provider**: OpenAI, DeepSeek, Anthropic, Google, xAI, Qwen, GLM, Ollama — per-user `/config` picks provider + deep-think + quick-think model independently.
- 📈 **Finviz Charts + Telegraph Reports**: Each analysis posts a finviz daily chart inline, with a Telegraph link to the full multi-agent rationale.
- 🔐 **Access Control**: `ALLOWED_USER_IDS` allowlist gates every update; rejection responds with the requesting user's Telegram ID for easy whitelisting.
- 🐳 **Docker-Ready**: Multi-arch (`linux/amd64` + `linux/arm64`) prebuilt image on Docker Hub, daily-rebuilt to track upstream `tradingagents@HEAD`.
- 💾 **Durable Storage**: JSON-backed per-user state with atomic + `fsync` writes — survives mid-write crashes and power loss.
- 🛡 **Hardened CI**: Per-arch Trivy scanning, CodeQL `security-extended`, weekly Dependabot, provenance + SBOM attestations published with every image.

## Demo

Sample published analysis: [BRK-B — 2026-05-05](https://telegra.ph/BRK-B-Analysis-05-05-2). Each `Run` from `/watch` produces a Telegraph page like this — chart + decision + multi-agent rationale.

<!-- TODO: drop a screenshot/GIF of the bot in action here — e.g. the `/watch` paginated keyboard, or a completed analysis caption with cancel button. -->

## Deploy on a VPS — Docker (recommended)

A prebuilt image is published to Docker Hub at [`ivanwng97/tradingagents-telegram:latest`](https://hub.docker.com/r/ivanwng97/tradingagents-telegram), so you don't need to clone the repo or build locally — just grab the compose file + `.env` and run.

```bash
mkdir tradingagents-telegram && cd tradingagents-telegram

# 1. Grab the compose file (it points at the prebuilt image)
curl -O https://raw.githubusercontent.com/IvanWng97/TradingAgents-Telegram/main/docker-compose.yml

# 2. Configure secrets
cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=...
TELEGRAPH_ACCESS_TOKEN=...
ALLOWED_USER_IDS=12345678              # IMPORTANT: leave empty only if you trust the public
DEEPSEEK_API_KEY=...                   # or OPENAI_API_KEY / ANTHROPIC_API_KEY / etc.
TRADINGAGENTS_RESULTS_DIR=/app/data/ta-logs
TRADINGAGENTS_CACHE_DIR=/app/data/ta-cache
EOF

# 3. Pull + run
docker compose pull
docker compose up -d
docker compose logs -f
```

`docker-compose.yml` bind-mounts `./data` into the container so watchlists, LLM settings, and (with the env vars above) `/history` data and the yfinance cache survive restarts. `.env` is loaded via `env_file:` and is never baked into the image.

**Upgrade later**: `docker compose pull && docker compose up -d`. The image is rebuilt automatically by a daily GitHub Action whenever upstream [`tradingagents`](https://github.com/TauricResearch/TradingAgents) advances; the cron skips the build when the SHA hasn't changed, so you only pull a new image when there's actually new upstream code.

Get a bot token from [@BotFather](https://t.me/BotFather) (`/newbot`); a Telegraph access token from [Telegraph's API docs](https://telegra.ph/api).

> ⚠️ Leaving `ALLOWED_USER_IDS` empty makes the bot open to anyone who finds your bot handle, and they will burn your LLM tokens. The bot replies with the requesting user's Telegram ID on rejection so you can whitelist them.
>
> Don't know your Telegram user ID? Forward any message to [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID instantly.
>
> 💰 **Cost expectations**: each analysis runs ~12 LLM calls across the agent pipeline. Per-ticker rough estimates: `deepseek-v4` ≈ $0.01, `gpt-4o` ≈ $0.20, `claude-sonnet-4` ≈ $0.40. Tapping `Run all` on a 10-ticker watchlist can easily hit single-digit dollars in minutes — pick your provider accordingly.

## Run locally (Python venv)

```bash
git clone https://github.com/IvanWng97/TradingAgents-Telegram.git
cd TradingAgents-Telegram

# 1. Set up venv + install
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Configure secrets — same .env contents as the Docker setup above
$EDITOR .env

# 3. (macOS only, once per fresh install) clear macOS UF_HIDDEN flag
#    on the editable .pth so Python 3.14 stops skipping it
chflags nohidden .venv/lib/python3.14/site-packages/*.pth

# 4. Run
python -m tg_bot
```

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
| `/digest` | Schedule a daily run of every ticker in your watchlist. Single-screen picker: 24-hour grid + 10 IANA time zones (Pacific, Eastern, UTC, GMT/BST, JST, …). One summary message per day, one Telegraph link per ticker. `▶ Run now` triggers an ad-hoc fan-out; `🔕 Off` disables. Auto-disables itself if you block the bot. |
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

## Layout

```
src/tg_bot/
├── __main__.py            # `python -m tg_bot`
├── app.py                 # Application builder, BOT_COMMANDS, post_init / post_stop
├── auth.py                # ALLOWED_USER_IDS gate (TypeHandler at group=-1)
├── config.py              # env-driven Config
├── analysis.py            # TradingAgents adapter, graph pool, model catalog
├── chart.py               # finviz_chart_url
├── formatters.py          # Telegram caption (signal emoji + summary) + Telegraph markdown
├── progress.py            # ProgressReporter + cancel-aware LangChain BaseCallbackHandler
├── history.py             # disk-readers for past analyses
├── telegraph_client.py    # sanitize + publish (table → bullet rewrite)
├── validation.py          # yfinance-backed ticker validation + class-share rewrite
├── handlers/{commands,callbacks}.py
└── storage/                # JSON-backed, atomic + fsync writes; per-user
    ├── _base.py            # JsonStorage (async wrapper for mutations)
    ├── watchlist.py
    ├── user_config.py
    └── __init__.py         # exports singletons
data/                       # runtime state (watchlist.json, user_config.json)
scripts/                    # smoke_concurrent.py, smoke_parallel.py
.github/workflows/          # ruff lint, Docker build with SHA-check skip, CodeQL
pyproject.toml              # deps + package metadata
Dockerfile, docker-compose.yml
```

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `TradingAgents not available: …` at startup | Upstream `tradingagents` install failed during the image build. Try `docker compose pull && docker compose up -d` — the daily-rebuilt image often resolves transient git/pip issues. For local dev, re-run `pip install -e ".[dev]"`. |
| `Auth disabled — ALLOWED_USER_IDS empty…` (WARNING at startup) | The bot is open to the public. Set `ALLOWED_USER_IDS=<your_id>` in `.env` and restart. Use [@userinfobot](https://t.me/userinfobot) to find your numeric ID. |
| Bot replies "🚫 Not authorized…" with a Telegram ID | That ID isn't in `ALLOWED_USER_IDS`. Add it (comma-separated for multiple users) and restart. |
| Analysis caption stuck at "📊 Analyzing… please wait" with no progress | LLM authorization or rate-limit error. Check `docker compose logs -f` for the actual error from `tradingagents`. Common: provider key for the wrong service (e.g. `OPENAI_API_KEY` set but `/config` selected `deepseek`). |
| `Analysis failed. TradingAgents module not available.` in chat | Server-side import error — check `docker compose logs`. Usually a stale image; `docker compose pull` to refresh. |
| `/history` empty even after running analyses | `TRADINGAGENTS_RESULTS_DIR` defaults to a path inside the container that gets wiped on restart. Set it to `/app/data/ta-logs` (or similar under `/app/data`) so it persists via the bind mount. |

## Development

Only needed if you'll be modifying code. Install the `[dev]` extra (adds `ruff` + `pre-commit`) and wire up the pre-commit hook so format/lint runs on every commit:

```bash
pip install -e ".[dev]"
pre-commit install   # one-time: lints/formats on every git commit
```

Smoke tests live under `scripts/`:

```bash
python scripts/smoke_concurrent.py   # 11 orchestration scenarios
python scripts/smoke_parallel.py     # parallelism wall-time check
```

## TODO

- **Same-day result cache + `/refresh`** — `_run_analysis_for_ticker` re-runs the full graph even if the same `(ticker, date, provider, deep, quick)` was analyzed minutes ago. Reusing tradingagents' on-disk `full_states_log_<date>.json` would make the second tap free + instant; `/refresh NVDA` opts back into a fresh run.

See [`CLAUDE.md`](./CLAUDE.md) for architecture notes, key contracts, and current limitations.
