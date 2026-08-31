import json
import base64
import logging
from cryptography.fernet import Fernet
from groq import Groq
from google.cloud import speech
from google.cloud import texttospeech
from starlette.concurrency import run_in_threadpool

from .secrets import get_broker_credential_salt, access_secret_version
from .agents import Orchestrator, MODEL_NAME, _strip_code_fences

_client = None

def get_client():
    global _client
    if _client is None:
        api_key = access_secret_version("GROQ_API_KEY")
        _client = Groq(api_key=api_key)
    return _client

# ─────────────────────────────────────────────────────────────────────────────
# CFA Voice Bot — powered by Google Cloud Speech-to-Text (input) and
# Google Cloud Text-to-Speech (output). Deep-dive reasoning itself lives in
# the multi-agent Orchestrator (see agents.py); this class is responsible for
# turning voice into text, running the orchestrator, and turning the answer
# back into speech.
# ─────────────────────────────────────────────────────────────────────────────

# Maps the friendly "persona" names used across the app to real Google Cloud
# Text-to-Speech Neural2 voices. Feel free to swap these for WaveNet/Studio
# voices if your GCP project has access to them.
PERSONA_VOICE_MAP = {
    "Aoede":  {"language_code": "en-US", "name": "en-US-Neural2-F", "ssml_gender": texttospeech.SsmlVoiceGender.FEMALE},
    "Kore":   {"language_code": "en-US", "name": "en-US-Neural2-C", "ssml_gender": texttospeech.SsmlVoiceGender.FEMALE},
    "Puck":   {"language_code": "en-US", "name": "en-US-Neural2-D", "ssml_gender": texttospeech.SsmlVoiceGender.MALE},
    "Charon": {"language_code": "en-US", "name": "en-US-Neural2-A", "ssml_gender": texttospeech.SsmlVoiceGender.MALE},
    "Fenrir": {"language_code": "en-US", "name": "en-US-Neural2-I", "ssml_gender": texttospeech.SsmlVoiceGender.MALE},
}
DEFAULT_PERSONA = "Aoede"

DISCLAIMER = (
    "DISCLAIMER: I am an AI assistant, not a licensed financial advisor. "
    "The following information is for educational purposes only and does not "
    "constitute financial or trading advice. Execute trades at your own risk."
)

class CFAMultiAgentBot:
    def __init__(self, voice_persona: str = DEFAULT_PERSONA):
        """
        Initializes the Multi-Agent CFA Bot.
        voice_persona options: "Puck", "Charon", "Kore", "Fenrir", "Aoede"
        (mapped to real Google Cloud TTS voices in PERSONA_VOICE_MAP).
        """
        self.orchestrator = Orchestrator()
        self.voice_persona = voice_persona if voice_persona in PERSONA_VOICE_MAP else DEFAULT_PERSONA
        self._speech_client = None
        self._tts_client = None

    # ── lazily-created GCP clients (avoids paying connection cost when a
    # session never actually uses voice) ──────────────────────────────────
    def _get_speech_client(self) -> speech.SpeechClient:
        if self._speech_client is None:
            self._speech_client = speech.SpeechClient()
        return self._speech_client

    def _get_tts_client(self) -> texttospeech.TextToSpeechClient:
        if self._tts_client is None:
            self._tts_client = texttospeech.TextToSpeechClient()
        return self._tts_client

    def transcribe_audio(self, audio_bytes: bytes) -> str:
        """
        Transcribes recorded audio to text using Google Cloud Speech-to-Text.
        First tries auto-detecting the encoding/sample rate from the WAV header
        (leaving `encoding`/`sample_rate_hertz` unset). If that's rejected, falls
        back to explicit LINEAR16 @ 16kHz, which is what st.audio_input records
        by default.
        """
        client = self._get_speech_client()
        audio = speech.RecognitionAudio(content=audio_bytes)

        def _run(config: speech.RecognitionConfig):
            response = client.recognize(config=config, audio=audio)
            return " ".join(
                result.alternatives[0].transcript
                for result in response.results
                if result.alternatives
            ).strip()

        try:
            return _run(speech.RecognitionConfig(
                language_code="en-US",
                enable_automatic_punctuation=True,
                model="latest_long",
            ))
        except Exception as e:
            logging.warning(f"STT auto-detect config failed ({e}); retrying with explicit LINEAR16/16kHz")
            return _run(speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=16000,
                language_code="en-US",
                enable_automatic_punctuation=True,
                model="latest_long",
            ))

    def synthesize_speech(self, text: str) -> bytes:
        """
        Converts text to speech using Google Cloud Text-to-Speech and
        returns MP3-encoded audio bytes.
        """
        client = self._get_tts_client()
        voice_cfg = PERSONA_VOICE_MAP[self.voice_persona]

        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice = texttospeech.VoiceSelectionParams(
            language_code=voice_cfg["language_code"],
            name=voice_cfg["name"],
            ssml_gender=voice_cfg["ssml_gender"],
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=1.0,
        )
        response = client.synthesize_speech(
            input=synthesis_input, voice=voice, audio_config=audio_config
        )
        return response.audio_content

    async def process_query(self, user_prompt: str = "", audio_bytes: bytes = None, mode: str = "TEXT", location: str = None, history: list = None):
        """
        Handles a single turn of the conversation:
          1. If audio was recorded, transcribe it (Google Cloud STT) to get the question.
          2. Route the question through the multi-agent Orchestrator for a deep analysis.
          3. Prepend the compliance disclaimer.
          4. If the caller wants a spoken reply (mode == "VOICE"), synthesize it
             (Google Cloud TTS).

        `location` (optional) is the browser-supplied "lat,lon" string — only used
        by the leisure agent (movies/restaurants nearby), ignored by finance agents.

        Returns a (audio_bytes_or_None, final_text, transcript, route_meta_or_None) tuple.
        route_meta is {"source": ..., "destination": ...} whenever the leisure
        agent successfully called PathSense's get_safe_route this turn, else None.

        Async so the orchestrator can be awaited directly on the caller's own
        event loop instead of going through Orchestrator.process_query(), which
        wraps every call in a fresh asyncio.run() — that spins up and tears
        down a new loop per request, and the module-level cached genai.Client
        in agents.py binds its async transport to whichever loop was live the
        first time it was used. After the first call, every later call ran on
        a different loop and every generate_content_async raised "Event loop
        is closed" (see the router fallback log). The blocking Google Cloud
        STT/TTS calls are pushed onto a thread via run_in_threadpool so they
        don't block this same event loop.
        """
        transcript = None
        if audio_bytes:
            try:
                transcript = await run_in_threadpool(self.transcribe_audio, audio_bytes)
            except Exception as e:
                logging.exception("Speech-to-text failed")
                # Surface the real reason (e.g. API not enabled / missing IAM permission /
                # unsupported audio encoding) instead of a generic message, so it's
                # diagnosable from the UI without having to dig through Cloud Run logs.
                return None, f"Sorry, speech-to-text failed: {e}", None, None
            if not transcript:
                return None, "I recorded audio but didn't detect any speech in it. Try again, speak right after pressing record, and make sure your mic isn't muted.", transcript, None
            user_prompt = transcript

        user_prompt = (user_prompt or "").strip()
        if not user_prompt:
            return None, "I didn't catch a question — please type or speak it in.", transcript, None

        # 1. Gather deep, multi-domain expert analysis — awaited directly,
        # not via the sync asyncio.run() wrapper.
        raw_answer, route_meta = await self.orchestrator.process_query_async(user_prompt, location=location, history=history)

        # 2. Enforce regulatory disclaimer
        final_text = f"{DISCLAIMER}\n\n{raw_answer}"

        # 3. Optionally synthesize a spoken reply
        audio_out = None
        if mode.upper() == "VOICE":
            try:
                audio_out = await run_in_threadpool(self.synthesize_speech, final_text)
            except Exception as e:
                logging.exception("Text-to-speech failed")
                final_text += f"\n\n[Voice reply unavailable: {e}]"
                audio_out = None

        return audio_out, final_text, transcript, route_meta

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
# Batch Headline Sentiment (single Groq call for all headlines)
# ─────────────────────────────────────────────────────────────────────────────
def batch_analyze_headlines(headlines: list) -> dict:
    """
    Analyzes a list of news headlines in a single Groq call.
    Returns a dict mapping title -> sentiment (BULLISH/BEARISH/NEUTRAL)
    """
    if not headlines:
        return {}
    client = get_client()

    titles_block = "\n".join([f"{i+1}. {h}" for i, h in enumerate(headlines)])
    prompt = f"""Analyze the following {len(headlines)} financial news headlines for Indian equity market sentiment.
For each headline, classify sentiment as BULLISH, BEARISH, or NEUTRAL from an Indian market perspective.
Return a JSON object of the form {{"results": [...]}}  containing exactly {len(headlines)} objects in the
same order, each with:
  "index": <1-based integer>,
  "sentiment": "BULLISH" | "BEARISH" | "NEUTRAL"

Headlines:
{titles_block}"""

    try:
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        parsed = json.loads(_strip_code_fences(response.choices[0].message.content))
        data = parsed.get("results", []) if isinstance(parsed, dict) else parsed
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
You MUST return a JSON object of the form {"signals": [...]}, where the array contains objects
matching this EXACT schema:
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
    Analyzes Indian market news using Groq and returns a structured JSON payload of market signals.
    """
    client = get_client()
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": ANALYST_SYSTEM_INSTRUCTION},
            {"role": "user", "content": raw_news_payload},
        ],
        response_format={"type": "json_object"},
    )
    try:
        parsed = json.loads(_strip_code_fences(response.choices[0].message.content))
        data = parsed.get("signals", []) if isinstance(parsed, dict) else parsed
        return data if isinstance(data, list) else [data]
    except Exception as e:
        print(f"Failed to parse JSON response: {e}")
        return []