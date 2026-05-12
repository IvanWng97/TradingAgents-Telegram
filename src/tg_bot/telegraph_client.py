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


# Exception types from `requests` that suggest a transient network issue —
# worth one retry. API-level errors (TelegraphException, content too large,
# token invalid) raise different types and shouldn't be retried.
_TRANSIENT_NET_ERRORS: tuple[type[Exception], ...] = ()
try:
    from requests.exceptions import (
        ConnectionError as _RequestsConnectionError,
        Timeout as _RequestsTimeout,
    )

    _TRANSIENT_NET_ERRORS = (_RequestsConnectionError, _RequestsTimeout)
except ImportError:  # pragma: no cover — requests is a transitive dep of telegraph
    pass


async def publish_to_telegraph(title: str, content: str) -> Optional[str]:
    """Publish HTML content to Telegraph; returns the page URL or None on failure.

    The Telegraph SDK uses synchronous HTTP under the hood, so we offload
    create_page to a worker thread to keep PTB's event loop responsive.

    Resilience:
    - Transient network errors (ConnectionError / Timeout) get one retry
      with 1s backoff — Telegraph's API has occasional flakiness, and
      losing the link entirely for a one-off network blip is the worse UX.
    - On any failure, the log records the input/output HTML sizes and the
      exception class so future failures can be diagnosed quickly without
      needing to instrument further.
    """
    cleaned_html = sanitize_html_for_telegraph(content)
    client = _telegraph_client()

    for attempt in range(2):
        try:
            page = await asyncio.to_thread(
                client.create_page,
                title=title,
                html_content=cleaned_html,
                author_name="TradingAgents Bot",
            )
            return f"https://telegra.ph/{page['path']}"
        except _TRANSIENT_NET_ERRORS as e:
            if attempt == 0:
                logger.warning(
                    "Telegraph network error (attempt 1, will retry in 1s): %s: %s",
                    type(e).__name__,
                    e,
                )
                await asyncio.sleep(1.0)
                continue
            logger.error(
                "Failed to publish to Telegraph after retry. "
                "input_html=%d cleaned_html=%d type=%s: %s",
                len(content),
                len(cleaned_html),
                type(e).__name__,
                e,
            )
            return None
        except Exception as e:
            logger.error(
                "Failed to publish to Telegraph. "
                "input_html=%d cleaned_html=%d type=%s: %s",
                len(content),
                len(cleaned_html),
                type(e).__name__,
                e,
            )
            return None
    return None
