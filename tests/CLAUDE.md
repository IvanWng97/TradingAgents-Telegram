# tests/ — pytest suite

240 scenarios across `test_*.py` files; default pytest discovery + `pytest-asyncio` in `auto` mode (so plain `async def test_*` runs without per-test markers). Run: `.venv/bin/python -m pytest tests/`. CI runs the same command via `.github/workflows/smoke.yml` and uploads `junit.xml` as a 7-day artifact.

## Layout

- `test_*.py` — one suite per subsystem (`test_cache`, `test_digest`, `test_runner`, `test_watchlist`, …).
- `_smoke_helpers.py` — shared fixtures (`FakeBot`, `FakeContext`, `set_smoke_data_dir`, `FakeTimedOut`). Used by `test_runner.py` + `test_runner_parallel.py` only — the unit-flavoured suites stay self-contained.
- `conftest.py` — cross-cutting setup (sys.path wiring + autouse session fixture).

Pytest config lives in `pyproject.toml` under `[tool.pytest.ini_options]` — `testpaths = ["tests"]`, `asyncio_mode = "auto"`. No `python_files` override; the default `test_*.py` rule applies.

## The `TG_BOT_DATA_DIR` module-import-time gotcha

Every suite needs `data/` isolation — the same-day result cache and the storage JSONs both read paths off `TG_BOT_DATA_DIR`, so a leaked tempdir between scenarios produces false greens (cache-hit short-circuits, stale watchlists). `set_smoke_data_dir(prefix)` in `_smoke_helpers.py` mkdtemp's a fresh path and reassigns the env var.

**Trap:** most suites call `set_smoke_data_dir(...)` at MODULE TOP. Pytest imports every collected module before running any test, so by run-time the env var points at whichever module imported LAST in collection order. Suites that *read* the cache from a clean slate get whatever the last-importing suite happened to seed.

**Fix:** if a test must see a guaranteed-empty tempdir, call `set_smoke_data_dir(...)` inside the test body. Canonical pattern: `test_runner_parallel.py::test_real_parallelism` (calls `set_smoke_data_dir("smoke_runner_parallel_")` first thing in the function body so the parallelism wall-clock check isn't short-circuited by a cache populated during `test_runner.py` collection).

Don't try to solve this with a global autouse fixture — the suites that rely on the module-top behavior would break, and the dynamic-set-from-body pattern is already established and grep-able.

## `FakeBot` / `FakeContext` conventions

`FakeBot` (`_smoke_helpers.py:46`) records every call. Methods are a deliberate superset of what either runner suite uses today — kitchen-sink so adding a new `bot.X` surface in `_run_analysis_for_ticker` doesn't require parallel edits across two test files.

Recording surfaces (all instance attributes, populated by the relevant `async def`):

| Attribute | Shape | Pinning purpose |
|---|---|---|
| `captions` | `dict[message_id, str]` | latest caption per message — most tests assert against this |
| `caption_history` | `list[(caption, parse_mode)]` | Invariant #4 — transient captions are MarkdownV2, final/cache-hit are HTML |
| `edit_markup_history` | `list[(caption, reply_markup)]` | Invariant #3 — every in-flight `editMessageCaption` re-attaches the ❌ Cancel keyboard |
| `documents` | `list[dict]` | `getmd` `.md` archival path via `chat.send_document` |
| `messages` | `list[dict]` | bot.send_message — `.md` fallback ("Markdown report unavailable") |

Failure injection: `FakeBot(send_photo_failures=N)` raises `FakeTimedOut` (named `"TimedOut"` so the `type(e).__name__` retry branch fires) on the first N `send_photo` attempts per logical caption. Exercises the retry path without depending on PTB's error hierarchy.

`FakeContext` (`_smoke_helpers.py:119`) is minimal — `bot` + `chat_data` dict + `bot_data` dict. That's all `_run_analysis_for_ticker` and the digest fan-out reach for; new state goes through one of those two dicts, not a new context attribute.

## When to extract a fixture into `_smoke_helpers.py`

**Only when 2+ suites share the exact same shape.** `FakeBot` + `FakeContext` qualify (both runner suites use them). Unit-flavoured suites (`test_cache`, `test_config_key`, `test_formatters`, `test_storage`, …) stay self-contained — adding bot fakes there would obscure what's being tested.

Resist the temptation to make `_smoke_helpers.py` a junk drawer for "kinda-shared" helpers. The small duplication is worth the per-suite readability when each file diverges on what it actually tests.

## `conftest.py` setup

Two cross-cutting concerns live here:

1. **`sys.path` wiring** — prepends `<repo>/src` so `tg_bot.*` imports resolve during collection (before any test file's module-top imports fire). The duplicate sys.path insert in each test file is intentional — both timings matter.

2. **`_disable_digest_llm_precheck`** (session-scoped, autouse) — no-ops `check_llm_configured` on `tg_bot.handlers.callbacks` AND `tg_bot.handlers.analysis_runner` (the two modules that bind it as a module-level import). Digest fan-out / cancel / progress tests assume the precheck is a pass-through; without this they'd all have to arm a provider + matching env var.
   - **Important asymmetry:** `tg_bot.pipeline.analysis.check_llm_configured` itself is NOT patched. `test_llm_precheck_*` scenarios call the source function directly and expect real behavior. Only the rebound module-level references in the two handler modules are no-op'd.

## "New behavior needs a scenario" — discipline rule

A new callback prefix, storage field, command, dataflow path, or Invariant from the root CLAUDE.md each need **at least one assertion** in `tests/test_*.py`. Non-negotiable — treat "no test scenario" as a merge blocker.

Two directions to watch:

- **Extending existing fixtures**: when a new gate or precondition is added, the runner suites may need fixture updates rather than new scenarios. Learned twice — H4 auth fail-closed required updating channel-post tests; the LLM precheck broke 7 fan-out tests until the conftest bypass landed.
- **New scenario per behavior**: extend an existing suite OR start `test_<subsystem>.py` for a wholly new surface. Every recent PR shipped one (PR #14 added `test_cache.py`, PR #15 added `test_user_config.py`, PR #16 added `test_watchlist.py`). Don't break the pattern.

**Drift audit** (post-refactor or quarterly): the per-PR rule is forward-looking; it does not catch invariants whose pinning lapsed when surrounding code moved. The `drift-audit.yml` workflow runs weekly and opens an issue with a punch list when gaps exist. Treat HIGH gaps (invariant unpinned) as merge blockers for the next PR that touches the implicated surface.

## Test framework choice rationale

Pytest was adopted in PR #62 by pivot from a bash aggregator (`scripts/run_smoke.sh`) to discovery-based scenarios. The migration was additive — the smoke files were already shaped as `async def test_*()` functions, so `[tool.pytest.ini_options]` + an autouse conftest fixture for the cross-cutting setup picked them up with zero rewrites to test bodies. PR #63 then renamed `scripts/smoke_*.py` → `tests/test_*.py` to match pytest convention and dropped the bash aggregator entirely.

Both moves caught latent test-vs-code drift that bash's stdout-only tailing had been silently hiding: a `fake_publish` missing the `edit_path` kwarg added to the real `publish_to_telegraph`; cross-test `TG_BOT_DATA_DIR` leakage under pytest's single-process model. Surfacing those was the proof that the framework switch was load-bearing, not cosmetic.
