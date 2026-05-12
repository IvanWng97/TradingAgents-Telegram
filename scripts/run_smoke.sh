#!/usr/bin/env bash
# Run every smoke suite and report a single pass/fail summary.
#
# Usage:  bash scripts/run_smoke.sh
# Exit code: 0 if every suite passed, 1 if any failed.
#
# The suites are intentionally NOT run in parallel — each one repoints
# TG_BOT_DATA_DIR and patches module-level state, so parallel runs would
# cross-contaminate. Sequential is fast enough (~30s total for all 10).

set -uo pipefail

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-.venv/bin/python3}"
SUITES=(
  cache
  config_key
  user_config
  formatters
  watchlist
  validation
  digest
  runner
  runner_parallel
  telegraph
)

# Color helpers — degrade to plain text when stdout isn't a TTY (CI logs).
if [ -t 1 ]; then
  GREEN=$'\033[32m'; RED=$'\033[31m'; RESET=$'\033[0m'
else
  GREEN=""; RED=""; RESET=""
fi

failed=()
for s in "${SUITES[@]}"; do
  printf "%-20s " "smoke_$s"
  out=$("$PYTHON" "scripts/smoke_$s.py" 2>&1)
  if [ $? -eq 0 ]; then
    # Suites end with either "all N passed" or "ALL CHECKS PASSED" — print
    # the last line so the user sees the scenario count.
    last=$(printf "%s" "$out" | tail -1)
    printf "%s%s%s\n" "$GREEN" "$last" "$RESET"
  else
    failed+=("$s")
    last=$(printf "%s" "$out" | tail -1)
    printf "%s%s%s\n" "$RED" "$last" "$RESET"
  fi
done

echo
if [ ${#failed[@]} -eq 0 ]; then
  printf "%sAll suites passed.%s\n" "$GREEN" "$RESET"
  exit 0
else
  printf "%s%d suite(s) failed: %s%s\n" "$RED" "${#failed[@]}" "${failed[*]}" "$RESET"
  exit 1
fi
