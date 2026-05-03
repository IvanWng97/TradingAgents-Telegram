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


def finviz_chart_url(
    ticker: str,
    chart_type: str = "c",
    indicators: bool = True,
    period: str = "d",
    size: str = "l",
) -> str:
    """Return a finviz chart URL.

    chart_type: 'c' candle, 'l' line, 'b' bar.
    indicators: True overlays SMAs/RSI/MACD/volume.
    period: 'd' daily, 'w' weekly, 'm' monthly, 'i5'/'i15'/'i30'/'i60' intraday.
    size: 's' small, 'm' medium, 'l' large.

    A cache-busting timestamp is appended so Telegram (which caches photos by
    URL on its CDN) refetches finviz on every call instead of serving a stale
    image. Finviz ignores the unknown param.
    """
    import time
    cache_bust = int(time.time())
    return (
        f"https://finviz.com/chart.ashx?t={ticker}"
        f"&ty={chart_type}&ta={int(indicators)}&p={period}&s={size}"
        f"&_={cache_bust}"
    )


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

    Emits the final trade decision and the trader's executable plan.
    """
    return (
        f"{final_state.get('final_trade_decision', 'N/A')}\n\n"
        f"{final_state.get('trader_investment_plan', '')}\n"
    )


def format_short_message(ticker: str, signal: str, telegraph_url: str = None) -> str:
    """Format short message for Telegram with Telegraph link."""
    message = f"*{ticker} Analysis Result*\n\n" f"📊 **Decision**: `{signal}`\n\n"
    if telegraph_url:
        message += f"📄 [View Full Report]({telegraph_url})"
    return message
