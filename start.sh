#!/usr/bin/env bash
# TradingAgents-Telegram — guided one-shot installer.
#
# Usage (run from anywhere):
#   bash <(curl -fsSL https://raw.githubusercontent.com/IvanWng97/TradingAgents-Telegram/main/start.sh)
#
# Walks you through:
#   1. Telegram bot token (from @BotFather)
#   2. Your Telegram user ID (from @userinfobot)
#   3. Telegraph access token (auto-create or paste your own)
#   4. Pick an LLM provider + paste its API key (canonical deep/quick models
#      written to .env — edit later to switch)
#   5. Drop a configured .env + docker-compose.yml in ./tradingagents-telegram
#   6. Pull the prebuilt Docker image
# After it finishes, the script prints the exact `docker compose up -d` command
# plus the in-Telegram next steps.

set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/IvanWng97/TradingAgents-Telegram/main"
INSTALL_DIR="${INSTALL_DIR:-tradingagents-telegram}"

# Color helpers — degrade gracefully when not on a TTY.
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'; BLUE=$'\033[0;34m'; GREEN=$'\033[0;32m'
    YELLOW=$'\033[0;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
else
    BOLD=""; BLUE=""; GREEN=""; YELLOW=""; RED=""; NC=""
fi
step() { printf "\n${BOLD}${BLUE}▸ %s${NC}\n" "$1"; }
ask()  { printf "${YELLOW}? %s${NC} " "$1"; }
ok()   { printf "${GREEN}✓${NC} %s\n" "$1"; }
warn() { printf "${RED}!${NC} %s\n" "$1" >&2; }

# Always read from the controlling tty, so `bash <(curl …)` AND `curl … | bash`
# both work — even when stdin is the script body.
if [[ ! -t 0 && ! -e /dev/tty ]]; then
    warn "No interactive terminal detected. Run from a real shell, e.g.:"
    warn "  bash <(curl -fsSL $REPO_RAW/start.sh)"
    exit 1
fi
read_tty()    { read -r "$@" < /dev/tty; }
read_secret() { read -rs "$@" < /dev/tty; echo; }

# Preflight: docker + curl required.
command -v docker >/dev/null 2>&1 || {
    warn "Docker not installed. Get it from https://docs.docker.com/get-docker/"
    exit 1
}
command -v curl >/dev/null 2>&1 || {
    warn "curl not installed."
    exit 1
}

# Choose install location.
if [[ -e "$INSTALL_DIR" && -f "$INSTALL_DIR/.env" ]]; then
    warn "$INSTALL_DIR/.env already exists. Re-running will OVERWRITE it."
    ask "Continue? [y/N]:"
    read_tty CONFIRM
    [[ "$CONFIRM" == "y" || "$CONFIRM" == "Y" ]] || { warn "Aborting."; exit 1; }
fi
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# ─── Step 1 — Telegram bot token ─────────────────────────────────────────
step "Step 1/4 — Telegram bot token"
echo "  DM @BotFather, send /newbot, follow the prompts."
echo "  The token looks like:  12345678:AAH..."
while :; do
    ask "Paste your TELEGRAM_BOT_TOKEN (input hidden):"
    read_secret TG_TOKEN
    if [[ -n "$TG_TOKEN" && "$TG_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
        ok "Got it."
        break
    fi
    warn "That doesn't look like a bot token (expected 12345:AAA...). Try again."
done

# ─── Step 2 — Telegram user ID ───────────────────────────────────────────
step "Step 2/4 — Your Telegram user ID"
echo "  DM @userinfobot any message — it replies with your numeric ID."
while :; do
    ask "Your Telegram user ID:"
    read_tty TG_USER_ID
    if [[ "$TG_USER_ID" =~ ^[0-9]+$ ]]; then
        ok "Got it."
        break
    fi
    warn "All digits, no quotes. Try again."
done

# ─── Step 3 — Telegraph access token ─────────────────────────────────────
step "Step 3/4 — Telegraph access token"
echo "  Used to publish each analysis as a Telegraph page."
echo "  Hit Enter to auto-create a token, or paste an existing one."
ask "Token (or Enter for auto-create):"
read_tty TG_GRAPH_TOKEN
if [[ -z "$TG_GRAPH_TOKEN" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
        warn "python3 not found — can't parse Telegraph response. Paste a token instead."
        exit 1
    fi
    TG_GRAPH_TOKEN=$(
        curl -fsSL "https://api.telegra.ph/createAccount?short_name=TradingAgentsBot&author_name=TradingAgentsBot" \
            | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["access_token"])'
    )
    ok "Created Telegraph account; token captured."
else
    ok "Got it."
fi

# ─── Step 4 — LLM provider + API key ─────────────────────────────────────
step "Step 4/4 — LLM provider + API key"
echo "  Pick the provider you'll use most. Edit .env later to switch."
echo "  Cost per analysis varies by provider — deepseek is cheapest, frontier"
echo "  models (gpt-5.x, claude-opus, gemini-pro) cost \$0.20-\$0.40+ per run."
echo
# Each row: name|api_key_env|deep_think_model|quick_think_model
# Models are the canonical first catalog entry from upstream tradingagents;
# they're written to .env so the bot uses them out of the box. Edit .env
# afterward to pick different models from the upstream catalog.
PROVIDERS=(
    "deepseek|DEEPSEEK_API_KEY|deepseek-v4-pro|deepseek-v4-flash"
    "openai|OPENAI_API_KEY|gpt-5.5|gpt-5.4-mini"
    "anthropic|ANTHROPIC_API_KEY|claude-opus-4-7|claude-sonnet-4-6"
    "google|GOOGLE_API_KEY|gemini-3.1-pro-preview|gemini-3-flash-preview"
    "xai|XAI_API_KEY|grok-4.20-reasoning|grok-4.20-non-reasoning"
    "qwen|DASHSCOPE_API_KEY|qwen3.6-plus|qwen3.6-flash"
    "qwen-cn|DASHSCOPE_CN_API_KEY|qwen3.6-plus|qwen3.6-flash"
    "glm|ZHIPU_API_KEY|glm-5.1|glm-5-turbo"
    "glm-cn|ZHIPU_CN_API_KEY|glm-5.1|glm-5-turbo"
    "minimax|MINIMAX_API_KEY|MiniMax-M2.7|MiniMax-M2.7-highspeed"
    "minimax-cn|MINIMAX_CN_API_KEY|MiniMax-M2.7|MiniMax-M2.7-highspeed"
    "ollama||glm-4.7-flash:latest|qwen3:latest"
)
for i in "${!PROVIDERS[@]}"; do
    IFS='|' read -r NAME _ _ _ <<< "${PROVIDERS[$i]}"
    printf "  %d) %s\n" "$((i+1))" "$NAME"
done
while :; do
    ask "Pick 1-${#PROVIDERS[@]}:"
    read_tty CHOICE
    if [[ "$CHOICE" =~ ^[1-9][0-9]*$ ]] && (( CHOICE >= 1 && CHOICE <= ${#PROVIDERS[@]} )); then
        break
    fi
    warn "Out of range."
done
IFS='|' read -r PROVIDER_NAME KEY_NAME DEEP_MODEL QUICK_MODEL <<< "${PROVIDERS[$((CHOICE-1))]}"
if [[ -n "$KEY_NAME" ]]; then
    while :; do
        ask "Paste your $KEY_NAME (input hidden):"
        read_secret KEY_VAL
        [[ -n "$KEY_VAL" ]] && break
        warn "Empty value."
    done
    PROVIDER_LINE="$KEY_NAME=$KEY_VAL"
else
    PROVIDER_LINE="# ollama — runs locally, no API key needed"
fi
ok "Provider: $PROVIDER_NAME (deep=$DEEP_MODEL, quick=$QUICK_MODEL)."

# ─── Materialize files ───────────────────────────────────────────────────
step "Writing config"
if [[ ! -f docker-compose.yml ]]; then
    curl -fsSL -o docker-compose.yml "$REPO_RAW/docker-compose.yml"
    ok "Fetched docker-compose.yml"
fi

cat > .env <<EOF
# Generated by start.sh on $(date -u +'%Y-%m-%dT%H:%M:%SZ')
TELEGRAM_BOT_TOKEN=$TG_TOKEN
TELEGRAPH_ACCESS_TOKEN=$TG_GRAPH_TOKEN
ALLOWED_USER_IDS=$TG_USER_ID

$PROVIDER_LINE

# tradingagents config — .env is the single source of truth (no /config UI).
# Edit these to pick different catalog entries, then restart the bot.
TRADINGAGENTS_LLM_PROVIDER=$PROVIDER_NAME
TRADINGAGENTS_DEEP_THINK_LLM=$DEEP_MODEL
TRADINGAGENTS_QUICK_THINK_LLM=$QUICK_MODEL

# Persist /history + yfinance cache via the bind-mounted ./data volume.
TRADINGAGENTS_RESULTS_DIR=/app/data/ta-logs
TRADINGAGENTS_CACHE_DIR=/app/data/ta-cache
EOF
chmod 600 .env
ok "Wrote .env (chmod 600 — readable only by you)"

# ─── Pull image ──────────────────────────────────────────────────────────
step "Pulling Docker image"
docker-compose pull

# ─── Final message ───────────────────────────────────────────────────────
HERE="$(pwd)"
cat <<EOF

${GREEN}${BOLD}✓ Setup complete.${NC}

You're configured at: ${BOLD}$HERE${NC}

${BOLD}Next:${NC}

  cd $HERE
  docker-compose up -d         # start the bot
  docker-compose logs -f       # follow logs (Ctrl-C to detach)

${BOLD}Then in Telegram, message your bot:${NC}

  /start                       # confirm auth works
  /status                      # see active LLM provider + models from .env
  /add NVDA AAPL               # add tickers
  /watch                       # tap Done to run your first analysis
  /digest                      # (optional) schedule a daily auto-run

${YELLOW}Tip:${NC} if /start says "Not authorized", your user ID isn't in ALLOWED_USER_IDS.
     Edit $HERE/.env, then \`docker-compose up -d\` to restart.
     Edit TRADINGAGENTS_DEEP_THINK_LLM / TRADINGAGENTS_QUICK_THINK_LLM to switch models.

EOF
