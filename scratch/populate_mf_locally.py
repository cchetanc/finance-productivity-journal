import sys
import os
import time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend')))

from app.mf_data import run_refresh_batch

print("Starting local database population for Mutual Funds...")
print("This will process the entire universe of funds (approx 14,000).")
while True:
    try:
        res = run_refresh_batch(batch_size=100)
        print(f"Batch processed. Cursor at {res.get('cursor', 0)} / {res.get('universe_size', 0)}")
        if res.get("wrapped_full_pass"):
            print("Finished full pass!")
            break
        # Small sleep between batches
        time.sleep(0.5)
    except Exception as e:
        print(f"Error during batch: {e}")
        time.sleep(5)
