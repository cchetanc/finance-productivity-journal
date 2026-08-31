import yfinance as yf
import feedparser
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# VERIFIED WORKING TICKERS (tested against Yahoo Finance)
# ─────────────────────────────────────────────────────────────────────────────
INDICES = {
    # ── Broad Market ──────────────────────────────────────────────────────────
    "Nifty 50":    "^NSEI",
    "Sensex":      "^BSESN",
    # ── Banking & Finance ─────────────────────────────────────────────────────
    "Nifty Bank":  "^NSEBANK",
    # ── Sectoral ──────────────────────────────────────────────────────────────
    "Nifty IT":    "^CNXIT",
    "Nifty Auto":  "^CNXAUTO",
    "Nifty Pharma":"^CNXPHARMA",
    "Nifty FMCG":  "^CNXFMCG",
    "Nifty Energy":"^CNXENERGY",
    "Nifty Metal": "^CNXMETAL",
    "Nifty Realty":"^CNXREALTY",
    "Nifty Infra": "^CNXINFRA",
    # ── Global ────────────────────────────────────────────────────────────────
    "NASDAQ":      "^IXIC",
    "S&P 500":     "^GSPC",
    "Dow Jones":   "^DJI",
}

# ─────────────────────────────────────────────────────────────────────────────
# GROUP METADATA for frontend display
# ─────────────────────────────────────────────────────────────────────────────
INDEX_GROUPS = {
    "Broad Market":   ["Nifty 50", "Sensex"],
    "Banking":        ["Nifty Bank"],
    "Sectoral":       ["Nifty IT", "Nifty Auto", "Nifty Pharma", "Nifty FMCG",
                       "Nifty Energy", "Nifty Metal", "Nifty Realty", "Nifty Infra"],
    "Global":         ["NASDAQ", "S&P 500", "Dow Jones"],
}

# ─────────────────────────────────────────────────────────────────────────────
# LIVE NEWS RSS FEEDS
# ─────────────────────────────────────────────────────────────────────────────
RSS_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "https://www.moneycontrol.com/rss/marketreports.xml",
]

# General (non-market-specific) headline feeds for the Global/India/Local
# news panel — deliberately separate from RSS_FEEDS above, which stays
# finance-focused for the FinBERT-scored "Live Wire" panel. Google News'
# section/search RSS needs no API key and covers both general World/India
# news and arbitrary city-scoped search in one consistent format.
GLOBAL_NEWS_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-IN&gl=IN&ceid=IN:en",
]
INDIA_NEWS_FEEDS = [
    "https://news.google.com/rss/headlines/section/geo/India?hl=en-IN&gl=IN&ceid=IN:en",
]

# Movie/series release feeds for the "New Releases" tile — theatrical
# (box-office) vs. OTT platforms. No dedicated movie-database API key is
# wired up yet, so this reuses the same key-less Google News search RSS
# pattern as the local-news feed above rather than adding a new dependency.
ENTERTAINMENT_FEEDS = {
    "theatres": "https://news.google.com/rss/search?q=%22box%20office%22%20OR%20%22releasing%20this%20week%22%20movie%20India%20theatres&hl=en-IN&gl=IN&ceid=IN:en",
    "ott": "https://news.google.com/rss/search?q=(Netflix%20OR%20%22Amazon%20Prime%22%20OR%20%22Prime%20Video%22%20OR%20%22JioHotstar%22%20OR%20%22Disney%2BHotstar%22%20OR%20Zee5)%20new%20release%20this%20week&hl=en-IN&gl=IN&ceid=IN:en",
}


def _local_news_feed(city: str) -> str:
    from urllib.parse import quote
    return f"https://news.google.com/rss/search?q={quote(city)}&hl=en-IN&gl=IN&ceid=IN:en"


def get_live_indices() -> list:
    """
    Fetches real-time price and change for all verified index tickers.
    Uses history(period='2d') which is more reliable than fast_info.
    """
    results = []
    for name, symbol in INDICES.items():
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="2d")

            if hist.empty or len(hist) < 1:
                # A weekend/holiday gap (or a transient feed hiccup) can
                # leave "2d" short even though the ticker is fine — widen
                # the window once before giving up, so a normal non-trading
                # day doesn't get misreported as "no data".
                hist = ticker.history(period="5d")

            if hist.empty or len(hist) < 1:
                raise ValueError("No data returned")

            import math
            price = float(hist["Close"].iloc[-1])
            if math.isnan(price):
                price = 0.0

            prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else float(hist["Open"].iloc[-1])
            if math.isnan(prev):
                prev = price

            change     = round(price - prev, 2)
            change_pct = round((change / prev) * 100, 2) if prev and not math.isnan(prev) and prev != 0 else 0.0
            if math.isnan(change_pct):
                change_pct = 0.0
            positive   = change >= 0

            results.append({
                "name":       name,
                "symbol":     symbol,
                "price":      round(price, 2) if price else 0.0,
                "change":     change,
                "change_pct": change_pct,
                "positive":   positive,
            })
        except Exception as e:
            print(f"Failed {name} ({symbol}): {e}")
            results.append({
                "name": name, "symbol": symbol,
                "price": 0, "change": 0, "change_pct": 0, "positive": False,
            })
    return results


def compute_market_mood(indices: list) -> dict:
    """
    Derives quantitative bias score and risk flags from live index breadth.
    """
    if not indices:
        return {"systemic_score": 50, "bias": "NEUTRAL", "macro_risk_flags": []}

    with_data   = [i for i in indices if i["price"] > 0]
    if not with_data:
        return {"systemic_score": 50, "bias": "NEUTRAL", "macro_risk_flags": ["No live data"]}

    positive_count = sum(1 for i in with_data if i["positive"])
    score = round((positive_count / len(with_data)) * 100)

    bias  = "BULLISH" if score >= 65 else ("BEARISH" if score < 40 else "NEUTRAL")

    flags = [
        f"{i['name']} {'+' if i['positive'] else ''}{i['change_pct']:.2f}%"
        for i in sorted(with_data, key=lambda x: abs(x["change_pct"]), reverse=True)
        if abs(i["change_pct"]) > 0.25
    ][:4]

    if not flags:
        flags = ["Markets trading near flat"]

    return {"systemic_score": score, "bias": bias, "macro_risk_flags": flags}


def _parse_feeds(feed_urls: list, limit: int) -> list:
    """Shared RSS-parsing core used by get_live_news and get_categorized_news
    — pulls entries from each feed, tags them with a per-entry display
    source (falls back to the feed's own title if an entry doesn't carry
    one, which Google News entries usually do via their <source> element),
    and de-duplicates by title across feeds."""
    headlines = []
    for feed_url in feed_urls:
        try:
            feed = feedparser.parse(feed_url)
            feed_title = feed.feed.get("title", "News")
            for entry in feed.entries[:limit]:
                try:
                    pub_dt = datetime(*entry.published_parsed[:6])
                    time_str = pub_dt.strftime("%H:%M")
                except Exception:
                    time_str = "Live"
                source = entry.get("source", {}).get("title") if isinstance(entry.get("source"), dict) else None
                headlines.append({
                    "title": entry.get("title", ""),
                    "source": source or feed_title,
                    "link": entry.get("link", ""),
                    "published": time_str,
                })
        except Exception as e:
            print(f"Failed RSS {feed_url}: {e}")

    seen, unique = set(), []
    for h in headlines:
        if h["title"] and h["title"] not in seen:
            seen.add(h["title"])
            unique.append(h)
    return unique[:limit]


def get_live_news(limit: int = 12) -> list:
    """
    Fetches latest market news headlines from ET and Moneycontrol RSS feeds.
    """
    return _parse_feeds(RSS_FEEDS, limit)


def get_categorized_news(city: str | None = None, limit_each: int = 4) -> dict:
    """Global / India / Local general-news headlines for the dashboard's
    news panel. 'Local' needs a city (from the browser's reverse-geocoded
    location) — omitted entirely rather than guessing when none is given,
    so the frontend can show its own 'enable location' placeholder instead
    of us silently returning something misleading."""
    result = {
        "global": _parse_feeds(GLOBAL_NEWS_FEEDS, limit_each),
        "india": _parse_feeds(INDIA_NEWS_FEEDS, limit_each),
        "local": [],
    }
    if city and city.strip():
        # A reverse-geocoded location can be "Suburb, City, State" — the
        # city/area segment alone is a far better search query than the
        # whole string, which Google News' search endpoint tends to return
        # nothing useful for.
        query = city.split(",")[0].strip()
        result["local"] = _parse_feeds([_local_news_feed(query)], limit_each)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CORPORATE ACTIONS / RESULTS CALENDAR
# ─────────────────────────────────────────────────────────────────────────────
# NSE/BSE do publish structured corporate-announcement JSON APIs, but both
# require browser session cookies and aggressively rate-limit/block
# server-side requests without them — unreliable to depend on for a
# production feature. This uses the same key-less Google News search RSS
# approach as the rest of the news panels: it gives real, clickable
# headlines with working links, just not a structured ex-date/record-date
# table the way a paid corporate-actions feed would.
CORP_ACTION_FEEDS = {
    "dividends": "https://news.google.com/rss/search?q=dividend%20(%22record%20date%22%20OR%20%22ex-date%22)%20NSE%20India&hl=en-IN&gl=IN&ceid=IN:en",
    "other_actions": "https://news.google.com/rss/search?q=(bonus%20shares%20OR%20stock%20split%20OR%20buyback)%20announcement%20NSE%20BSE%20India&hl=en-IN&gl=IN&ceid=IN:en",
    "results_calendar": "https://news.google.com/rss/search?q=%22quarterly%20results%22%20OR%20%22Q2%20results%22%20OR%20%22Q3%20results%22%20date%20announcement%20NSE%20India&hl=en-IN&gl=IN&ceid=IN:en",
}


def get_dividend_announcements(limit: int = 20) -> list:
    return _parse_feeds([CORP_ACTION_FEEDS["dividends"]], limit)


def get_corporate_action_announcements(limit: int = 20) -> list:
    """Bonus issues, stock splits, buybacks — everything except dividends,
    which get their own feed above since they're the highest-volume
    category and usually what people want a dedicated view for."""
    return _parse_feeds([CORP_ACTION_FEEDS["other_actions"]], limit)


def get_results_calendar(limit: int = 20) -> list:
    return _parse_feeds([CORP_ACTION_FEEDS["results_calendar"]], limit)


def get_entertainment_releases(limit_each: int = 4) -> dict:
    """Movie & series release headlines for the 'New Releases' tile —
    theatrical/box-office on one side, OTT platforms (Netflix, Prime Video,
    JioHotstar/Disney+Hotstar, Zee5) on the other. Same RSS-search approach
    as the news panels, so it needs no new API key or dependency."""
    return {
        "theatres": _parse_feeds([ENTERTAINMENT_FEEDS["theatres"]], limit_each),
        "ott": _parse_feeds([ENTERTAINMENT_FEEDS["ott"]], limit_each),
    }


def get_precious_metal_rates() -> dict:
    """Gold/silver rates for the header pills. Uses the same yfinance
    source as the rest of the app (COMEX futures GC=F / SI=F, quoted in
    USD per troy ounce) converted to INR/10g and INR/kg via the live
    USD-INR rate. This is an INTERNATIONAL SPOT PROXY, not the exact
    Indian bullion-association retail rate — it excludes import duty,
    GST, and local dealer premiums, so it will run somewhat below the
    price a local jeweller quotes. Flagged clearly in the frontend label
    for that reason, rather than presented as an authoritative local rate."""
    try:
        gold_hist   = yf.Ticker("GC=F").history(period="2d")
        silver_hist = yf.Ticker("SI=F").history(period="2d")
        fx_hist     = yf.Ticker("INR=X").history(period="2d")

        if gold_hist.empty or silver_hist.empty or fx_hist.empty:
            raise ValueError("No data returned")

        gold_usd_oz   = float(gold_hist["Close"].iloc[-1])
        silver_usd_oz = float(silver_hist["Close"].iloc[-1])
        usd_inr       = float(fx_hist["Close"].iloc[-1])

        GRAMS_PER_TROY_OZ = 31.1035

        gold_inr_per_gram   = (gold_usd_oz / GRAMS_PER_TROY_OZ) * usd_inr
        silver_inr_per_gram = (silver_usd_oz / GRAMS_PER_TROY_OZ) * usd_inr

        return {
            "gold_inr_10g": round(gold_inr_per_gram * 10, 0),
            "silver_inr_kg": round(silver_inr_per_gram * 1000, 0),
            "usd_inr": round(usd_inr, 2),
            "is_proxy": True,
        }
    except Exception as e:
        print(f"[metals] fetch failed: {e}")
        return {"gold_inr_10g": None, "silver_inr_kg": None, "usd_inr": None, "is_proxy": True}