import json
import base64
from cryptography.fernet import Fernet
import google.generativeai as genai
from .secrets import get_gemini_api_key, get_broker_credential_salt

_is_configured = False

def _ensure_configured():
    global _is_configured
    if not _is_configured:
        genai.configure(api_key=get_gemini_api_key())
        _is_configured = True

# ─────────────────────────────────────────────────────────────────────────────
# CFA Voice Bot
# ─────────────────────────────────────────────────────────────────────────────
CFA_SYSTEM_INSTRUCTION = """
You are a Chartered Financial Analyst (CFA) Voice Assistant for a Secure Finance Productivity Journal.
Your primary role is to provide analytical, objective, and data-driven insights based on market data.

MANDATORY REGULATORY RISK DISCLAIMER:
You MUST prefix any analysis or output with the following disclaimer:
"DISCLAIMER: I am an AI assistant, not a licensed financial advisor. The following information is for educational purposes only and does not constitute financial or trading advice. Execute trades at your own risk."

You operate in a multi-turn conversational context utilizing Voice-to-Voice capabilities.
Maintain a professional, concise, and analytical persona.
"""

def initialize_cfa_bot():
    _ensure_configured()
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=CFA_SYSTEM_INSTRUCTION,
    )
    return model

# ─────────────────────────────────────────────────────────────────────────────
# Smart Order Routing Engine & Safeguards
# ─────────────────────────────────────────────────────────────────────────────
class SmartOrderRouter:
    MAX_ORDER_SIZE = 100
    MAX_SLIPPAGE_PERCENT = 1.0

    def __init__(self, broker_type: str, encrypted_broker_token: str):
        self.broker_type = broker_type
        self._broker_token = self._decrypt_token_in_memory(encrypted_broker_token)

    def _decrypt_token_in_memory(self, encrypted_token: str) -> str:
        if not encrypted_token:
            return ""
        try:
            salt = get_broker_credential_salt()
            try:
                f = Fernet(salt.encode())
            except ValueError:
                import hashlib
                hashed_salt = base64.urlsafe_b64encode(hashlib.sha256(salt.encode()).digest())
                f = Fernet(hashed_salt)
            return f.decrypt(encrypted_token.encode()).decode()
        except Exception as e:
            print(f"Decryption failed: {e}")
            return "decryption_failed"

    def route_order(self, ticker: str, quantity: int, price: float, current_market_price: float):
        if quantity > self.MAX_ORDER_SIZE:
            raise ValueError(f"Order quantity {quantity} exceeds maximum allowed size of {self.MAX_ORDER_SIZE}.")
        if current_market_price <= 0:
            raise ValueError("Invalid current market price.")
        slippage = abs(price - current_market_price) / current_market_price * 100.0
        if slippage > self.MAX_SLIPPAGE_PERCENT:
            raise ValueError(f"Requested price slippage ({slippage:.2f}%) exceeds maximum allowed ({self.MAX_SLIPPAGE_PERCENT}%).")
        return {
            "status": "EXECUTED",
            "broker": self.broker_type,
            "ticker": ticker,
            "quantity": quantity,
            "execution_price": price,
            "slippage_percent": slippage,
        }

# ─────────────────────────────────────────────────────────────────────────────
# Batch Headline Sentiment (single Gemini call for all headlines)
# ─────────────────────────────────────────────────────────────────────────────
def batch_analyze_headlines(headlines: list) -> dict:
    """
    Analyzes a list of news headlines in a single Gemini call.
    Returns a dict mapping title -> sentiment (BULLISH/BEARISH/NEUTRAL)
    """
    if not headlines:
        return {}
    _ensure_configured()

    titles_block = "\n".join([f"{i+1}. {h}" for i, h in enumerate(headlines)])
    prompt = f"""Analyze the following {len(headlines)} financial news headlines for Indian equity market sentiment.
For each headline, classify sentiment as BULLISH, BEARISH, or NEUTRAL from an Indian market perspective.
Return a JSON array with exactly {len(headlines)} objects in the same order, each with:
  "index": <1-based integer>,
  "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL"

Headlines:
{titles_block}"""

    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        generation_config=genai.GenerationConfig(response_mime_type="application/json")
    )
    try:
        response = model.generate_content(prompt)
        data = json.loads(response.text)
        result = {}
        for item in (data if isinstance(data, list) else []):
            idx = item.get("index", 0) - 1
            if 0 <= idx < len(headlines):
                result[headlines[idx]] = item.get("sentiment", "NEUTRAL").upper()
        return result
    except Exception as e:
        print(f"Batch analysis failed: {e}")
        return {}

# ─────────────────────────────────────────────────────────────────────────────
# Full Signal Analysis (structured JSON for a single news text)
# ─────────────────────────────────────────────────────────────────────────────
ANALYST_SYSTEM_INSTRUCTION = """
You are an Elite SEBI-registered Equity Research Analyst.
Your task is to analyze Indian Equity Market news and extract signals.
You MUST return a JSON array containing objects matching this EXACT schema:
[{
    "ticker": "NSE/BSE string symbol (e.g., RELIANCE, HDFCBANK, TCS)",
    "sector": "Sector name (e.g., Banking, Energy, IT, Metals, Power)",
    "sentiment": "BULLISH", "BEARISH", or "NEUTRAL",
    "impact_score": <integer from 1 to 10>,
    "macro_risk_flag": <boolean>,
    "reasoning": "Short 10-word summary of market impact"
}]
"""

def analyze_indian_market_news(raw_news_payload: str) -> list:
    """
    Analyzes Indian market news using Gemini and returns a structured JSON payload of market signals.
    """
    _ensure_configured()
    model = genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=ANALYST_SYSTEM_INSTRUCTION,
        generation_config=genai.GenerationConfig(response_mime_type="application/json")
    )
    response = model.generate_content(raw_news_payload)
    try:
        data = json.loads(response.text)
        return data if isinstance(data, list) else [data]
    except Exception as e:
        print(f"Failed to parse JSON response: {e}")
        return []
