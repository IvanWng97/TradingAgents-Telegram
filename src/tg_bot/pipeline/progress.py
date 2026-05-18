"""Per-run progress reporting for TradingAgents analyses.

Wires a LangChain BaseCallbackHandler into the cached TradingAgentsGraph so
each langgraph node entry triggers a caption update on the user's Telegram
message. The callback singleton lives on the cached graph; per-run dispatch
targets are stored in a threadlocal so we don't lose the graph cache.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown


logger = logging.getLogger(__name__)


# Friendly-name + ordinal map for the canonical TradingAgents pipeline.
# The langgraph node identifier is matched case-insensitively against the
# keys; unknown nodes fall back to a raw name display.
#
# Pinned against tradingagents v0.2.5 `graph/setup.py:97-112` `add_node()`
# calls. Aliases below cover upstream rename churn we've observed across
# versions installed via `pip install git+...`:
#   - `sentiment analyst` — issue #557 renamed `create_social_analyst` →
#     `create_sentiment_analyst`. v0.2.5 keeps the back-compat node name
#     `"Social Analyst"`, but later HEAD builds emit `"Sentiment Analyst"`
#     directly (observed on the user's VPS). Both alias to step 2 so the
#     "(2/12)" badge survives the upstream switch.
# See `tests/test_progress.py::test_step_map_covers_all_upstream_v025_nodes`
# for the alignment pin — bump it AND this map when upgrading tradingagents.
_STEP_MAP: dict[str, tuple[str, int]] = {
    "market analyst": ("Market Analyst", 1),
    "social analyst": ("Social Analyst", 2),
    "sentiment analyst": ("Social Analyst", 2),  # alias: upstream issue #557
    "news analyst": ("News Analyst", 3),
    "fundamentals analyst": ("Fundamentals Analyst", 4),
    "bull researcher": ("Bull Researcher", 5),
    "bear researcher": ("Bear Researcher", 6),
    "research manager": ("Research Manager", 7),
    "trader": ("Trader", 8),
    "aggressive analyst": ("Aggressive Risk Analyst", 9),
    "conservative analyst": ("Conservative Risk Analyst", 10),
    "neutral analyst": ("Neutral Risk Analyst", 11),
    "portfolio manager": ("Portfolio Manager", 12),
}
# Derived from `_STEP_MAP` so adding/removing entries auto-updates the
# "(N/M)" badge. Counts unique ordinals (some node names alias to the same
# step — e.g. `sentiment analyst` and `social analyst` both map to step 2).
TOTAL_STEPS = max(ordinal for _name, ordinal in _STEP_MAP.values())


# Per-thread reporter so the singleton callback on a cached graph can dispatch
# to the right run. Set by run_trading_analysis around its propagate() call.
_current_reporter = threading.local()


def resolve_step(raw_name: str) -> tuple[str, int | None]:
    """Map a langgraph node name to (friendly_name, ordinal). Falls back to
    a Title-Cased version of the raw name with no ordinal when unknown."""
    key = raw_name.replace("_", " ").lower().strip()
    if key in _STEP_MAP:
        return _STEP_MAP[key]
    # Best-effort fallback: drop common "tools" suffix/prefix and re-look up
    # before falling through to a title-case display. Lets a node like
    # "news_analyst tools" still resolve to its ordinal.
    cleaned_lower = key.replace("tools ", "").replace(" tools", "").strip()
    if cleaned_lower in _STEP_MAP:
        return _STEP_MAP[cleaned_lower]
    # WARN on fallback: a node name not in `_STEP_MAP` means either upstream
    # tradingagents renamed something or a new node was added. Either way
    # the ordinal badge `(n/12)` disappears and the user-visible step name
    # may look off ("Sentiment Analyst" instead of "Social Analyst").
    logger.warning(
        "resolve_step: unknown node %r (cleaned=%r) — falling back to title-case; "
        "expected one of %s",
        raw_name,
        cleaned_lower,
        sorted(_STEP_MAP.keys()),
    )
    return cleaned_lower.title() or raw_name, None


class CancelledByUserError(RuntimeError):
    """Raised from the progress callback when the user taps Cancel mid-run.

    Bubbles up through tradingagents/langgraph back into `asyncio.to_thread`
    so the analysis handler can render a "Cancelled" caption instead of a
    result. Cancellation is checked at LLM-call boundaries — the in-flight
    LLM call still completes; downstream steps are skipped.
    """


class ProgressReporter:
    """Edits a Telegram photo caption to reflect the current pipeline step.

    All state is per-run: bound to a single chat_id/message_id and to the
    asyncio loop that owns the bot connection. Reporter instances are NOT
    shared across runs — even for the same user.
    """

    def __init__(
        self,
        bot: Any,
        chat_id: int,
        message_id: int,
        ticker: str,
        loop: asyncio.AbstractEventLoop,
        cancel_event: threading.Event | None = None,
        cancel_run_id: str | None = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.ticker = ticker
        self.loop = loop
        self.cancel_event = cancel_event
        # Stable per-run id used in the Cancel button's callback_data.
        # Re-attached on every caption edit since editMessageCaption drops
        # reply_markup unless re-sent.
        self.cancel_run_id = cancel_run_id
        self._last_step: str | None = None

    async def report(self, raw_node_name: str) -> None:
        """Coalesce duplicate step names and edit the caption. Swallows
        BadRequest / network errors — progress is best-effort."""
        if raw_node_name == self._last_step:
            return
        self._last_step = raw_node_name

        friendly, ordinal = resolve_step(raw_node_name)
        ticker_v2 = escape_markdown(self.ticker, version=2)
        friendly_v2 = escape_markdown(friendly, version=2)
        if ordinal is not None:
            caption = (
                f"📊 Analyzing *{ticker_v2}* — running {friendly_v2} "
                f"\\({ordinal}/{TOTAL_STEPS}\\)…"
            )
        else:
            caption = f"📊 Analyzing *{ticker_v2}* — running {friendly_v2}…"

        # Telegram's editMessageCaption drops the existing reply_markup unless
        # we re-send it — so re-attach the cancel button on every step update.
        reply_markup = None
        if self.cancel_event is not None and self.cancel_run_id is not None:
            reply_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "❌ Cancel",
                            callback_data=f"cancel_analysis:{self.cancel_run_id}",
                        )
                    ]
                ]
            )

        try:
            await self.bot.edit_message_caption(
                chat_id=self.chat_id,
                message_id=self.message_id,
                caption=caption,
                parse_mode="MarkdownV2",
                reply_markup=reply_markup,
            )
            logger.info(
                "report: caption updated ticker=%s node=%s friendly=%s ord=%s/%s",
                self.ticker,
                raw_node_name,
                friendly,
                ordinal if ordinal is not None else "?",
                TOTAL_STEPS,
            )
        except Exception as e:
            # Final result may have already replaced the caption, or Telegram
            # may have rate-limited — neither should crash the analysis. WARN
            # (not debug) so a wedged progress surface is visible without
            # raising the bot's log verbosity globally.
            logger.warning(
                "report: caption edit FAILED ticker=%s node=%s (%s: %s)",
                self.ticker,
                raw_node_name,
                type(e).__name__,
                e,
            )


class _DelegatingProgressCallback(BaseCallbackHandler):
    """LangChain callback singleton attached to a cached TradingAgentsGraph.

    TradingAgents passes our callbacks into the LLM constructor kwargs, so we
    receive LLM-level events (`on_chat_model_start` / `on_llm_start`) — NOT
    `on_chain_start`. LangGraph propagates the surrounding node name as
    `metadata["langgraph_node"]` on every LLM call inside a node, which is
    what we use to identify the current pipeline step.

    The callback reads the per-run reporter from threadlocal storage and
    bridges back onto the asyncio loop that owns the bot. If no reporter is
    set for the current thread, the event is silently dropped.

    raise_error=True is load-bearing: LangChain's callback manager swallows
    exceptions from handler methods by default, which would defeat our
    cancellation strategy of raising CancelledByUserError out of `_dispatch`
    to abort the in-flight LLM call. With raise_error=True the exception
    propagates into the LLM invocation chain and bubbles up through
    langgraph back to our `to_thread` await.
    """

    raise_error = True

    def on_chat_model_start(
        self,
        serialized: dict | None,
        messages: Any,
        **kwargs: Any,
    ) -> None:
        self._dispatch(kwargs)

    def on_llm_start(
        self,
        serialized: dict | None,
        prompts: Any,
        **kwargs: Any,
    ) -> None:
        self._dispatch(kwargs)

    def _dispatch(self, kwargs: dict) -> None:
        reporter: ProgressReporter | None = getattr(_current_reporter, "value", None)
        if reporter is None:
            # WARN (not debug) — a missing reporter means our threadlocal
            # binding lost the run (sub-thread spawned by a langgraph node
            # would do this). Surfaces silently otherwise.
            logger.warning(
                "dispatch: no reporter on this thread (thread=%s) — event dropped; "
                "kwargs keys=%s",
                threading.current_thread().name,
                sorted(kwargs.keys()),
            )
            return
        # Check the cancel flag BEFORE dispatching the next step's UI update —
        # this is our only hook into the running pipeline. Raising here aborts
        # the about-to-start LLM call and bubbles up through langgraph.
        if reporter.cancel_event is not None and reporter.cancel_event.is_set():
            logger.info(
                "dispatch: cancel flag is set for ticker=%s message_id=%s — raising",
                reporter.ticker,
                reporter.message_id,
            )
            raise CancelledByUserError("Analysis cancelled by user")
        metadata = kwargs.get("metadata") or {}
        node_name = metadata.get("langgraph_node")
        if not node_name:
            # Not an LLM call inside a langgraph node — ignore noise from
            # any LLM client warm-ups or auxiliary calls. INFO (not silent)
            # so we can confirm callbacks are firing at all and see what
            # langgraph/langchain version is sending us — if every event
            # lands here, the metadata shape changed upstream.
            logger.info(
                "dispatch: event without langgraph_node ticker=%s "
                "metadata_keys=%s kwargs_keys=%s",
                reporter.ticker,
                sorted(metadata.keys()),
                sorted(kwargs.keys()),
            )
            return
        logger.info(
            "dispatch: progress event ticker=%s node=%s", reporter.ticker, node_name
        )
        # Build the coroutine separately so we can `.close()` it cleanly if
        # the loop is gone — otherwise gc surfaces a `coroutine was never
        # awaited` RuntimeWarning on shutdown for every leaked dispatch.
        coro = reporter.report(str(node_name))
        try:
            asyncio.run_coroutine_threadsafe(coro, reporter.loop)
        except RuntimeError as e:
            # Loop closed (analysis outlived the chat) — drop the event.
            coro.close()
            logger.warning(
                "dispatch: run_coroutine_threadsafe failed ticker=%s node=%s (%s)",
                reporter.ticker,
                node_name,
                e,
            )


# Single instance reused across all cached graphs — the per-run target lives
# in the threadlocal, so this callback is safe to share.
delegating_progress_callback = _DelegatingProgressCallback()


def set_reporter(reporter: ProgressReporter | None) -> None:
    """Bind a reporter to the current thread (or clear it with None)."""
    _current_reporter.value = reporter
