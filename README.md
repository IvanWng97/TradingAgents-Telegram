# TradingAgents Telegram Bot

Telegram bot wrapping the [TradingAgents](https://github.com/TauricResearch/TradingAgents) framework. Curate a watchlist via Telegram, tap one or more tickers, and the bot runs `TradingAgentsGraph.propagate(...)` for each and replies with a finviz chart, the trade decision, a short summary, and a Telegraph link to the full analysis. Per-step pipeline progress (Market Analyst → Bull Researcher → … → Portfolio Manager) streams back into the message caption while the analysis runs, and every in-flight analysis carries its own ❌ Cancel button.

## Commands

| Command | Description |
|---|---|
| `/start`, `/help` | Welcome / help text |
| `/add NVDA AAPL TSLA` | Bulk-add tickers. Each is validated against yfinance — invalid symbols are rejected with a hint, and US class-share dot forms (`BRK.B`) auto-correct to dash form (`BRK-B`). |
| `/add` (no args) | Bot prompts; reply with the ticker(s) you want to add |
| `/del NVDA AAPL` | Bulk-remove |
| `/del` (no args) | Inline-button picker — tap ❌ on each ticker, `✅ Done` to close |
| `/watch`, `/list` | Show your watchlist as a select-mode keyboard. Tap any ticker to toggle (✅ prefix), then `✅ Done` runs the selected ones — single ticker uses the cached graph (fast), multi runs in parallel with independent ❌ Cancel buttons. |
| `/config` | Three-step picker: provider → deep model → quick model. `❌ Cancel` at any step rolls back to your prior settings |
| `/history` (no args) | Inline-button picker of all tickers with saved analyses |
| `/history NVDA` | Inline-button picker of recent analysis dates for that ticker. `← Back` returns to the ticker picker. |
| `/history NVDA 2026-04-15` | Direct lookup — publishes that day's saved analysis to Telegraph |
| `/status` | Operational snapshot: bot uptime, # analyses since boot, graph-pool size, your current LLM config |

The Telegram client also exposes a Menu button next to the input field with the same commands (populated via `set_my_commands`), and `/`-autocomplete works after typing the first letter or two.

## Cancellation

Every running analysis attaches a ❌ Cancel button to its progress message. Tapping it sets a per-run flag that the LangChain progress callback checks at every LLM-call boundary; the next call raises `CancelledByUserError`, which propagates out of langgraph and aborts the pipeline. The in-flight LLM call still completes (we can't kill an HTTP request that's already on the wire), but no further steps run. In a multi-ticker queue, cancelling one only stops that one — the rest keep going.

## Quick start (local)

```bash
git clone <repo>
cd TradingAgents-Telegram

# 1. Set up venv + install
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Configure secrets
cat > .env <<EOF
TELEGRAM_BOT_TOKEN=...
TELEGRAPH_ACCESS_TOKEN=...
ALLOWED_USER_IDS=12345678          # IMPORTANT: leave empty only if you trust the public
DEEPSEEK_API_KEY=...               # or OPENAI_API_KEY / ANTHROPIC_API_KEY / etc.
EOF

# 3. (macOS only, once per fresh install) clear macOS UF_HIDDEN flag
#    on the editable .pth so Python 3.14 stops skipping it
chflags nohidden .venv/lib/python3.14/site-packages/*.pth

# 4. Run
python -m tg_bot
```

Get a bot token from [@BotFather](https://t.me/BotFather) (`/newbot`); get a Telegraph access token from [Telegraph's API docs](https://telegra.ph/api).

> ⚠️ Leaving `ALLOWED_USER_IDS` empty makes the bot open to anyone who finds your bot handle, and they will burn your LLM tokens. The bot replies with the requesting user's Telegram ID on rejection so you can whitelist them.

## Deploy with Docker

```bash
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` mounts `./data` into the container so per-user watchlists, LLM settings, and (with the env vars below) tradingagents history/cache survive restarts. `.env` is loaded via `env_file:` and is never baked into the image.

To upgrade later: `git pull && docker compose up -d --build`.

**To pick up new upstream `tradingagents` commits**: GitHub Actions has a daily cron that resolves upstream HEAD via `git ls-remote`, skips the build when nothing changed, and otherwise rebuilds the image with `--no-cache`. Manual dispatch forces a rebuild. From the VPS just `docker compose pull && docker compose up -d`.

## Configuration

Set in `.env` (or as compose env vars):

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `TELEGRAPH_ACCESS_TOKEN` | yes | for Telegraph publishing |
| `ALLOWED_USER_IDS` | strongly recommended | comma-separated; empty = open to anyone (logged at WARNING). Bot replies with the user's Telegram ID on rejection so you can whitelist them. |
| `TG_BOT_DATA_DIR` | no | default `data` |
| `TG_BOT_TA_DEBUG` | no | `1`/`true` enables `TradingAgentsGraph(debug=True)` (streams every langgraph chunk to stdout — verbose, dev only). Default off. |
| `TRADINGAGENTS_RESULTS_DIR` | recommended in Docker | `/history` reads from here. Defaults to `~/.tradingagents/logs` (ephemeral in containers). Set to e.g. `/app/data/ta-logs` and bind-mount to persist. |
| `TRADINGAGENTS_CACHE_DIR` | recommended in Docker | yfinance cache. Defaults to `~/.tradingagents/cache` (ephemeral). Set to `/app/data/ta-cache` to skip re-downloads on every restart. |
| Provider keys | yes (one) | `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, etc. — must match the provider you select via `/config` |

Supported LLM providers (set via `/config`): `openai`, `google`, `anthropic`, `xai`, `deepseek`, `qwen`, `glm`, `ollama` (have built-in model catalogs); `openrouter`, `azure` (require manual model IDs — UI not yet wired).

## Layout

```
src/tg_bot/
├── __main__.py            # `python -m tg_bot`
├── app.py                 # Application builder, BOT_COMMANDS, post_init
├── auth.py                # ALLOWED_USER_IDS gate (TypeHandler at group=-1)
├── config.py              # env-driven Config
├── analysis.py            # TradingAgents adapter, graph pool, model catalog
├── chart.py               # finviz_chart_url
├── formatters.py          # Telegram caption (signal emoji + summary) + Telegraph markdown
├── progress.py            # ProgressReporter + cancel-aware LangChain BaseCallbackHandler
├── history.py             # disk-readers for past analyses
├── telegraph_client.py    # sanitize + publish
├── validation.py          # yfinance-backed ticker validation + class-share rewrite
├── handlers/{commands,callbacks}.py
└── storage/                # JSON-backed, atomic + fsync writes; per-user
    ├── _base.py            # JsonStorage (async wrapper for mutations)
    ├── watchlist.py
    ├── user_config.py
    └── __init__.py         # exports singletons
data/                       # runtime state (watchlist.json, user_config.json)
.github/workflows/          # ruff lint, Docker build with SHA-check skip
pyproject.toml              # deps + package metadata
Dockerfile, docker-compose.yml
```

## TODO

- **Daily digest scheduler** — cron-like `JobQueue` job that walks each user's watchlist nightly and posts a single summary message with all signals, so users get a passive daily read without tapping anything.

See [`CLAUDE.md`](./CLAUDE.md) for architecture notes, key contracts, and current limitations.
