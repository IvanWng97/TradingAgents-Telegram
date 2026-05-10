# Development

## Run from source

```bash
git clone https://github.com/IvanWng97/TradingAgents-Telegram.git
cd TradingAgents-Telegram

# 1. Set up venv + install
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure secrets — same .env contents as the Docker setup in the README
$EDITOR .env

# 3. (macOS only, once per fresh install) clear macOS UF_HIDDEN flag
#    on the editable .pth so Python 3.14 stops skipping it
chflags nohidden .venv/lib/python3.14/site-packages/*.pth

# 4. Run
python -m tg_bot
```

## Pre-commit

The `[dev]` extra adds `ruff` + `pre-commit`. Wire up the hook so format/lint runs on every commit:

```bash
pre-commit install   # one-time: lints/formats on every git commit
```

## Smoke tests

Smoke tests live under `scripts/`:

```bash
python scripts/smoke_concurrent.py    # 11 orchestration scenarios: queue, cancel, retry, shutdown
python scripts/smoke_parallel.py      # parallelism wall-time check (5 tickers, cap=5)
python scripts/smoke_digest.py        # 57 scenarios: storage, picker, fan-out, cancel, precheck
python scripts/smoke_cache.py         # 14 scenarios: same-day result cache + slug variants
python scripts/smoke_user_config.py   # 16 scenarios: rounds + effort knobs, build_user_config, config_summary
python scripts/smoke_watchlist.py     # 4 scenarios: /watch + /refresh shared picker
```

See [`CLAUDE.md`](../CLAUDE.md) for architecture notes, key contracts, and current limitations.
