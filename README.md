# TradingAgents Telegram Bot

Telegram bot wrapping the [TradingAgents](https://github.com/TauricResearch/TradingAgents) framework. Users add tickers to a watchlist via Telegram, tap a ticker, and the bot runs `TradingAgentsGraph.propagate(...)` and replies with a finviz chart, the trade decision, and a Telegraph link to the full analysis.

## Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | Show available commands |
| `/add <ticker>` | Add a ticker to your watchlist (e.g. `/add NVDA`) |
| `/del <ticker>` | Remove a ticker from your watchlist |
| `/watch` / `/list` | Show your watchlist with clickable buttons — tap a ticker to run analysis |
| `/config` | Pick LLM provider, then deep-think and quick-think models |

The Telegram client also exposes a Menu button next to the input field with the same commands (populated via `set_my_commands`).

## Quick start (local)

```bash
git clone <repo>
cd TradingAgents-Telegram

# 1. Set up venv + install
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 2. Configure secrets
cat > .env <<EOF
TELEGRAM_BOT_TOKEN=...
TELEGRAPH_ACCESS_TOKEN=...
ALLOWED_USER_IDS=        # comma-separated user IDs; empty = open to everyone
DEEPSEEK_API_KEY=...     # or OPENAI_API_KEY / ANTHROPIC_API_KEY / etc.
EOF

# 3. Run
python -m tg_bot
```

Get a bot token from [@BotFather](https://t.me/BotFather) (`/newbot`); get a Telegraph access token from [Telegraph's API docs](https://telegra.ph/api).

## Deploy with Docker

```bash
docker compose up -d --build
docker compose logs -f
```

`docker-compose.yml` mounts `./data` into the container so per-user watchlists and LLM settings persist across restarts, and reads `.env` via `env_file:` (the file is never baked into the image).

To upgrade later: `git pull && docker compose up -d --build`.

## Configuration

Set in `.env` (or as compose env vars):

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
| `TELEGRAPH_ACCESS_TOKEN` | yes | for Telegraph publishing |
| `ALLOWED_USER_IDS` | no | comma-separated; empty = open. The bot replies with `Your user ID is <id>` to unauthorized users so you can whitelist them. |
| `TG_BOT_DATA_DIR` | no | default `data` |
| Provider keys | yes (one) | `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, etc. — must match the provider you select via `/config` |

Supported LLM providers (set via `/config`): `openai`, `google`, `anthropic`, `xai`, `deepseek`, `qwen`, `glm`, `ollama` (have built-in model catalogs); `openrouter`, `azure` (require manual model IDs — UI not yet wired).

## Layout

```
src/tg_bot/
├── __main__.py            # `python -m tg_bot`
├── app.py                 # Application builder, BOT_COMMANDS
├── auth.py                # ALLOWED_USER_IDS gate (TypeHandler at group=-1)
├── analysis.py            # TradingAgents adapter + model catalog
├── chart.py               # finviz_chart_url
├── formatters.py          # Telegram caption + Telegraph markdown
├── telegraph_client.py    # sanitize + publish
├── config.py              # env-driven Config
├── handlers/{commands,callbacks}.py
└── storage/               # JSON-backed, atomic writes; per-user
    ├── _base.py           # JsonStorage
    ├── watchlist.py
    ├── user_config.py
    └── __init__.py        # exports singletons
data/                       # runtime state (watchlist.json, user_config.json)
pyproject.toml              # deps + package metadata
Dockerfile, docker-compose.yml
```

See [`CLAUDE.md`](./CLAUDE.md) for architecture notes and current limitations.
