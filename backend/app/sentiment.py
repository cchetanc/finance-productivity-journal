"""
Financial Sentiment Analysis using ProsusAI/FinBERT via HuggingFace Inference API.
FinBERT is purpose-trained on financial news for positive/negative/neutral classification.
This is significantly more accurate than general-purpose LLMs for market news.
"""
import requests

FINBERT_URL = "https://api-inference.huggingface.co/models/ProsusAI/finbert"

# Label mapping from FinBERT -> our schema
LABEL_MAP = {
    "positive": "BULLISH",
    "negative": "BEARISH",
    "neutral":  "NEUTRAL",
}


def classify_single(text: str, hf_token: str = "") -> str:
    """
    Classifies a single financial headline using FinBERT.
    Returns: 'BULLISH', 'BEARISH', or 'NEUTRAL'
    """
    headers = {}
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"

    try:
        resp = requests.post(
            FINBERT_URL,
            headers=headers,
            json={"inputs": text[:512]},   # FinBERT max 512 tokens
            timeout=15
        )
        if resp.status_code == 200:
            result = resp.json()
            # HF returns [[{label, score}, ...]] for classification
            if isinstance(result, list) and result:
                candidates = result[0] if isinstance(result[0], list) else result
                best = max(candidates, key=lambda x: x.get("score", 0))
                return LABEL_MAP.get(best.get("label", "neutral").lower(), "NEUTRAL")
        elif resp.status_code == 503:
            # Model loading — HF cold start, return NEUTRAL gracefully
            print(f"FinBERT model loading (503), skipping: {text[:60]}")
    except Exception as e:
        print(f"FinBERT error for '{text[:60]}': {e}")
    return "NEUTRAL"


def batch_classify_headlines(headlines: list, hf_token: str = "") -> dict:
    """
    Classifies a list of news headlines using FinBERT.
    Returns dict mapping headline -> sentiment.
    Processes sequentially (HF free tier rate limits parallel calls).
    """
    results = {}
    for title in headlines:
        results[title] = classify_single(title, hf_token)
    return results
