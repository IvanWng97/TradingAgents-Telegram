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


def sanitize_html_for_telegraph(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(list(_TAG_MAP.keys())):
        element.name = _TAG_MAP[element.name]
    return str(soup)


async def publish_to_telegraph(title: str, content: str) -> Optional[str]:
    """Publish HTML content to Telegraph; returns the page URL or None on failure.

    The Telegraph SDK uses synchronous HTTP under the hood, so we offload
    create_page to a worker thread to keep PTB's event loop responsive.
    """
    try:
        cleaned_html = sanitize_html_for_telegraph(content)
        client = _telegraph_client()
        page = await asyncio.to_thread(
            client.create_page,
            title=title,
            html_content=cleaned_html,
            author_name="TradingAgents Bot",
        )
        return f"https://telegra.ph/{page['path']}"
    except Exception as e:
        logger.error("Failed to publish to Telegraph: %s", e)
        return None
