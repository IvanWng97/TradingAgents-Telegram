# TODO

Roadmap items not yet started. Tracked here while small; promote to GitHub Issues once an item picks up enough scope to need discussion or assignment.

## Same-day result cache + `/refresh`

`_run_analysis_for_ticker` re-runs the full graph even if the same `(ticker, date, provider, deep, quick)` was analyzed minutes ago. Reusing tradingagents' on-disk `full_states_log_<date>.json` would make the second tap free + instant; `/refresh NVDA` opts back into a fresh run.
