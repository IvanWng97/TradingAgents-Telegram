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
from typing import Any, Optional

from langchain_core.callbacks import BaseCallbackHandler
from telegram.helpers import escape_markdown


logger = logging.getLogger(__name__)


# Friendly-name + ordinal map for the canonical TradingAgents pipeline.
# The langgraph node identifier is matched case-insensitively against the
# keys; unknown nodes fall back to a raw name display.
_STEP_MAP: dict[str, tuple[str, int]] = {
    "market analyst": ("Market Analyst", 1),
    "social analyst": ("Social Analyst", 2),
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
    "risk judge": ("Portfolio Manager", 12),
}
_TOTAL_STEPS = 12


# Per-thread reporter so the singleton callback on a cached graph can dispatch
# to the right run. Set by run_trading_analysis around its propagate() call.
_current_reporter = threading.local()


def _resolve_step(raw_name: str) -> tuple[str, Optional[int]]:
    """Map a langgraph node name to (friendly_name, ordinal). Falls back to
    a Title-Cased version of the raw name with no ordinal when unknown."""
    key = raw_name.replace("_", " ").lower().strip()
    if key in _STEP_MAP:
        return _STEP_MAP[key]
    # Best-effort fallback: drop common prefixes, title-case the rest.
    cleaned = key.replace("tools ", "").strip().title()
    return cleaned or raw_name, None


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
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.ticker = ticker
        self.loop = loop
        self._last_step: Optional[str] = None

    async def report(self, raw_node_name: str) -> None:
        """Coalesce duplicate step names and edit the caption. Swallows
        BadRequest / network errors — progress is best-effort."""
        if raw_node_name == self._last_step:
            return
        self._last_step = raw_node_name

        friendly, ordinal = _resolve_step(raw_node_name)
        ticker_v2 = escape_markdown(self.ticker, version=2)
        friendly_v2 = escape_markdown(friendly, version=2)
        if ordinal is not None:
            caption = (
                f"📊 Analyzing *{ticker_v2}* — running {friendly_v2} "
                f"\\({ordinal}/{_TOTAL_STEPS}\\)…"
            )
        else:
            caption = f"📊 Analyzing *{ticker_v2}* — running {friendly_v2}…"

        try:
            await self.bot.edit_message_caption(
                chat_id=self.chat_id,
                message_id=self.message_id,
                caption=caption,
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            # Final result may have already replaced the caption, or Telegram
            # may have rate-limited — neither should crash the analysis.
            logger.debug("Progress edit skipped for %r: %s", raw_node_name, e)


class _DelegatingProgressCallback(BaseCallbackHandler):
    """LangChain callback singleton attached to a cached TradingAgentsGraph.

    On each node entry it reads the per-run reporter from threadlocal storage
    and bridges the call back onto the asyncio loop that owns the bot. If no
    reporter is set for the current thread (e.g. a non-bot caller), the event
    is silently dropped.
    """

    def on_chain_start(
        self,
        serialized: Optional[dict],
        inputs: Any,
        *,
        run_id: Any = None,
        parent_run_id: Any = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        reporter: Optional[ProgressReporter] = getattr(_current_reporter, "value", None)
        if reporter is None:
            return

        node_name = self._extract_node_name(serialized, metadata, kwargs)
        if not node_name:
            return

        try:
            asyncio.run_coroutine_threadsafe(reporter.report(node_name), reporter.loop)
        except RuntimeError:
            # Loop closed (analysis outlived the chat) — drop the event.
            pass

    @staticmethod
    def _extract_node_name(
        serialized: Optional[dict],
        metadata: Optional[dict],
        kwargs: dict,
    ) -> Optional[str]:
        if metadata and "langgraph_node" in metadata:
            return str(metadata["langgraph_node"])
        # Fallbacks for older langgraph or non-graph chains.
        name = kwargs.get("name") or (serialized or {}).get("name")
        return str(name) if name else None


# Single instance reused across all cached graphs — the per-run target lives
# in the threadlocal, so this callback is safe to share.
delegating_progress_callback = _DelegatingProgressCallback()


def set_reporter(reporter: Optional[ProgressReporter]) -> None:
    """Bind a reporter to the current thread (or clear it with None)."""
    _current_reporter.value = reporter
