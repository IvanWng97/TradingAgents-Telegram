# Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `TradingAgents not available: …` at startup | Upstream `tradingagents` install failed during the image build. Try `docker compose pull && docker compose up -d` — the daily-rebuilt image often resolves transient git/pip issues. For local dev, re-run `pip install -e ".[dev]"`. |
| `Auth disabled — ALLOWED_USER_IDS empty…` (WARNING at startup) | The bot is open to the public. Set `ALLOWED_USER_IDS=<your_id>` in `.env` and restart. Use [@userinfobot](https://t.me/userinfobot) to find your numeric ID. |
| Bot replies "🚫 Not authorized…" with a Telegram ID | That ID isn't in `ALLOWED_USER_IDS`. Add it (comma-separated for multiple users) and restart. |
| Analysis caption stuck at "📊 Analyzing… please wait" with no progress | LLM authorization or rate-limit error. Check `docker compose logs -f` for the actual error from `tradingagents`. Common: provider key for the wrong service (e.g. `OPENAI_API_KEY` set but `/config` selected `deepseek`). |
| `Analysis failed. TradingAgents module not available.` in chat | Server-side import error — check `docker compose logs`. Usually a stale image; `docker compose pull` to refresh. |
| `/history` empty even after running analyses | `TRADINGAGENTS_RESULTS_DIR` defaults to a path inside the container that gets wiped on restart. Set it to `/app/data/ta-logs` (or similar under `/app/data`) so it persists via the bind mount. |
