# `/list` Grid Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the `/list` command's wobbling inline-codespan grid with a MarkdownV2 preformatted block that pads cells uniformly and adapts column count to the longest ticker, eliminating alignment breakage on long tickers (e.g. `RELIANCE.NS`) and inline `🔔` markers.

**Architecture:** Add a pure `_format_ticker_grid(watchlist)` helper that returns a triple-backtick block with padded cells and adaptive column count. Rewrite `_format_list_view(watchlist, digest, enrolled)` to render header lines (including a `→ T1, T2, ...` line listing enrolled tickers when a proper subset is enrolled) above the grid and a state-dependent footer below. The grid never contains `🔔` markers — they live in the header section.

**Tech Stack:** Python 3.12+, MarkdownV2 (Telegram Bot API), existing hand-rolled smoke harness at `scripts/smoke_watchlist.py`.

**Spec:** `docs/specs/2026-05-14-list-quick-view-redesign.md`

**Branch:** `feat/list-grid-redesign` (already created, off main, spec committed at `a5d5fce`).

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/tg_bot/handlers/commands.py` | modify | Add constants + `_format_ticker_grid` helper; rewrite `_format_list_view`. Function signatures of `_format_list_view` and `_digest_enrolled_set` unchanged. |
| `scripts/smoke_watchlist.py` | modify | Add 7 new test scenarios for the grid + state branches; update 3 existing `_format_list_view` tests to expect the new pre-block output. |
| `CLAUDE.md` (root) | modify | "Recently fixed" entry: rotate the get_enrolled_tickers entry into the body (it's already there) and replace with the grid redesign narrative. |
| `src/tg_bot/handlers/CLAUDE.md` | modify | Update the `/list` Commands-section bullet to mention pre-block grid + adaptive columns. |

---

## Task 1: Add `_format_ticker_grid` helper with TDD

**Files:**
- Modify: `src/tg_bot/handlers/commands.py` (add helper + constants near `_format_list_view`)
- Test: `scripts/smoke_watchlist.py` (add 5 helper scenarios)

### Step 1.1: Write failing tests for the helper

Open `scripts/smoke_watchlist.py`. After `test_list_format_digest_with_filter` (or at a sensible position alongside other `_format_*` tests), insert these 5 test functions:

- [ ] **Step 1.1: Insert helper test functions**

```python
# ─── _format_ticker_grid helper ─────────────────────────────────────────


async def test_grid_renders_inside_pre_block() -> None:
    """Grid output is wrapped in MarkdownV2 triple-backtick fences so the
    entire block is one monospace context. Without this, spaces BETWEEN
    inline code-spans render in the proportional message font and rows
    wobble."""
    _fresh_data_dir()
    _seed_storage(["AAPL"])
    commands = _reload_storage_singletons()

    text = commands._format_ticker_grid(["AAPL"])
    assert text.startswith("```\n"), repr(text[:10])
    assert text.endswith("\n```"), repr(text[-10:])


async def test_grid_short_tickers_4_cols() -> None:
    """Short US tickers (≤5 chars) → 4-column grid, cells padded to
    max_len + _GRID_GUTTER. With 4 tickers `AAPL NVDA TSLA MSFT`,
    cell_width = 4 + 2 = 6, ncols = min(4, 36//6) = 4 → 1 row."""
    _fresh_data_dir()
    _seed_storage(["AAPL"])
    commands = _reload_storage_singletons()

    text = commands._format_ticker_grid(["AAPL", "NVDA", "TSLA", "MSFT"])
    # Strip pre fences: "```\n" prefix (4 chars), "\n```" suffix (4 chars).
    body = text[4:-4]
    lines = body.split("\n")
    assert len(lines) == 1, lines
    # Each cell = "AAPL  ", joined → "AAPL  NVDA  TSLA  MSFT  ", rstripped
    # to "AAPL  NVDA  TSLA  MSFT".
    assert lines[0] == "AAPL  NVDA  TSLA  MSFT", repr(lines[0])


async def test_grid_8_tickers_wraps_to_2_rows() -> None:
    """8 short tickers at 4 cols → exactly 2 rows."""
    _fresh_data_dir()
    _seed_storage(["AAPL"])
    commands = _reload_storage_singletons()

    text = commands._format_ticker_grid(
        ["AAPL", "NVDA", "TSLA", "MSFT", "GOOG", "AMZN", "META", "NFLX"]
    )
    body = text[4:-4]
    lines = body.split("\n")
    assert len(lines) == 2, lines


async def test_grid_long_ticker_drops_to_2_cols() -> None:
    """Indian NSE-style ticker `RELIANCE.NS` (11 chars) forces
    cell_width = 13, ncols = min(4, 36//13) = 2. All rows pad to 13
    so alignment is uniform regardless of which row holds the long
    ticker."""
    _fresh_data_dir()
    _seed_storage(["AAPL"])
    commands = _reload_storage_singletons()

    text = commands._format_ticker_grid(
        ["AAPL", "NVDA", "RELIANCE.NS", "MSFT"]
    )
    body = text[4:-4]
    lines = body.split("\n")
    assert len(lines) == 2, lines
    # Row 1: "AAPL         NVDA         " → rstrip → "AAPL         NVDA"
    assert lines[0] == "AAPL         NVDA", repr(lines[0])
    # Row 2: "RELIANCE.NS  MSFT         " → rstrip → "RELIANCE.NS  MSFT"
    assert lines[1] == "RELIANCE.NS  MSFT", repr(lines[1])


async def test_grid_extreme_ticker_drops_to_1_col() -> None:
    """Pathological 20-char ticker → cell_width = 22, ncols = max(1, 36//22)
    = 1 → vertical list. The guard `max(1, ...)` prevents ncols=0 on
    impossibly long tickers."""
    _fresh_data_dir()
    _seed_storage(["AAPL"])
    commands = _reload_storage_singletons()

    text = commands._format_ticker_grid(["AAPL", "X" * 20])
    body = text[4:-4]
    lines = body.split("\n")
    assert len(lines) == 2, lines
    # cell_width = 22, both rows pad to 22 then rstrip
    assert lines[0].rstrip() == "AAPL"
    assert lines[1].rstrip() == "X" * 20
```

Also add to the `SCENARIOS` list (find the existing list at the bottom of the file, append):

```python
    # --- _format_ticker_grid helper ---
    ("_format_ticker_grid: wrapped in pre block", test_grid_renders_inside_pre_block),
    ("_format_ticker_grid: 4 short tickers → 4 cols, 1 row", test_grid_short_tickers_4_cols),
    ("_format_ticker_grid: 8 short tickers → 2 rows", test_grid_8_tickers_wraps_to_2_rows),
    ("_format_ticker_grid: RELIANCE.NS → drops to 2 cols", test_grid_long_ticker_drops_to_2_cols),
    ("_format_ticker_grid: 20-char ticker → 1 col", test_grid_extreme_ticker_drops_to_1_col),
```

- [ ] **Step 1.2: Run tests to verify they fail**

Run: `.venv/bin/python scripts/smoke_watchlist.py`
Expected: All 5 new scenarios fail with `AttributeError: module 'tg_bot.handlers.commands' has no attribute '_format_ticker_grid'`. Existing 10 scenarios still pass.

- [ ] **Step 1.3: Implement the helper + constants**

Open `src/tg_bot/handlers/commands.py`. Find the existing `_LIST_ROW_WIDTH = 4` constant and the surrounding `_format_list_view` function (around line 350–400 — verify with grep).

Replace the constant `_LIST_ROW_WIDTH = 4` with this block:

```python
# Pre-block grid layout for /list. The whole grid renders inside a
# MarkdownV2 triple-backtick block so spaces inside it stay monospace —
# inline code-span padding wobbles because Telegram renders BETWEEN-span
# whitespace in the proportional message font.
_GRID_GUTTER = 2          # spaces between cells in the grid
_GRID_TARGET_WIDTH = 36   # mobile-safe target line width (≈ iPhone SE)
_GRID_MAX_COLS = 4        # cap regardless of viewport


def _format_ticker_grid(watchlist: list[str]) -> str:
    """Render a ticker grid inside a MarkdownV2 pre block.

    Cell width = max(len(t) for t in watchlist) + _GRID_GUTTER. Column
    count is clamped so a row fits within _GRID_TARGET_WIDTH characters
    on mobile viewports; falls back to 1 column on pathologically long
    tickers (≥ _GRID_TARGET_WIDTH chars).

    All cells in all rows pad to the same width, so a long ticker like
    `RELIANCE.NS` does not push subsequent cells out of column
    alignment — the entire grid widens uniformly.

    Tickers can only contain `[A-Z0-9.\\-]` (enforced by validation.py:
    TICKER_RE), so no escaping is needed inside the pre block — neither
    `\\`` nor `\\` characters can appear.
    """
    cell_width = max(len(t) for t in watchlist) + _GRID_GUTTER
    ncols = max(1, min(_GRID_MAX_COLS, _GRID_TARGET_WIDTH // cell_width))
    rows: list[str] = []
    for i in range(0, len(watchlist), ncols):
        row = "".join(f"{t:<{cell_width}}" for t in watchlist[i : i + ncols])
        rows.append(row.rstrip())
    return "```\n" + "\n".join(rows) + "\n```"
```

- [ ] **Step 1.4: Run tests to verify they pass**

Run: `.venv/bin/python scripts/smoke_watchlist.py`
Expected: All 15 scenarios pass (10 existing + 5 new helper).

- [ ] **Step 1.5: Commit**

```bash
git add src/tg_bot/handlers/commands.py scripts/smoke_watchlist.py
git commit -m "feat(/list): add _format_ticker_grid pre-block helper

Pure helper that builds the MarkdownV2 triple-backtick grid for the
/list view. Cell width = max(len(t)) + 2, columns adaptive to fit
~36 chars (mobile-safe), capped at 4. RELIANCE.NS (11 chars) drops
the grid to 2 cols cleanly; 20-char ticker drops to 1 col.

Not yet wired into _format_list_view — that lands in the next task.
5 new smoke scenarios pin the helper contract.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Rewrite `_format_list_view` to use the new grid

**Files:**
- Modify: `src/tg_bot/handlers/commands.py` (rewrite `_format_list_view`)
- Test: `scripts/smoke_watchlist.py` (update 3 existing tests + add 2 state-branch tests)

### Step 2.1: Update existing `_format_list_view` smoke tests

Open `scripts/smoke_watchlist.py`. Find the three existing tests and replace their assertions to match the new output shape.

- [ ] **Step 2.1: Update `test_list_format_no_digest`**

Replace the existing body of `test_list_format_no_digest` with:

```python
async def test_list_format_no_digest() -> None:
    """`_format_list_view`: watchlist + None digest → no digest header,
    grid in pre block, footer says 'off'. Pure formatter test — no
    storage involvement."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA"])
    commands = _reload_storage_singletons()

    text = commands._format_list_view(["AAPL", "NVDA"], None, None)
    # Header
    assert "Watchlist" in text and "2 tickers" in text, text
    assert "Digest" not in text, "no digest header should render"
    # Grid in pre block
    assert "```\n" in text and "\n```" in text, "grid must be in pre block"
    # Footer
    assert "Daily digest off" in text, text
    # No bell markers anywhere
    assert "🔔" not in text, "no bell markers when digest off"
```

- [ ] **Step 2.2: Update `test_list_format_digest_all_watchlist`**

Replace with:

```python
async def test_list_format_digest_all_watchlist() -> None:
    """`_format_list_view`: enrolled == set(watchlist) (legacy save) →
    digest header reads 'all N fire daily', no per-ticker bell markers
    needed (every ticker is enrolled), grid stays clean."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA"])
    commands = _reload_storage_singletons()

    digest = {
        "enabled": True,
        "hour_local": 9,
        "tz": "America/Los_Angeles",
        "chat_id": 999,
    }
    enrolled = {"AAPL", "NVDA"}

    text = commands._format_list_view(["AAPL", "NVDA"], digest, enrolled)
    # Digest header line present
    assert "Digest" in text and "09:00" in text, text
    assert "all 2 fire daily" in text, text
    # Grid in pre block
    assert "```\n" in text and "\n```" in text, "grid must be in pre block"
    # No per-ticker bell in the grid block — the digest header has its own bell
    # icon, but rows in the grid must not.
    grid_start = text.index("```\n") + 4
    grid_end = text.index("\n```", grid_start)
    grid_body = text[grid_start:grid_end]
    assert "🔔" not in grid_body, f"grid must not contain bell markers: {grid_body!r}"
    # No "→" enrolled-tickers list either (all-enrolled case skips it)
    assert "→" not in text, text
```

- [ ] **Step 2.3: Update `test_list_format_digest_with_filter`**

Replace with:

```python
async def test_list_format_digest_with_filter() -> None:
    """`_format_list_view`: enrolled is a proper subset of watchlist →
    header has the digest line AND an indented `→ T1, T2` line naming
    the enrolled tickers. Grid stays clean (no inline markers)."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA", "TSLA", "MSFT"])
    commands = _reload_storage_singletons()

    digest = {
        "enabled": True,
        "hour_local": 8,
        "tz": "America/New_York",
        "chat_id": 999,
        "tickers": ["AAPL", "TSLA"],
    }
    enrolled = {"AAPL", "TSLA"}

    text = commands._format_list_view(
        ["AAPL", "NVDA", "TSLA", "MSFT"], digest, enrolled
    )
    # Header
    assert "Digest" in text and "08:00" in text, text
    # Enrolled tickers named in the → line (not in the grid)
    assert "→" in text, "expected '→ T1, T2' header line for subset"
    assert "AAPL" in text and "TSLA" in text
    # Grid in pre block
    assert "```\n" in text and "\n```" in text, "grid must be in pre block"
    # Grid body has no bell markers
    grid_start = text.index("```\n") + 4
    grid_end = text.index("\n```", grid_start)
    grid_body = text[grid_start:grid_end]
    assert "🔔" not in grid_body, (
        f"per-ticker bell markers must not appear in grid: {grid_body!r}"
    )
```

- [ ] **Step 2.4: Add new state-branch tests**

After the three updated tests, insert these two new ones:

```python
async def test_list_format_digest_zero_enrolled() -> None:
    """`_format_list_view`: digest enabled but no tickers enrolled
    (empty filter set, K=0) → digest header line present (so user sees
    the schedule), no `→` line (nothing to list), footer reminds the
    user to fix their filter."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA"])
    commands = _reload_storage_singletons()

    digest = {
        "enabled": True,
        "hour_local": 9,
        "tz": "UTC",
        "chat_id": 999,
        "tickers": [],
    }
    enrolled: set[str] = set()

    text = commands._format_list_view(["AAPL", "NVDA"], digest, enrolled)
    assert "Digest" in text and "09:00" in text, text
    assert "→" not in text, "no '→' line when enrolled set is empty"
    assert "Digest enabled but no tickers enrolled" in text, text


async def test_list_format_no_inline_backticks_per_ticker() -> None:
    """The new grid uses a single triple-backtick pre block, NOT per-
    ticker inline backticks. Verifies the layout regression doesn't
    accidentally revert to inline code-span styling, which is what
    caused the original alignment wobble."""
    _fresh_data_dir()
    _seed_storage(["AAPL", "NVDA", "TSLA"])
    commands = _reload_storage_singletons()

    text = commands._format_list_view(["AAPL", "NVDA", "TSLA"], None, None)
    grid_start = text.index("```\n") + 4
    grid_end = text.index("\n```", grid_start)
    grid_body = text[grid_start:grid_end]
    # No backticks inside the grid body (which would imply nested
    # inline code spans — wrong format).
    assert "`" not in grid_body, f"no inline backticks in grid: {grid_body!r}"
```

Append to the `SCENARIOS` list:

```python
    (
        "_format_list_view: digest on, zero enrolled → reminder footer",
        test_list_format_digest_zero_enrolled,
    ),
    (
        "_format_list_view: grid uses pre block, no inline backticks",
        test_list_format_no_inline_backticks_per_ticker,
    ),
```

- [ ] **Step 2.5: Run tests to verify the 3 updated + 2 new ones fail**

Run: `.venv/bin/python scripts/smoke_watchlist.py`
Expected:
- 5 helper tests still pass (from Task 1)
- 3 updated `_format_list_view` tests FAIL (current implementation uses inline backticks per ticker, not a pre block, and lacks the `→` line)
- 2 new state-branch tests FAIL for the same reason
- Other tests (watch/refresh picker tests, /add reply test, empty test, handler test) still pass

- [ ] **Step 2.6: Rewrite `_format_list_view`**

Open `src/tg_bot/handlers/commands.py`. Find `_format_list_view` and replace its body. The function signature stays the same:

```python
def _format_list_view(
    watchlist: list[str],
    digest: dict | None,
    enrolled: set[str] | None,
) -> str:
    """MarkdownV2 view for `/list`.

    Composition:
      - Title line: "📋 *Watchlist* — N tickers"
      - Digest header (when enabled is true): "🔔 *Digest* — HH:00 TZ"
        plus either "· all N fire daily" suffix (legacy all-enrolled),
        or an indented "   → `T1`, `T2`, ..." subset line below.
      - Grid in a MarkdownV2 pre block. Bullet (🔔) markers are NEVER
        in the grid — they would break monospace alignment.
      - Footer: state-dependent reminder when digest is off OR enabled
        but no tickers enrolled.
    """
    parts: list[str] = []
    parts.append(f"📋 *Watchlist* — {len(watchlist)} tickers")

    # Digest header section
    if digest is not None and enrolled is not None:
        hour = digest.get("hour_local", 0)
        tz_label = tz_short(digest.get("tz"))
        # tz_label may contain a slash from raw IANA fallback ("America/Los_Angeles")
        # which is MarkdownV2-special and must be escaped outside code spans.
        safe_tz = escape_markdown(tz_label, version=2)

        all_watchlist = enrolled == set(watchlist)
        if all_watchlist:
            parts.append(
                f"🔔 *Digest* — `{hour:02d}:00` {safe_tz} · "
                f"all {len(watchlist)} fire daily"
            )
        else:
            parts.append(f"🔔 *Digest* — `{hour:02d}:00` {safe_tz}")
            # Only emit the "→ T1, T2" line when there's at least one
            # enrolled ticker. Empty enrolled set gets a footer reminder
            # instead (see below) — the picker UX is already a tap away.
            if enrolled:
                # Watchlist order preserved (sorted on-disk per
                # set_digest_tickers). Each ticker in monospace code span.
                cells = ", ".join(f"`{t}`" for t in watchlist if t in enrolled)
                parts.append(f"   → {cells}")

    parts.append("")  # blank line between header and grid
    parts.append(_format_ticker_grid(watchlist))

    # Footer section
    if digest is None:
        parts.append("")
        parts.append("_Daily digest off — use /digest to enable\\._")
    elif enrolled is not None and not enrolled:
        # Digest enabled but filter set excludes the entire watchlist.
        # The fan-out treats this as "nothing to do" and sends a reminder;
        # surface that state here so the user understands why.
        parts.append("")
        parts.append(
            "_Digest enabled but no tickers enrolled — /digest to fix\\._"
        )

    return "\n".join(parts)
```

- [ ] **Step 2.7: Run tests to verify they pass**

Run: `.venv/bin/python scripts/smoke_watchlist.py`
Expected: All 17 scenarios pass (10 original − 0 removed + 5 helper + 2 new state branches = 17 total in this suite).

Also run lint:
Run: `.venv/bin/python -m ruff check src/tg_bot/handlers/commands.py scripts/smoke_watchlist.py`
Expected: All checks passed!

And formatting:
Run: `.venv/bin/python -m ruff format --check src/tg_bot/handlers/commands.py scripts/smoke_watchlist.py`
Expected: 2 files already formatted (or, if changes are needed, run without `--check` to format).

- [ ] **Step 2.8: Run the full smoke suite as a regression gate**

Run: `bash scripts/run_smoke.sh`
Expected: All 11 suites green, total scenarios ~219 (was 214; helper +5, state branches +2; -2 if some old test asserted inline backticks that I removed — verify actual count from the output).

If anything outside `smoke_watchlist.py` fails, STOP and investigate — there may be a coupling between `_format_list_view`'s output shape and some assertion in another suite.

- [ ] **Step 2.9: Commit**

```bash
git add src/tg_bot/handlers/commands.py scripts/smoke_watchlist.py
git commit -m "feat(/list): rewrite _format_list_view to use pre-block grid

The inline-codespan grid wobbled because MarkdownV2 renders
whitespace BETWEEN code spans in the proportional message font, not
monospace. Adding a long ticker like RELIANCE.NS or the 🔔
enrollment marker pushed cells out of alignment.

New layout:
  📋 *Watchlist* — N tickers
  🔔 *Digest* — HH:00 TZ                    [omitted if digest off]
     → \`T1\`, \`T2\`, ...                    [only on proper subset]
  \`\`\`<grid>\`\`\`
  _footer_                                  [state-dependent]

The grid is a single triple-backtick pre block, so its internal
spacing is uniformly monospace. Markers (🔔) live in the header
section, never inside the grid. Cell width adapts to the longest
ticker in the watchlist; column count clamps to fit a mobile-safe
~36-char line.

Three existing _format_list_view tests updated to expect the new
shape; two new tests added for the zero-enrolled state branch and
the no-inline-backticks invariant.

Spec: docs/specs/2026-05-14-list-quick-view-redesign.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Update CLAUDE.md docs

**Files:**
- Modify: `CLAUDE.md` (root) — Recently fixed entry
- Modify: `src/tg_bot/handlers/CLAUDE.md` — /list Commands-section bullet

- [ ] **Step 3.1: Update handlers/CLAUDE.md `/list` bullet**

Open `src/tg_bot/handlers/CLAUDE.md`. Find the bullet starting with `**`/list` is read-only; `/watch` is the picker.**`. Replace it (find the existing description ending with "...since the storage method deliberately doesn't gate on `enabled`.") with:

```markdown
- **`/list` is read-only; `/watch` is the picker.** Until PR #56 they were aliases (both opened the picker). Now `/list` produces a MarkdownV2 text view: title + optional digest header + a ticker grid inside a triple-backtick pre block (so spaces inside are guaranteed monospace, fixing the alignment wobble that inline-codespan grids inherit). Grid cells pad to `max(len(t)) + 2`; column count adapts to fit ~36 chars on mobile viewports (4 cols for short US tickers, drops to 2 when a long ticker like `RELIANCE.NS` lands in the watchlist, 1 col for pathological 20+ char tickers). 🔔 enrollment markers live in the header section (an indented `→ T1, T2` line listing enrolled tickers), never in the grid — inline markers would break the monospace padding. Empty watchlist short-circuits to a `/add` nudge. No callbacks, no chat_data, no inline keyboard — pure storage read + format + reply. The digest-enrolment intersection is computed by `user_config_storage.get_enrolled_tickers` (shared with `run_user_digest` — that method is the single canonical evaluator for "what tickers does this user's digest fire today"). The helper `_digest_enrolled_set` wraps it to add a `None` sentinel for "digest off entirely" (different footer copy from "digest on but enrolled set is empty"), since the storage method deliberately doesn't gate on `enabled`.
```

- [ ] **Step 3.2: Update root CLAUDE.md "Recently fixed" entry**

Open `CLAUDE.md` (project root). Find the "## Recently fixed" section. The current entry is about `get_enrolled_tickers` extraction (or about the folder reorg — whichever was the last to land). The convention per the section header is: "Curated narrative for the latest non-trivial PR. Older entries graduate into the body sections above on the next PR cycle — git log carries the full history."

Replace the existing "Recently fixed" bullet with:

```markdown
- **`/list` grid wobble fixed — pre-block layout + adaptive columns.** The Markdown V2 inline-codespan grid wobbled because Telegram renders the whitespace BETWEEN inline code spans in the proportional message font, not monospace. Adding an 11-char Indian ticker (`RELIANCE.NS`) made it visually broken; the inline `🔔` enrollment markers shifted column positions further. New layout wraps the grid in a single triple-backtick MarkdownV2 pre block (one monospace context for the whole grid), pads every cell to `max(len(t)) + 2`, and adapts column count to fit a mobile-safe ~36-char line (4 cols for short tickers, drops to 2 for long, 1 for extreme). 🔔 markers move OUT of the grid into a `→ T1, T2` header line listing enrolled tickers — the grid stays uniformly aligned regardless of enrollment state. New `_format_ticker_grid` helper in `commands.py` carries the pure grid-building logic; `_format_list_view` composes header + grid + footer. 7 new smoke scenarios in `smoke_watchlist.py` (5 for the helper covering 4-col / 2-col / 1-col / 8-ticker / pre-block-fence; 2 new for the zero-enrolled state branch and the no-inline-backticks invariant); 3 existing `_format_list_view` tests updated to expect the new output shape.
```

If the existing entry is about the folder reorg, that one should ALREADY have rotated. Verify what's there first; only the most recent entry stays. If you find the reorg entry still present, KEEP it and add this new one BELOW (the section can hold one or two entries during transitions per the existing pattern).

- [ ] **Step 3.3: Commit docs**

```bash
git add CLAUDE.md src/tg_bot/handlers/CLAUDE.md
git commit -m "docs: /list grid redesign — Layout + Commands updates

handlers/CLAUDE.md: rewrite the /list bullet to reflect the pre-block
grid + adaptive columns. Calls out the root cause (inline-codespan
spacing renders proportional, not monospace) so future contributors
know why we don't go back.

Root CLAUDE.md: rotate the 'Recently fixed' entry to describe this
change.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Final verification + open PR

- [ ] **Step 4.1: Final lint + smoke pass**

Run:
```bash
.venv/bin/python -m ruff check src/ scripts/
.venv/bin/python -m ruff format --check src/ scripts/
bash scripts/run_smoke.sh
```

Expected:
- Ruff check: All checks passed!
- Ruff format: all files already formatted
- Smoke: All suites passed. Total scenarios ~219 (verify exact count from the output).

If any check fails, STOP and fix before opening the PR. Do not bypass.

- [ ] **Step 4.2: Push the branch**

```bash
git push -u origin feat/list-grid-redesign
```

- [ ] **Step 4.3: Open the PR**

```bash
gh pr create --base main --head feat/list-grid-redesign \
  --title "feat(/list): pre-block grid + adaptive columns" \
  --body "$(cat <<'EOF'
## Summary

Fixes the grid alignment wobble in `/list` output by rendering the
ticker grid inside a MarkdownV2 triple-backtick pre block, padding
cells to a uniform width, and adapting column count to fit mobile
viewports. 🔔 enrollment markers move out of the grid into a header
section.

## Why

Telegram renders whitespace **between** inline code spans in the
proportional message font, not monospace. The previous layout
(per-ticker `` `T1` `` followed by spaces followed by `` `T2` ``)
wobbled even without any markers. Adding an 11-char Indian ticker
(`RELIANCE.NS`) or the inline `🔔` enrollment marker made the
misalignment visually severe.

## What changed

- `src/tg_bot/handlers/commands.py`
  - New constants: `_GRID_GUTTER`, `_GRID_TARGET_WIDTH`, `_GRID_MAX_COLS`
  - New pure helper: `_format_ticker_grid(watchlist) -> str`
  - Rewrote `_format_list_view` to compose header + pre-block grid +
    footer; `→ T1, T2` line lists enrolled tickers when a proper
    subset is enrolled
- `scripts/smoke_watchlist.py`
  - 5 new helper scenarios (4-col / 2-col / 1-col / 8-ticker / pre-block fence)
  - 2 new state-branch scenarios (zero enrolled, no inline backticks)
  - 3 existing `_format_list_view` tests updated for the new output shape
- `CLAUDE.md` (root) + `src/tg_bot/handlers/CLAUDE.md` — updated descriptions

## Test plan

- [x] All 11 smoke suites pass (~219 scenarios total)
- [x] `ruff check` + `ruff format --check` clean
- [ ] Manual phone test: `/list` with current 33-ticker watchlist
- [ ] Stress test: `/add RELIANCE.NS` → `/list` → drops to 2 cols cleanly

## Spec

`docs/specs/2026-05-14-list-quick-view-redesign.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4.4: Watch the PR's auto-review**

Run: `gh pr view --json url --jq '.url'` to get the PR URL. Then watch the latest Claude Review run:

```bash
gh run list --workflow=claude-review.yml --limit 1 --json databaseId --jq '.[0].databaseId' | xargs -I {} gh run watch {} --exit-status
```

Expected: review run succeeds, posts a sticky summary + any inline findings on the PR. Review the findings before requesting merge.

---

## Done criteria

- [x] Spec at `docs/specs/2026-05-14-list-quick-view-redesign.md`
- [ ] `_format_ticker_grid` helper added with 5 smoke scenarios
- [ ] `_format_list_view` rewritten using the helper; header section emits `→ T1, T2` for proper subsets
- [ ] 3 existing `_format_list_view` tests updated; 2 new state-branch tests added
- [ ] `ruff check + ruff format --check` clean
- [ ] Full smoke suite green
- [ ] Root + handlers/ CLAUDE.md updated
- [ ] PR opened; Claude auto-review posted
- [ ] Manual phone test confirms layout is clean with the live 33-ticker watchlist + RELIANCE.NS stress test
