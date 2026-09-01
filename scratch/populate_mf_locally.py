import sys
import os
import time
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend')))

from app.mf_data import run_refresh_batch

print("Starting local Mutual Fund database population...")
while True:
    try:
        res = run_refresh_batch(batch_size=50, request_delay_sec=0.2)
        print(f"Batch processed. Cursor at {res.get('cursor', 0)} / {res.get('universe_size', 0)}")
        if res.get("wrapped_full_pass"):
            print("Finished full pass!")
            break
        # Small sleep between batches
        time.sleep(1)
    except Exception as e:
        print(f"Error during batch: {e}")
        time.sleep(5)
