import feedparser

FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://www.binance.com/en/blog/rss",
]

async def fetch_top_news(limit: int = 6, lang: str = "en"):
    items = []
    for url in FEEDS:
        try:
            d = feedparser.parse(url)
            for e in d.get('entries', [])[:limit]:
                title = e.get('title') or ''
                link = e.get('link') or ''
                items.append(f"📰 {title}\n{link}")
        except Exception:
            continue
        if len(items) >= limit:
            break
    if not items:
        return ["لا توجد أخبار متاحة حالياً"]
    return items[:limit]
