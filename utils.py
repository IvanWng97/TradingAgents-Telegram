"""
Utility functions for the bot.
Message formatting and sending.
"""

import logging
import os
from pathlib import Path
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from telegraph import Telegraph

load_dotenv(Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# Initialize Telegraph client with access token
telegraph_token = os.getenv("TELEGRAPH_ACCESS_TOKEN")
telegraph = Telegraph(access_token=telegraph_token)


def sanitize_html_for_telegraph(html: str) -> str:
    """Convert HTML to Telegraph-compatible format."""
    soup = BeautifulSoup(html, "html.parser")

    # Map unsupported HTML tags to Telegraph-supported ones
    tag_map = {
        "h1": "h3",
        "h2": "h3",
        "h3": "h4",
        "h4": "h4",
        "h5": "h4",
        "h6": "h4",
    }

    for element in soup.find_all(list(tag_map.keys())):
        element.name = tag_map[element.name]

    return str(soup)


async def publish_to_telegraph(title: str, content: str) -> str:
    """Publish content to Telegraph and return URL."""
    try:
        cleaned_html = sanitize_html_for_telegraph(content)
        page = telegraph.create_page(
            title=title, html_content=cleaned_html, author_name="TradingAgents Bot"
        )
        return f"https://telegra.ph/{page['path']}"
    except Exception as e:
        logger.error(f"Failed to publish to Telegraph: {e}")
        return None


def format_analysis_result_markdown(ticker: str, final_state: dict, signal: str) -> str:
    """Format analysis result as Markdown.

    Currently emits only the final trade decision — other sections from
    final_state can be appended later.
    """
    return (
        f"**{ticker} Analysis Result**\n\n"
        f"**Decision:** {signal}\n\n"
        f"# Final Trade Decision\n\n"
        f"{final_state.get('final_trade_decision', 'N/A')}\n"
    )


def format_short_message(ticker: str, signal: str, telegraph_url: str = None) -> str:
    """Format short message for Telegram with Telegraph link."""
    message = f"*{ticker} Analysis Result*\n\n" f"📊 **Decision**: `{signal}`\n\n"
    if telegraph_url:
        message += f"📄 [View Full Report]({telegraph_url})"
    return message
