"""
Optional build-time step: downloads and caches the FinBERT weights so the
running container never needs outbound internet access to huggingface.co.

Without this, the model downloads lazily on first startup instead (see
app/sentiment.py::_get_pipeline) — that's fine as long as your deploy
platform allows outbound internet from the running container. Use this
script only if:
  - your runtime environment has restricted/no outbound internet, or
  - you want to avoid the one-time download delay on every fresh deploy /
    cold start.

Usage (run during your build step, before the app starts):
    python scripts/prefetch_finbert.py

This respects HF_HOME if set, so the cache location matches whatever the
app will look for at runtime. If your platform's build and runtime
filesystems are different, set HF_HOME to a directory that's actually
included in the final deploy artifact (e.g. baked into a Docker layer).
"""
import sys

MODEL_NAME = "ProsusAI/finbert"


def main():
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except ImportError:
        print("transformers is not installed — add it to requirements.txt first.", file=sys.stderr)
        sys.exit(1)

    print(f"Downloading {MODEL_NAME} weights...")
    AutoTokenizer.from_pretrained(MODEL_NAME)
    AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)
    print("Done. Model is cached locally and will not be re-downloaded at runtime.")


if __name__ == "__main__":
    main()