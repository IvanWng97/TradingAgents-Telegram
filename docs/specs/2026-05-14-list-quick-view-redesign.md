# `/list` Quick-View Redesign

**Status:** approved, ready for implementation
**Date:** 2026-05-14
**Author/owner:** maintainer + Claude
**Implementation target:** `src/tg_bot/handlers/commands.py:_format_list_view`

## Context

`/list` produces a paginated text view of the user's watchlist with
digest-enrollment markers. In production with a real 33-ticker
watchlist, two visual problems surfaced:

1. **Inline `🔔` markers shift columns out of alignment.** Rows
   without markers don't line up with rows that have them.
2. **Spaces between MarkdownV2 inline code spans render in the
   proportional message font, not monospace.** Even without markers,
   the grid wobbles slightly across rows because `` ` `` `ASTS` `` ` ``
   followed by N spaces followed by `` ` `` `ATI` `` ` `` has its
   inter-span spacing rendered proportionally.

Stress test that broke the design wide open: a long ticker like
`RELIANCE.NS` (11 chars) lands in column 1 and pushes the remaining
cells in that row 6–7 character widths to the right. The current
implementation is structurally fragile to ticker-length variance.

## Goals

- Eliminate grid alignment issues for tickers up to ~12 chars (Indian
  NSE format, e.g. `RELIANCE.NS`, `HDFCBANK.NS`).
- Keep `/list` *quick*: sub-100ms render, single Telegram message,
  zero extra taps.
- Preserve digest-enrollment visibility (which tickers fire daily).
- Adapt to mobile viewports (~30–40 chars per monospace line) without
  truncation or wrapping.

## Non-goals

- Rich portfolio dashboard with prices, sectors, P&L. That's a future
  `/dashboard` or `/portfolio` command and Telegraph is the right
  surface for it.
- Telegraph page publishing for `/list`. The indirection (extra tap +
  publish latency + IV caching) violates the "quick" design goal.
- Per-ticker interactive buttons. `/list` is read-only by design.
- Truncating long ticker names. Ambiguous; would require tap-to-expand
  which Telegram doesn't support cleanly.

## Design

Three changes compose into the fix:

### 1. Render the grid inside a MarkdownV2 preformatted block

Wrap the entire ticker grid in `` ``` ``…`` ``` ``. The pre block is
a **single monospace context**, so any whitespace inside it stays
monospace — solves the proportional-spaces-between-spans problem
documented in §Context.

Visual cost: Telegram renders pre blocks with a faint gray
background. Reframed as a *feature* — the gray box visually anchors
the watchlist as a discrete data section, distinct from the
HTML-styled headers above it.

### 2. Pad cells to `max(len(t) for t in watchlist) + 2`

Compute the longest ticker in the watchlist, set every cell to that
width (plus 2-char gutter). Means a 33-ticker watchlist of mostly
4–5-char US tickers has compact 6–7-char cells; adding `RELIANCE.NS`
expands every cell to 13 chars.

```python
_GRID_GUTTER = 2
cell_width = max(len(t) for t in watchlist) + _GRID_GUTTER
```

### 3. Adaptive column count

Pick `ncols` so the rendered row fits a mobile-safe viewport target:

```python
_GRID_TARGET_WIDTH = 36   # mobile-safe target line width
_GRID_MAX_COLS = 4        # cap regardless of viewport

ncols = max(1, min(_GRID_MAX_COLS, _GRID_TARGET_WIDTH // cell_width))
```

| Watchlist composition | `cell_width` | `ncols` |
|---|---|---|
| 33 tickers, max 5 chars (`BRK-B`) | 7 | 4 (capped) |
| 33 tickers + `RELIANCE.NS` (11) | 13 | 2 |
| Hypothetical 30-char ticker | 32 | 1 |

Mobile viewport empirical estimates: iPhone SE ~30 chars, iPhone 14
Pro ~38, iPad portrait ~80. Targeting 36 keeps even iPhone SE safe
with 4 columns of short tickers (4 × 7 = 28 chars actual). Wider
viewports show narrower content than they could fit — acceptable.

### 4. Move 🔔 markers out of the grid into the header

The grid stays uniform (no per-cell markers). Enrolled tickers are
named explicitly in a header line above:

```
🔔 Digest — 08:00 PT
   → `COHR`, `FN`
```

Only shown when the enrolled set is a *proper subset* of the
watchlist. All-enrolled (legacy save) still says "all N fire daily"
in the header — no per-ticker enumeration needed.

## Output shape per state

```
📋 *Watchlist* — N tickers
🔔 *Digest* — HH:00 TZ                  [omitted if digest off]
   → `T1`, `T2`, ...                     [only when K of N enrolled, K > 0, K < N]

```
T1     T2     T3     T4
T5     T6     T7     T8
...
```

_footer_                                  [state-dependent]
```

| State | Header | Grid | Footer |
|---|---|---|---|
| Empty watchlist | `📋 Watchlist is empty` | — | `Use /add to start` |
| Digest off | `📋 Watchlist — N` | All N | `Daily digest off — /digest to enable` |
| Digest on, legacy (no `tickers`) | `📋 ... — N` + `🔔 Digest — HH:00 TZ · all N fire daily` | All N | — |
| Digest on, K of N enrolled (0 < K < N) | `📋 ... — N` + `🔔 Digest — HH:00 TZ` + `   → T1, T2, ...` | All N | — |
| Digest on, 0 enrolled | `📋 ... — N` + `🔔 Digest — HH:00 TZ` | All N | `Digest enabled but no tickers enrolled — /digest to fix` |

## Implementation surface

### Files modified

| File | Change |
|---|---|
| `src/tg_bot/handlers/commands.py` | `_format_list_view` rewrite. Signature unchanged. |
| `scripts/smoke_watchlist.py` | 4 new scenarios + update 3 existing grid scenarios. |

### Function signature (unchanged)

```python
def _format_list_view(
    watchlist: list[str],
    digest: dict | None,
    enrolled: set[str] | None,
) -> str:
```

### New constants

```python
_GRID_GUTTER = 2          # spaces between cells in the pre-block grid
_GRID_TARGET_WIDTH = 36   # mobile-safe target line width
_GRID_MAX_COLS = 4        # don't exceed 4 columns regardless of width
```

### Grid-builder helper

```python
def _format_ticker_grid(watchlist: list[str]) -> str:
    """Pre-block ticker grid with adaptive column count.

    Cell width = max(len(t)) + _GRID_GUTTER. Column count clamped to
    fit _GRID_TARGET_WIDTH on mobile viewports. The whole block is
    wrapped in MarkdownV2 triple-backticks so spaces inside render
    monospace.
    """
    cell_width = max(len(t) for t in watchlist) + _GRID_GUTTER
    ncols = max(1, min(_GRID_MAX_COLS, _GRID_TARGET_WIDTH // cell_width))
    rows = []
    for i in range(0, len(watchlist), ncols):
        row = "".join(f"{t:<{cell_width}}" for t in watchlist[i : i + ncols])
        rows.append(row.rstrip())
    return "```\n" + "\n".join(rows) + "\n```"
```

### Header builder

Splits the digest header into 2-3 lines based on enrollment shape.
Computes `enrolled_subset = enrolled is not None and enrolled != set(watchlist)`
to decide whether to render the `→ T1, T2, ...` line.

### MarkdownV2 escaping inside pre block

Per Telegram Bot API: characters inside `` ``` ``…`` ``` `` need only
`` ` `` and `\` escaped. Ticker characters are `[A-Z0-9.\-]` per
`TICKER_RE` — no backticks or backslashes possible. Safe.

## Testing

### Smoke scenarios to ADD (in `scripts/smoke_watchlist.py`)

1. **`test_list_grid_short_tickers_4_cols`** — Watchlist of US tickers
   max 5 chars → ncols = 4, every row ≤ 28 chars wide.
2. **`test_list_grid_long_indian_ticker_drops_to_2_cols`** — Add
   `RELIANCE.NS` to watchlist → ncols = 2, all cells padded to 13
   chars, every row exactly aligned.
3. **`test_list_grid_extreme_ticker_drops_to_1_col`** — Inject 14+
   char ticker → ncols = 1.
4. **`test_list_grid_renders_inside_pre_block`** — Output contains
   `\`\`\`\n...\n\`\`\``; no inline backticks on individual tickers
   within the grid.

### Smoke scenarios to UPDATE

1. **`test_list_format_no_digest`** — Assert grid is in pre block; no
   per-ticker backticks in the grid section.
2. **`test_list_format_digest_all_watchlist`** — Verify "all N fire
   daily" still present; no 🔔 markers anywhere in the grid block.
3. **`test_list_format_digest_with_filter`** — Verify enrolled
   tickers listed in header `→ T1, T2`; grid contains no 🔔.

### Manual phone test

After implementation: restart the bot and exercise:
1. `/list` with current 33-ticker watchlist → grid should render
   cleanly, no wobble.
2. `/add RELIANCE.NS` then `/list` → grid drops to 2 cols, all rows
   aligned, gray pre-block background visible.
3. `/del RELIANCE.NS` then `/list` → grid back to 4 cols.

## Risks

### Pre-block visual style
Telegram renders `` ``` `` blocks with a faint gray background and
slightly different vertical padding. Some users may find this
"code-like" feel jarring on a watchlist.

**Mitigation:** intentional — the visual demarcation is a feature.
If user feedback rejects it, fallback is to use `<pre>` in HTML mode
(also monospace, slightly different style). The internal algorithm
is unchanged either way.

### Adaptive ncols edge cases
- Single ticker: `cell_width = max(len) + 2`, `ncols = min(4, 36 / cell_width)`. Works.
- Single 30-char ticker: `cell_width = 32`, `ncols = 1`. Vertical-list rendering for that case. Acceptable.

### Tablet / landscape viewports
The 36-char target is conservative for mobile. Tablets/landscape
phones could fit 6+ columns of 5-char tickers but we cap at 4. Users
see narrower content than they could; nothing breaks.

## Decision log

| Alternative | Why rejected |
|---|---|
| **Comma-wrap (no grid)** | Reads as prose, not data. Watchlist scans alphabetically; the eye expects a grid. |
| **Vertical list (one per line)** | 33-line scroll on mobile is excessive for a "quick" command. Reconsider when per-ticker indicators (price, change) land. |
| **Telegraph page** | Indirection (extra tap + publish latency + IV cache staleness) violates the "quick" goal. Right surface for a future `/dashboard` feature. |
| **Truncate long tickers** | Ambiguous to users; Telegram doesn't support tap-to-expand cleanly. |
| **Keep inline backticks, reduce inter-cell gap** | Cosmetic — doesn't solve the proportional-spaces-between-spans root cause. |

## Done criteria

- [ ] `_format_list_view` rewritten with pre-block grid + max-width
      padding + adaptive ncols
- [ ] `_format_ticker_grid` helper extracted (pure function, easy to
      unit-test)
- [ ] Header marker line for enrolled subset
- [ ] 4 new smoke scenarios; 3 updated
- [ ] `ruff check + ruff format --check` clean
- [ ] Full smoke suite green (214 → 218 scenarios)
- [ ] Manual phone test with current 33-ticker watchlist + stress test
      with `RELIANCE.NS`
- [ ] CLAUDE.md `/list` Commands-section bullet updated to mention
      "pre-block grid, adaptive columns"
- [ ] CLAUDE.md "Recently fixed" entry replaced

## Scope summary

~30–50 LOC change in one function + one new helper, ~80 LOC of new
smoke scenarios. One PR. ~1.5 hours of work including review pass.
