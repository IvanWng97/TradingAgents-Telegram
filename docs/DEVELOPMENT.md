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

## Tests

Tests live under `tests/` (pytest standard layout). 235 scenarios as of 2026-05-17, spanning storage, config-key, cache, watchlist, validation, formatters, runner orchestration, runner parallelism, digest fan-out, telegraph publishing, and auth gate. The full suite runs in ~25s:

```bash
pytest tests/                                  # everything
pytest tests/test_cache.py                     # one file
pytest tests/test_runner.py::test_basic_queue  # one scenario
pytest tests/ -v --tb=short                    # CI's invocation
```

This is also what `.github/workflows/smoke.yml` runs on every PR, with a `junit.xml` archived as a build artifact.

See [`CLAUDE.md`](../CLAUDE.md) for architecture notes, key contracts, and current limitations.
