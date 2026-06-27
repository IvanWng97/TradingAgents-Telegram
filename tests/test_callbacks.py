"""Pins the `button_callback` prefix-dispatch router in `handlers/callbacks.py`.

The if-elif prefix chain (`callbacks.py:button_callback`) is the single
routing surface for every inline-button tap, yet nothing exercised it
(`grep button_callback tests/` was 0). This suite pins:

  * each prefix routes to its intended sub-handler with the correctly
    parsed post-prefix payload (and to NO other handler), and
  * the TWO combined-prefix pairs — `cancel_analysis:` routes to the
    analysis-cancel handler (not `cancel:`'s) and `digest_cancel:` to the
    digest-cancel handler (not `digest:`'s). With the current
    colon-terminated prefixes these can't actually collide regardless of
    chain order (`"cancel_analysis:".startswith("cancel:")` is False), so
    the "checked before" ordering is defensive, not load-bearing today.
    What these guard is the FORMAT contract: a future change that drops the
    disambiguating colon (e.g. `startswith("cancel")`) or renames a prefix
    so one becomes a literal prefix of another would reintroduce the
    overlap — and then chain position would silently misroute the tap.

Strategy: every module-level handler name the router dispatches to is
monkeypatched with an async `Recorder`, so calling `button_callback`
records which handler fired and with what args — without running any
real handler, storage, or LLM path. The `assert_only` helper asserts
exactly-one-fired, which is what makes the disambiguation guard tight.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_bot.handlers import callbacks  # noqa: E402
from tg_bot.handlers.callbacks import button_callback  # noqa: E402


USER_ID = 99


# ─── minimal fakes (only what button_callback touches) ──────────────────
# button_callback reads: update.callback_query, query.answer(), query.data,
# update.effective_user.id. Nothing else.


class Recorder:
    """Async stand-in for a sub-handler that records the call + args."""

    def __init__(self) -> None:
        self.called = False
        self.args: tuple = ()
        self.kwargs: dict = {}

    async def __call__(self, *args, **kwargs) -> None:
        self.called = True
        self.args = args
        self.kwargs = kwargs


class FakeQuery:
    def __init__(self, data: str | None) -> None:
        self.data = data
        self.answered = False

    async def answer(self, *args, **kwargs) -> None:
        self.answered = True


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeUpdate:
    def __init__(self, data: str | None, user_id: int = USER_ID) -> None:
        self.callback_query = FakeQuery(data)
        self.effective_user = FakeUser(user_id)


# Every sub-handler the if-elif chain dispatches to, by module-level name.
# All resolve as globals inside `button_callback`, so patching the attribute
# on the `callbacks` module intercepts the dispatch.
HANDLER_NAMES = (
    "_handle_select_toggle",  # multi:
    "_handle_select_bulk",  # wsel:
    "_handle_page_nav",  # wpage:
    "_handle_done",  # runall:
    "_handle_digest_cancel",  # digest_cancel:
    "_handle_digest",  # digest:
    "_handle_del",  # del:
    "_handle_cancel_analysis",  # cancel_analysis:
    "_handle_cancel",  # cancel:
    "_handle_history_back",  # hist_back:
    "_handle_history_ticker",  # hist_t:
    "_handle_history",  # hist:
    "_handle_get_full_md",  # getmd:
)


def _patch_all(monkeypatch) -> dict[str, Recorder]:
    recorders: dict[str, Recorder] = {}
    for name in HANDLER_NAMES:
        rec = Recorder()
        recorders[name] = rec
        monkeypatch.setattr(callbacks, name, rec)
    return recorders


async def _dispatch(monkeypatch, data: str | None, user_id: int = USER_ID):
    recorders = _patch_all(monkeypatch)
    update = FakeUpdate(data, user_id)
    context = object()  # sentinel — handlers are patched, never used
    await button_callback(update, context)
    return recorders, update, context


def _assert_only(recorders: dict[str, Recorder], expected: str) -> None:
    fired = sorted(name for name, rec in recorders.items() if rec.called)
    assert fired == [expected], f"expected only {expected!r} to fire, got {fired}"


# ─── routing table ──────────────────────────────────────────────────────
# (callback_data, expected_handler, {arg_index: expected_value})
# arg indices follow each handler's real signature in callbacks.py:
#   _handle_select_toggle(query, context, user_id, ticker)   -> ticker @3
#   _handle_select_bulk(query, context, user_id, action)     -> action @3
#   _handle_page_nav(query, context, user_id, action)        -> action @3
#   _handle_done(query, context, user_id)                    -> user_id @2
#   _handle_digest_cancel(context, query, msg_id)            -> msg_id @2
#   _handle_digest(query, context, user_id, data)            -> full data @3
#   _handle_del(query, user_id, ticker)                      -> ticker @2
#   _handle_cancel_analysis(context, query, run_id)          -> run_id @2
#   _handle_cancel(context, query, user_id, what)            -> what @3
#   _handle_history_back(query, target)                      -> target @1
#   _handle_history_ticker(query, ticker)                    -> ticker @1
#   _handle_history(query, ticker, date_str)                 -> @1, @2
#   _handle_get_full_md(query, context, ticker, date_str)    -> @2, @3
CASES = [
    ("multi:NVDA", "_handle_select_toggle", {3: "NVDA"}),
    ("wsel:all", "_handle_select_bulk", {3: "all"}),
    ("wsel:clear", "_handle_select_bulk", {3: "clear"}),
    ("wpage:next", "_handle_page_nav", {3: "next"}),
    ("wpage:noop", "_handle_page_nav", {3: "noop"}),
    ("runall:go", "_handle_done", {2: USER_ID}),
    ("digest_cancel:4242", "_handle_digest_cancel", {2: "4242"}),
    ("digest:hour:9", "_handle_digest", {3: "digest:hour:9"}),
    ("digest:tickerpick", "_handle_digest", {3: "digest:tickerpick"}),
    ("del:AAPL", "_handle_del", {2: "AAPL"}),
    ("cancel_analysis:abc-123-uuid", "_handle_cancel_analysis", {2: "abc-123-uuid"}),
    ("cancel:watch", "_handle_cancel", {3: "watch"}),
    ("cancel:del", "_handle_cancel", {3: "del"}),
    ("hist_back:tickers", "_handle_history_back", {1: "tickers"}),
    ("hist_back:dates:NVDA", "_handle_history_back", {1: "dates:NVDA"}),
    ("hist_t:NVDA", "_handle_history_ticker", {1: "NVDA"}),
    ("hist:NVDA:2026-06-26", "_handle_history", {1: "NVDA", 2: "2026-06-26"}),
    ("getmd:NVDA:2026-06-26", "_handle_get_full_md", {2: "NVDA", 3: "2026-06-26"}),
]


@pytest.mark.parametrize("data, expected, arg_checks", CASES, ids=[c[0] for c in CASES])
async def test_button_callback_routes_each_prefix(
    monkeypatch, data, expected, arg_checks
):
    """Each prefix reaches its own sub-handler — and only that handler —
    with the post-prefix payload parsed into the right argument slot."""
    recorders, update, _ = await _dispatch(monkeypatch, data)
    _assert_only(recorders, expected)
    rec = recorders[expected]
    for idx, val in arg_checks.items():
        assert rec.args[idx] == val, (
            f"{expected} arg[{idx}] = {rec.args[idx]!r}, expected {val!r}"
        )
    # The router acks the tap immediately via query.answer() before dispatch.
    assert update.callback_query.answered is True


# ─── combined-prefix disambiguation guards ──────────────────────────────


async def test_cancel_analysis_wins_over_cancel(monkeypatch):
    """`cancel_analysis:<uuid>` routes to the analysis-cancel handler, NOT
    `_handle_cancel`. With the current colon-terminated prefixes the two
    can't collide regardless of chain order
    (`"cancel_analysis:run-7".startswith("cancel:")` is False), so the
    documented "checked before `cancel:`" ordering is defensive, not
    load-bearing today. This guards the FORMAT contract: a future change
    that drops the disambiguating colon (e.g. `startswith("cancel")`) or
    renames a prefix so one becomes a literal prefix of the other would
    reintroduce the overlap — and then chain position would silently
    misroute a Cancel-button tap into the picker-cancel flow."""
    recorders, _, _ = await _dispatch(monkeypatch, "cancel_analysis:run-7")
    assert recorders["_handle_cancel_analysis"].called is True
    assert recorders["_handle_cancel"].called is False
    assert recorders["_handle_cancel_analysis"].args[2] == "run-7"


async def test_digest_cancel_wins_over_digest(monkeypatch):
    """`digest_cancel:<msg_id>` routes to the digest-cancel handler, NOT
    `_handle_digest`. Same shape as the cancel pair: the colon-terminated
    prefixes can't collide today
    (`"digest_cancel:555".startswith("digest:")` is False), so the "checked
    before `digest:`" ordering is defensive. This guards against a future
    prefix-format change reintroducing the overlap and swallowing the abort
    tap into the digest picker dispatch."""
    recorders, _, _ = await _dispatch(monkeypatch, "digest_cancel:555")
    assert recorders["_handle_digest_cancel"].called is True
    assert recorders["_handle_digest"].called is False
    assert recorders["_handle_digest_cancel"].args[2] == "555"


# ─── chain edges ────────────────────────────────────────────────────────


async def test_unknown_prefix_routes_nowhere_but_still_acks(monkeypatch):
    """An unrecognized prefix falls through every branch — no sub-handler
    fires, but the leading `query.answer()` ack still runs."""
    recorders, update, _ = await _dispatch(monkeypatch, "bogus:payload")
    assert not any(rec.called for rec in recorders.values())
    assert update.callback_query.answered is True


async def test_empty_callback_data_routes_nowhere(monkeypatch):
    """`query.data` of None coerces to '' and matches no prefix."""
    recorders, update, _ = await _dispatch(monkeypatch, None)
    assert not any(rec.called for rec in recorders.values())
    assert update.callback_query.answered is True


@pytest.mark.parametrize("data", ["hist:NVDA", "getmd:NVDA"], ids=["hist", "getmd"])
async def test_malformed_two_part_payload_calls_no_handler(monkeypatch, data):
    """`hist:`/`getmd:` require a `<prefix>:<ticker>:<date>` triple. A
    two-part payload hits the `len(parts) == 3` guard and dispatches to
    no handler (logged + dropped) rather than crashing on a missing arg."""
    recorders, _, _ = await _dispatch(monkeypatch, data)
    assert not any(rec.called for rec in recorders.values())
