"""
Scheduled market reports execution.
"""
import logging
from datetime import datetime
from pytz import timezone

from google import genai
from google.genai import types as genai_types

from .market_data import get_live_indices, get_categorized_news, get_precious_metal_rates, compute_market_mood
from .screener_data import get_market_movers
from .sentiment import batch_classify_headlines
from .tools_impls import get_macro_indicators
from .gmail_spending import send_gmail

logger = logging.getLogger("reports")
IST = timezone("Asia/Kolkata")
# Use the same model logic
from .agents import get_client, MODEL_NAME

async def generate_pre_market_report(uid: str, email_to: str):
    macro = get_macro_indicators()
    indices = get_live_indices()
    news = get_categorized_news(limit_each=5)
    metals = get_precious_metal_rates()
    
    prompt = f"""
    You are an expert market analyst compiling a pre-market morning brief.
    Use Google Search grounding to find the overnight US market close, key Asian markets today, crude oil and USD-INR moves, and any major scheduled economic events today.
    Also, provide a hedged, never-guaranteed directional read for Nifty/Sensex for today.
    Explicitly omit any data point you cannot reliably ground rather than guessing. 
    
    Current known data:
    Macro: {macro}
    Indices: {indices}
    Metals: {metals}
    News: {news}
    
    Return your report formatted entirely in HTML suitable for an email body. Do not include markdown code block backticks around the HTML.
    Include a disclaimer at the bottom: "Generated automatically — not investment advice."
    """
    
    client = get_client()
    try:
        resp = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                temperature=0.3
            ),
        )
        html_content = (resp.text or "").strip()
        if html_content.startswith("```html"):
            html_content = html_content[7:]
        if html_content.endswith("```"):
            html_content = html_content[:-3]
            
        send_gmail(
            uid=uid,
            to=email_to,
            subject=f"Pre-Market Brief - {datetime.now(IST).strftime('%d %b %Y')}",
            html_body=html_content
        )
    except Exception as e:
        logger.error(f"Pre-market report generation failed: {e}")
        raise

async def generate_eod_report(uid: str, email_to: str):
    indices = get_live_indices()
    movers = get_market_movers(limit=10)
    mood = compute_market_mood(indices)
    
    # Classify headlines
    raw_news = get_categorized_news(limit_each=6)
    headlines = []
    for cat, items in raw_news.items():
        if isinstance(items, list):
            for i in items:
                title = i.get("title")
                if title:
                    headlines.append(title)
    
    sentiment_scores = batch_classify_headlines(headlines) if headlines else {}
    
    prompt = f"""
    You are an expert market analyst compiling an End-of-Day (EOD) market report.
    Summarize the day's market action based on the following data.
    Narrate index moves, leading/lagging sectors, market breadth, and one line on what to watch tomorrow.
    
    Current data:
    Indices: {indices}
    Movers: {movers}
    Mood: {mood}
    Headline Sentiments: {sentiment_scores}
    
    Return your report formatted entirely in HTML suitable for an email body. Do not include markdown code block backticks around the HTML.
    Include a disclaimer at the bottom: "Generated automatically — not investment advice."
    """
    
    client = get_client()
    try:
        resp = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.3
            ),
        )
        html_content = (resp.text or "").strip()
        if html_content.startswith("```html"):
            html_content = html_content[7:]
        if html_content.endswith("```"):
            html_content = html_content[:-3]
            
        send_gmail(
            uid=uid,
            to=email_to,
            subject=f"EOD Market Report - {datetime.now(IST).strftime('%d %b %Y')}",
            html_body=html_content
        )
    except Exception as e:
        logger.error(f"EOD report generation failed: {e}")
        raise
