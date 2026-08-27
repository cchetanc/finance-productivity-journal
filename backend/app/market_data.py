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
                raise ValueError("No data returned")

            price = float(hist["Close"].iloc[-1])
            prev  = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else float(hist["Open"].iloc[-1])
            change     = round(price - prev, 2)
            change_pct = round((change / prev) * 100, 2) if prev else 0.0
            positive   = change >= 0

            results.append({
                "name":       name,
                "symbol":     symbol,
                "price":      round(price, 2),
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


def get_live_news(limit: int = 12) -> list:
    """
    Fetches latest market news headlines from ET and Moneycontrol RSS feeds.
    """
    headlines = []
    for feed_url in RSS_FEEDS:
        try:
            feed   = feedparser.parse(feed_url)
            source = feed.feed.get("title", "Market Wire")
            for entry in feed.entries[:limit]:
                try:
                    pub_dt   = datetime(*entry.published_parsed[:6])
                    time_str = pub_dt.strftime("%H:%M")
                except Exception:
                    time_str = "Live"
                headlines.append({
                    "title":     entry.get("title", ""),
                    "source":    source,
                    "link":      entry.get("link", ""),
                    "published": time_str,
                })
        except Exception as e:
            print(f"Failed RSS {feed_url}: {e}")

    seen   = set()
    unique = []
    for h in headlines:
        if h["title"] and h["title"] not in seen:
            seen.add(h["title"])
            unique.append(h)

    return unique[:limit]
