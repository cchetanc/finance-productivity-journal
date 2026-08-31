FROM python:3.11-slim

WORKDIR /app

# Install system dependencies if required by torch or others
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Prefetch FinBERT weights to cache them in the Docker image
RUN python scripts/prefetch_finbert.py

ENV PORT=8080
CMD uvicorn run:app --host 0.0.0.0 --port ${PORT}
