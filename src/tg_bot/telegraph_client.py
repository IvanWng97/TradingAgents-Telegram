"""Telegraph publishing: HTML sanitization + page creation."""

import asyncio
import logging
import os
from typing import Optional

from bs4 import BeautifulSoup
from telegraph import Telegraph


logger = logging.getLogger(__name__)


# Telegraph supports a small subset of HTML tags. Map unsupported header
# levels into supported ones so markdown→HTML conversion still renders.
_TAG_MAP = {
    "h1": "h3",
    "h2": "h3",
    "h3": "h4",
    "h4": "h4",
    "h5": "h4",
    "h6": "h4",
}


def _telegraph_client() -> Telegraph:
    """Lazy init so importing this module doesn't require the env var."""
    return Telegraph(access_token=os.getenv("TELEGRAPH_ACCESS_TOKEN"))


def _convert_tables_to_lists(soup: BeautifulSoup) -> None:
    """Telegraph silently strips <table> tags from submitted HTML, leaving
    cell text mashed into paragraphs. Convert each table into a <ul> where
    every body row becomes a list item with the first cell as a <strong>
    label and the rest joined by ' — '. Better than ASCII <pre> tables for
    long-cell content (agent-generated rationale prose), which would
    otherwise overflow horizontally on mobile.
    """
    for table in soup.find_all("table"):
        # Pull body rows (skip <thead> — it's column labels, not content).
        body_rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            # Skip rows that are entirely <th> — header row.
            if tr.find("th") and not tr.find("td"):
                continue
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            cells = [c for c in cells if c]
            if cells:
                body_rows.append(cells)

        if not body_rows:
            table.decompose()
            continue

        ul = soup.new_tag("ul")
        for row in body_rows:
            li = soup.new_tag("li")
            strong = soup.new_tag("strong")
            strong.string = row[0]
            li.append(strong)
            rest = " — ".join(row[1:])
            if rest:
                li.append(f": {rest}")
            ul.append(li)
        table.replace_with(ul)


def sanitize_html_for_telegraph(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(list(_TAG_MAP.keys())):
        element.name = _TAG_MAP[element.name]
    _convert_tables_to_lists(soup)
    return str(soup)


# Exception classes that suggest transient flakiness against Telegraph's
# API — worth one retry with backoff. API-level errors (TelegraphException
# for content/auth issues) raise differently and shouldn't be retried.
_TRANSIENT_NET_ERRORS: tuple[type[Exception], ...] = ()
try:
    from requests.exceptions import (
        ConnectionError as _RequestsConnectionError,
        Timeout as _RequestsTimeout,
    )

    _TRANSIENT_NET_ERRORS = (_RequestsConnectionError, _RequestsTimeout)
except ImportError:  # pragma: no cover — requests is a transitive dep
    pass


def _path_from_telegraph_url(url: str) -> Optional[str]:
    """Extract the Telegraph page path from a `https://telegra.ph/<path>`
    URL. Returns None for anything that doesn't look like a Telegraph URL
    so callers can fall back to `create_page` instead of feeding garbage
    into `edit_page`."""
    if not url or "telegra.ph/" not in url:
        return None
    return url.rsplit("/", 1)[-1] or None


async def _call_with_transient_retry(call, *args, **kwargs):
    """Run a blocking Telegraph SDK call in a thread with one retry on
    transient network errors (`ConnectionError` / `Timeout`). API-level
    errors (`TelegraphException` — auth, content rejection, page not
    found) propagate immediately because a re-attempt would fail
    identically.

    Reused by both `edit_page` (URL-stability path) and `create_page`
    (fresh-publish path) so a transient blip on either doesn't degrade
    behavior asymmetrically. Earlier revisions retried only on
    `create_page` — a `ConnectionError` during `edit_page` would log
    a warning, fall through to `create_page`, and silently leak a new
    Telegraph URL — exactly the regression the edit-in-place plumbing
    was added to prevent."""
    for attempt in range(2):
        try:
            return await asyncio.to_thread(call, *args, **kwargs)
        except _TRANSIENT_NET_ERRORS as e:
            if attempt == 0:
                logger.warning(
                    "Telegraph network error (attempt 1, will retry in 1s): %s: %s",
                    type(e).__name__,
                    e,
                )
                await asyncio.sleep(1.0)
                continue
            raise


async def publish_to_telegraph(
    title: str, content: str, edit_path: Optional[str] = None
) -> Optional[str]:
    """Publish HTML content to Telegraph; return page URL or None on failure.

    The Telegraph SDK uses synchronous HTTP under the hood, so we offload
    the API call to a worker thread to keep PTB's event loop responsive.

    `edit_path` enables in-place updates so `/refresh` keeps the same
    shareable URL while replacing the content — without it, every
    refresh creates a new page (Telegraph appends `-2`/`-3` suffixes on
    title collision) and old shared links go stale. When `edit_path` is
    provided we try `edit_page` first. A *transient* failure on edit
    (`ConnectionError`/`Timeout`) gets one retry; if it still fails, we
    return None (don't fall through to `create_page` — that would leak a
    new URL and defeat the whole edit-in-place fix). A *non-transient*
    failure on edit (page deleted, auth drift, oversized content) falls
    through to `create_page` so a broken edit-target doesn't block the
    publish entirely. The caller's cache-hygiene gate ensures a None
    return doesn't poison the cache.

    Resilience:
    - One retry with 1s backoff on transient network errors
      (ConnectionError / Timeout) — applied uniformly to both edit and
      create paths via `_call_with_transient_retry`.
    - Failure log records input/cleaned HTML sizes + exception class so
      future failures can be triaged without re-instrumenting.
    """
    cleaned_html = sanitize_html_for_telegraph(content)
    client = _telegraph_client()

    if edit_path:
        try:
            await _call_with_transient_retry(
                client.edit_page,
                path=edit_path,
                title=title,
                html_content=cleaned_html,
                author_name="TradingAgents Bot",
            )
            return f"https://telegra.ph/{edit_path}"
        except _TRANSIENT_NET_ERRORS as e:
            # Transient failure even after one retry — don't fall through
            # to create_page; returning None lets the cache-hygiene gate
            # skip the store so the next tap retries the edit cleanly.
            logger.error(
                "Telegraph edit_page transient failure after retry; "
                "NOT falling back to create_page (would leak URL). "
                "path=%r input_html=%d cleaned_html=%d type=%s: %s",
                edit_path,
                len(content),
                len(cleaned_html),
                type(e).__name__,
                e,
            )
            return None
        except Exception as e:
            # Non-transient edit failure (page deleted, content too big,
            # auth issue) — fall through to create_page. User gets a
            # fresh URL, which is acceptable degradation vs. blocking
            # the publish on a broken edit-target.
            logger.warning(
                "Telegraph edit_page failed for path=%r (will fall back to "
                "create_page). type=%s: %s",
                edit_path,
                type(e).__name__,
                e,
            )

    try:
        page = await _call_with_transient_retry(
            client.create_page,
            title=title,
            html_content=cleaned_html,
            author_name="TradingAgents Bot",
        )
        return f"https://telegra.ph/{page['path']}"
    except Exception as e:
        logger.error(
            "Failed to publish to Telegraph. input_html=%d cleaned_html=%d type=%s: %s",
            len(content),
            len(cleaned_html),
            type(e).__name__,
            e,
        )
        return None
