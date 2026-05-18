"""Per-user digest schedule storage.

Previously this module also held per-user LLM provider / model / rounds /
effort selections (driven by the `/config` Telegram command). That was
removed in the env-config refactor — LLM settings now flow from `.env`
via `TRADINGAGENTS_*` env vars (process-wide, single source of truth).
Existing `user_config.json` files may still carry the legacy LLM fields;
they're inert (no method reads them) and harmless. Each save rewrites
the whole file so they survive indefinitely — operators can delete the
JSON if they want a clean file."""

import logging
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ._base import JsonStorage


logger = logging.getLogger(__name__)


class UserConfigStorage(JsonStorage):
    """Per-user daily-digest schedule, keyed by user_id."""

    DIGEST_KEY = "digest"

    # ─── digest schedule ────────────────────────────────────────────────

    @staticmethod
    def _empty_digest() -> dict[str, Any]:
        # `tickers` is the explicit-opt-in filter list. New users start with
        # an empty list so the fan-out won't run anything until they pick;
        # existing users (saves predating this field) get their `tickers` key
        # backfilled at _post_init from the current watchlist for back-compat.
        return {
            "enabled": False,
            "hour_local": None,
            "tz": None,
            "chat_id": None,
            "tickers": [],
        }

    def get_digest(self, user_id: str) -> dict[str, Any] | None:
        """Return the digest block (or None if never set). Read-only — copy
        before mutating."""
        return self._data.get(str(user_id), {}).get(self.DIGEST_KEY)

    def get_enrolled_tickers(self, user_id: str, watchlist: list[str]) -> list[str]:
        """Effective digest enrolment intersected with the live watchlist.
        Single source of truth for runtime "which tickers fire if we run
        the fan-out right now" — used by both the daily fan-out
        (`run_user_digest`) and the `/list` UX view, which previously
        inlined this logic with subtle drift risk across legacy-fallback
        handling and case-normalization.

        Contract:
          - Digest block absent → `[]`
          - Legacy save (`tickers` key absent entirely) → full watchlist.
            This is the documented backward-compat rule in CLAUDE.md's
            "Digest schedule" key contract — `_post_init` backfills these
            on startup, so this branch is only hit on the first run after
            a legacy save migrates.
          - Otherwise: `tickers ∩ watchlist`, preserving watchlist order
            (sorted on-disk per `set_digest_tickers`) so the fan-out
            iterates a deterministic order.

        Deliberately does NOT gate on `enabled`. The "▶ Run now" path
        fires `run_user_digest` for an explicitly disabled digest (a
        user tapping run-now from the open picker), and that path must
        still respect the configured filter — gating on `enabled` here
        would suppress run-now's filter. Callers that need an
        "is digest scheduled?" check (e.g. `/list`'s decision to render
        the digest header section at all) should call `get_digest` and
        check `enabled` themselves.

        Note: picker-internal sites in `digest.py` deliberately do NOT use
        this method — they show the user's *currently saved* set during
        editing, where a fallback would mislead ("0/12 tickers" is the
        right pre-pick state, not "all enrolled"). This method is for
        runtime evaluation only.
        """
        digest = self.get_digest(user_id)
        if not digest:
            return []
        raw = digest.get("tickers")
        if raw is None:
            # Legacy save predating the filter feature → all watchlist.
            return list(watchlist)
        # Stored tickers are already uppercase (set_digest_tickers
        # canonicalizes), but be defensive — a hand-edited json or a
        # forward-compat shape with mixed case shouldn't silently miss.
        filter_set = {t.upper() for t in raw}
        return [t for t in watchlist if t in filter_set]

    async def set_digest_hour(self, user_id: str, hour: int, chat_id: int) -> bool:
        """Set hour (0-23), enable the digest, and capture chat_id.

        The job callback later fires unsolicited messages, so it needs a
        chat_id to send to — we can only learn that from a live Update.
        Hour selection is the natural moment to capture it.
        """
        if not (0 <= hour <= 23):
            return False
        user_id = str(user_id)
        bucket = self._data.setdefault(user_id, {})
        digest = bucket.setdefault(self.DIGEST_KEY, self._empty_digest())
        digest["enabled"] = True
        digest["hour_local"] = int(hour)
        digest["chat_id"] = int(chat_id)
        await self._save_async()
        return True

    async def set_digest_tz(self, user_id: str, tz: str) -> bool:
        """Set the IANA tz name for the user's digest schedule.

        Validated against `zoneinfo` so an invalid/typo'd tz never reaches
        the JobQueue. Doesn't touch `enabled` — a tz change on an active
        digest keeps it active; on a first-time setup the digest stays
        disabled until the user picks an hour.
        """
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as e:
            logger.warning("set_digest_tz: invalid tz %r (%s)", tz, e)
            return False
        user_id = str(user_id)
        bucket = self._data.setdefault(user_id, {})
        digest = bucket.setdefault(self.DIGEST_KEY, self._empty_digest())
        digest["tz"] = tz
        await self._save_async()
        return True

    async def set_digest_tickers(self, user_id: str, tickers: list[str]) -> bool:
        """Replace the digest-filter ticker list. De-duplicated + sorted so
        on-disk order is canonical (smaller diffs, predictable picker order).

        Doesn't touch `enabled`/`hour`/`tz` — the filter is independent of
        scheduling. An empty list is allowed and means "no tickers selected"
        (fan-out emits a reminder rather than running the watchlist)."""
        # Hard reject non-list inputs — passing a bare string would iterate
        # characters and silently store ["A", "D", "N", "V"] for "NVDA".
        if not isinstance(tickers, (list, tuple, set, frozenset)):
            raise TypeError(
                f"tickers must be a list/tuple/set, got {type(tickers).__name__}"
            )
        canonical = sorted(set(t.strip().upper() for t in tickers if t.strip()))
        user_id = str(user_id)
        bucket = self._data.setdefault(user_id, {})
        digest = bucket.setdefault(self.DIGEST_KEY, self._empty_digest())
        digest["tickers"] = canonical
        await self._save_async()
        return True

    async def disable_digest(self, user_id: str) -> bool:
        """Disable the digest; preserves hour + tz for one-tap re-enable."""
        digest = self._data.get(str(user_id), {}).get(self.DIGEST_KEY)
        if digest is None:
            return False
        digest["enabled"] = False
        await self._save_async()
        return True

    def iter_users_with_digest(self) -> list[str]:
        """Every user_id that has any digest block (enabled or not).

        Used by `_post_init` to backfill the `tickers` field on legacy saves
        — covers BOTH currently-enabled digests and disabled-but-preserved
        ones, so a user who flips the switch back on doesn't lose their
        original watchlist scope.
        """
        out: list[str] = []
        for user_id, bucket in self._data.items():
            if not isinstance(bucket, dict):
                continue
            digest = bucket.get(self.DIGEST_KEY)
            if isinstance(digest, dict):
                out.append(user_id)
        return out

    def iter_enabled_digests(self) -> list[tuple[str, dict[str, Any]]]:
        """List of (user_id, digest_block) for users with a fully-configured,
        enabled digest. Used by `_post_init` to re-register JobQueue jobs.

        Skips entries missing any of: enabled flag, hour_local, tz, chat_id —
        these are partial states from an in-progress picker session.

        Defensive: a corrupted JSON file or hand-edit could leave the
        bucket or its digest block as something other than a dict. Skip
        and log rather than crashing the whole _post_init walk for one
        bad row.
        """
        result: list[tuple[str, dict[str, Any]]] = []
        for user_id, bucket in self._data.items():
            if not isinstance(bucket, dict):
                logger.warning(
                    "iter_enabled_digests: skipping user %s — bucket is %s",
                    user_id,
                    type(bucket).__name__,
                )
                continue
            digest = bucket.get(self.DIGEST_KEY)
            if not isinstance(digest, dict):
                # None / never-set → silently skip; non-dict → log loud.
                if digest is not None:
                    logger.warning(
                        "iter_enabled_digests: skipping user %s — digest is %s",
                        user_id,
                        type(digest).__name__,
                    )
                continue
            if not digest.get("enabled"):
                continue
            if (
                digest.get("hour_local") is None
                or not digest.get("tz")
                or not digest.get("chat_id")
            ):
                continue
            result.append((user_id, digest))
        return result
