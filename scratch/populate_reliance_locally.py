import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend')))

from app.screener_data import _fetch_and_commit

# Test updating RELIANCE
print("Updating RELIANCE locally to bypass Cloud Run rate limits...")
_fetch_and_commit([{"symbol": "RELIANCE", "yf_symbol": "RELIANCE.NS", "exchange": "NSE"}], 0.1)
print("Done!")
