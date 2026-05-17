"""Shared fixtures for the smoke suites that exercise the analysis runner.

Used by `smoke_runner.py` (cancel + queueing scenarios) and
`smoke_runner_parallel.py` (wall-clock proof of true parallelism).
Not used by the unit-flavoured suites (cache, config_key, formatters,
etc.) — those don't need a fake PTB context.

Keep this module narrow: only put something here if at least two suites
share the exact same shape. The temptation to extract every Fake* class
should be resisted — the small duplication is worth the per-suite
readability when each file diverges on what it actually tests.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections import defaultdict
from types import SimpleNamespace


def set_smoke_data_dir(prefix: str) -> str:
    """Repoint `TG_BOT_DATA_DIR` to a fresh tempdir.

    Every smoke suite needs cache + storage isolation from the developer's
    real `data/` directory and from other suites running back-to-back.
    The cache module reads the env var on every call, so swapping it here
    propagates without re-import.
    """
    d = tempfile.mkdtemp(prefix=prefix)
    os.environ["TG_BOT_DATA_DIR"] = d
    return d


class FakeTimedOut(Exception):
    """Stand-in for `telegram.error.TimedOut` so `_run_analysis_for_ticker`'s
    retry path fires by exception-class name (`type(e).__name__ == "TimedOut"`)
    without pulling in PTB's error hierarchy."""


# Make the type name "TimedOut" so the retry's `type(e).__name__` check fires.
FakeTimedOut.__name__ = "TimedOut"


class FakeBot:
    """Records every call. Optionally fails send_photo on the first N attempts.

    Methods are a superset of what either suite uses today — kitchen-sink
    so adding a new bot.X surface in `_run_analysis_for_ticker` doesn't
    require parallel edits across two test files.
    """

    def __init__(self, send_photo_failures: int = 0) -> None:
        self.captions: dict[int, str] = {}
        # Ordered (caption, parse_mode) of every send_photo / edit_message_caption.
        # The parse_mode contract scenario (Invariant #4) reads this to verify
        # transient captions use MarkdownV2 and final captions use HTML.
        self.caption_history: list[tuple[str, str | None]] = []
        # Ordered (caption, reply_markup) of every send_photo / edit_message_caption.
        # The cancel-keyboard reattach scenario (Invariant #3) reads this to
        # verify every in-flight ProgressReporter edit re-sends the ❌ Cancel
        # button (Telegram drops reply_markup unless re-sent).
        self.edit_markup_history: list[tuple[str, object | None]] = []
        # Documents posted via `query.message.chat.send_document` (see
        # `_handle_get_full_md`). Each entry is the kwargs dict so tests can
        # introspect both the filename and the reply_to anchor.
        self.documents: list[dict] = []
        # Plain `bot.send_message` calls — recorded so the .md fallback path
        # ("Markdown report unavailable") can assert what was sent.
        self.messages: list[dict] = []
        self._next_id = 1000
        self._lock = asyncio.Lock()
        self._send_photo_attempts: dict[int, int] = defaultdict(int)
        self._send_photo_failures = send_photo_failures

    async def send_photo(self, chat_id, photo, caption, parse_mode, reply_markup):
        # Identify which logical "send" this is via the caption (contains ticker).
        key = hash(caption)
        self._send_photo_attempts[key] += 1
        if self._send_photo_attempts[key] <= self._send_photo_failures:
            raise FakeTimedOut(
                f"fake timeout (attempt {self._send_photo_attempts[key]})"
            )
        async with self._lock:
            mid = self._next_id
            self._next_id += 1
        self.captions[mid] = caption
        self.caption_history.append((caption, parse_mode))
        self.edit_markup_history.append((caption, reply_markup))
        # Stash a chat handle on the returned Message-like so callers exercising
        # `query.message.chat.send_document(...)` route through this FakeBot.
        chat = SimpleNamespace(send_document=self._send_document)
        return SimpleNamespace(message_id=mid, chat=chat, chat_id=chat_id)

    async def edit_message_caption(
        self, chat_id, message_id, caption, parse_mode=None, reply_markup=None
    ):
        self.captions[message_id] = caption
        self.caption_history.append((caption, parse_mode))
        self.edit_markup_history.append((caption, reply_markup))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        pass

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None):
        pass

    async def send_message(self, chat_id, text=None, parse_mode=None, **kw):
        self.messages.append({"chat_id": chat_id, "text": text, **kw})

    async def _send_document(self, document=None, **kw):
        """Backing impl for the `chat.send_document` handle returned by
        send_photo. The getmd happy path uses this surface to deliver the
        `.md` archival report as a reply to the photo."""
        self.documents.append({"document": document, **kw})


class FakeContext:
    """Minimal stand-in for PTB's `ContextTypes.DEFAULT_TYPE`. Carries the
    chat/bot-data dicts that `_run_analysis_for_ticker` reads/writes."""

    def __init__(self, bot: FakeBot) -> None:
        self.bot = bot
        self.chat_data: dict = {}
        self.bot_data: dict = {}
