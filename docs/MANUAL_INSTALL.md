# Manual install

If you don't want to run [`start.sh`](../start.sh), do it yourself.

## Secrets you'll need

| Secret | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | DM [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts. |
| `TELEGRAPH_ACCESS_TOKEN` | `curl 'https://api.telegra.ph/createAccount?short_name=YourBot&author_name=YourBot'` — copy the `access_token` from the JSON response. |
| Your Telegram user ID | DM [@userinfobot](https://t.me/userinfobot) any message — it replies with your numeric ID instantly. |
| One LLM provider key | Pick whichever provider you'll use (DeepSeek, OpenAI, Anthropic, Google, xAI, Qwen, GLM, MiniMax, OpenRouter, Ollama). Set `TRADINGAGENTS_LLM_PROVIDER` + `TRADINGAGENTS_DEEP_THINK_LLM` + `TRADINGAGENTS_QUICK_THINK_LLM` in `.env` to match — see [`docs/CONFIGURATION.md`](./CONFIGURATION.md) for the catalog and overlay semantics. |

## Steps

```bash
mkdir tradingagents-telegram && cd tradingagents-telegram

# Compose file + env template (no build needed — points at the prebuilt image)
curl -O https://raw.githubusercontent.com/IvanWng97/TradingAgents-Telegram/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/IvanWng97/TradingAgents-Telegram/main/.env.example -o .env

# Open .env, fill in the four secrets above
$EDITOR .env

docker-compose pull
docker-compose up -d
docker-compose logs -f
```

`docker-compose.yml` bind-mounts `./data` into the container so watchlists, digest schedules, and (with the env vars above) `/history` data and the yfinance cache survive restarts. `.env` is loaded via `env_file:` and is never baked into the image. The LLM config (provider + deep + quick model) is read from `.env` env vars at process start, not stored in `data/` — restart the bot to pick up changes.

Want to run from source instead? See [`DEVELOPMENT.md`](./DEVELOPMENT.md).
