"""
Financial Sentiment Analysis using ProsusAI/FinBERT, run locally in-process
via HuggingFace `transformers` (no outbound network call).

Previously this hit the public HF "Serverless Inference API"
(api-inference.huggingface.co), which is unauthenticated-unreliable and
explicitly not meant for production use — cold starts, rate limits, and
occasional outright failures were all silently mapped to NEUTRAL, which is
why sentiment looked "stuck". Loading the model locally removes that
external dependency entirely: no token, no rate limit, no cold-start 503s.

The model (~110M params, ~400MB) is loaded once at process start and reused
for every request. CPU inference is fast enough for headline-length text.
"""
import logging
import threading

logger = logging.getLogger(__name__)

MODEL_NAME = "ProsusAI/finbert"

# Label mapping from FinBERT -> our schema
LABEL_MAP = {
    "positive": "BULLISH",
    "negative": "BEARISH",
    "neutral":  "NEUTRAL",
}

_pipeline = None
_pipeline_lock = threading.Lock()
_load_failed = False


def _get_pipeline():
    """
    Lazily loads the FinBERT classification pipeline once per process.
    Thread-safe so concurrent requests during warm-up don't race to load
    the model twice.
    """
    global _pipeline, _load_failed
    if _pipeline is not None or _load_failed:
        return _pipeline

    with _pipeline_lock:
        if _pipeline is not None or _load_failed:
            return _pipeline
        try:
            from transformers import pipeline
            logger.info("Loading FinBERT (%s) locally — first call only, subsequent calls reuse it.", MODEL_NAME)
            _pipeline = pipeline(
                "text-classification",
                model=MODEL_NAME,
                tokenizer=MODEL_NAME,
                truncation=True,
                max_length=512,
            )
            logger.info("FinBERT loaded successfully.")
        except Exception:
            # Log the full traceback loudly instead of swallowing it — a
            # broken local model should be obvious in the logs, not
            # invisible behind a NEUTRAL fallback like the old HTTP path.
            logger.exception("Failed to load local FinBERT model — sentiment will fall back to NEUTRAL until this is fixed.")
            _load_failed = True
            _pipeline = None
    return _pipeline


def classify_single(text: str) -> str:
    """
    Classifies a single financial headline using locally-run FinBERT.
    Returns: 'BULLISH', 'BEARISH', or 'NEUTRAL'
    """
    clf = _get_pipeline()
    if clf is None:
        return "NEUTRAL"

    try:
        result = clf(text[:512])
        # transformers pipeline returns [{"label": ..., "score": ...}]
        best = result[0] if isinstance(result, list) else result
        label = str(best.get("label", "neutral")).lower()
        return LABEL_MAP.get(label, "NEUTRAL")
    except Exception:
        logger.exception("FinBERT inference error for headline: %r", text[:60])
        return "NEUTRAL"


def batch_classify_headlines(headlines: list) -> dict:
    """
    Classifies a list of news headlines using locally-run FinBERT.
    Returns dict mapping headline -> sentiment.

    Uses the pipeline's native batching (single forward pass over the
    batch) instead of one-at-a-time HTTP calls — faster and removes the
    old sequential-because-of-rate-limits constraint entirely.
    """
    if not headlines:
        return {}

    clf = _get_pipeline()
    if clf is None:
        return {title: "NEUTRAL" for title in headlines}

    try:
        truncated = [h[:512] for h in headlines]
        raw_results = clf(truncated)
        results = {}
        for title, res in zip(headlines, raw_results):
            label = str(res.get("label", "neutral")).lower()
            results[title] = LABEL_MAP.get(label, "NEUTRAL")
        return results
    except Exception:
        logger.exception("FinBERT batch inference error — falling back to per-item classification.")
        return {title: classify_single(title) for title in headlines}