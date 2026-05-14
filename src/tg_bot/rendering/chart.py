"""External chart URL builders."""

import time


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

    Appends a unix-timestamp cache-buster so Telegram (which caches photos by
    URL on its CDN) refetches finviz on every call. Finviz ignores the unknown
    param.
    """
    cache_bust = int(time.time())
    return (
        f"https://finviz.com/chart.ashx?t={ticker}"
        f"&ty={chart_type}&ta={int(indicators)}&p={period}&s={size}"
        f"&_={cache_bust}"
    )
