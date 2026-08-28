import json
import base64
from cryptography.fernet import Fernet
from google import genai
from google.genai import types
from .secrets import get_broker_credential_salt

_client = None

def get_client():
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project="gen-lang-client-0048936678", location="us-central1")
    return _client

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
Maintain a professional, concise, and analytical persona. Keep your responses short and suitable for audio output.
"""

from .agents import Orchestrator

class CFAMultiAgentBot:
    def __init__(self, voice_persona: str = "Aoede"):
        """
        Initializes the Multi-Agent CFA Bot with a TTS model for Voice synthesis.
        voice_persona options: "Puck", "Charon", "Kore", "Fenrir", "Aoede"
        """
        self.client = get_client()
        self.orchestrator = Orchestrator()
        
        # Configure structural capability parameters for native audio workflows
        self.voice_model_name = "gemini-2.5-flash-preview-tts"
        self.voice_config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_persona
                    )
                )
            )
        )

    def process_query(self, user_prompt: str, audio_bytes: bytes = None, mode: str = "TEXT"):
        """
        Routes the user prompt to the orchestrator.
        If audio_bytes is provided, transcribes it via Gemini first.
        If mode is VOICE, it reads the synthesized text aloud via Vertex TTS.
        """
        if audio_bytes:
            audio_part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/webm")
            transcribe_resp = self.client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[audio_part, "Transcribe the following audio accurately into text. Return ONLY the transcribed text."]
            )
            user_prompt = transcribe_resp.text.strip()
            print(f"Transcribed audio: {user_prompt}")

        # 1. Gather expert analysis
        raw_answer = self.orchestrator.process_query(user_prompt)
        
        # 2. Enforce regulatory disclaimer
        disclaimer = "DISCLAIMER: I am an AI assistant, not a licensed financial advisor. The following information is for educational purposes only and does not constitute financial or trading advice. Execute trades at your own risk."
        final_text = f"{disclaimer}\n\n{raw_answer}"
        
        # 3. Handle modalities
        if mode.upper() == "VOICE":
            audio_prompt = f"Please read the following text exactly as written, maintaining a professional tone:\n\n{final_text}"
            audio_response = self.client.models.generate_content(
                model=self.voice_model_name,
                contents=audio_prompt,
                config=self.voice_config
            )
            return audio_response, final_text
        else:
            return None, final_text

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
    client = get_client()

    titles_block = "\n".join([f"{i+1}. {h}" for i, h in enumerate(headlines)])
    prompt = f"""Analyze the following {len(headlines)} financial news headlines for Indian equity market sentiment.
For each headline, classify sentiment as BULLISH, BEARISH, or NEUTRAL from an Indian market perspective.
Return a JSON array with exactly {len(headlines)} objects in the same order, each with:
  "index": <1-based integer>,
  "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL"

Headlines:
{titles_block}"""

    config = types.GenerateContentConfig(response_mime_type="application/json")
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=config
        )
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
    client = get_client()
    config = types.GenerateContentConfig(
        system_instruction=ANALYST_SYSTEM_INSTRUCTION,
        response_mime_type="application/json"
    )
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=raw_news_payload,
        config=config
    )
    try:
        data = json.loads(response.text)
        return data if isinstance(data, list) else [data]
    except Exception as e:
        print(f"Failed to parse JSON response: {e}")
        return []
