"""
Fetch NSE Participant-wise Open Interest (FII lean).
Calculates the 5-day drift of FII Net Index Futures and saves to SystemState.
"""
import csv
import json
import logging
from datetime import datetime, timedelta
import httpx
from io import StringIO
import os
import sys

# Ensure backend is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from vectra_quant.db import state_get, state_set

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fetch_nse_oi")

def fetch_participant_oi_for_date(dt: datetime) -> int | None:
    date_str = dt.strftime("%d%m%Y")
    url = f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{date_str}.csv"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    
    try:
        with httpx.Client(headers=headers, timeout=10) as client:
            client.get("https://www.nseindia.com") # get cookies
            resp = client.get(url)
            
            if resp.status_code == 200:
                content = resp.text
                reader = csv.DictReader(StringIO(content))
                for row in reader:
                    # Look for FII row
                    client_type = row.get("Client Type", "").strip().upper()
                    if client_type == "FII":
                        future_index_long = int(row.get("Future Index Long", 0))
                        future_index_short = int(row.get("Future Index Short", 0))
                        net = future_index_long - future_index_short
                        return net
        return None
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return None

def main():
    log.info("Fetching FII Net Index Futures for 5-day drift...")
    
    try:
        existing_raw = state_get("FII_NET_INDEX_FUTURES", "[]")
        drift_data = json.loads(existing_raw)
    except json.JSONDecodeError:
        drift_data = []

    # Get last 5 valid trading days (naive approach, skips weekends)
    collected = []
    days_back = 0
    dt = datetime.now()
    
    while len(collected) < 5 and days_back < 15:
        check_dt = dt - timedelta(days=days_back)
        if check_dt.weekday() < 5: # Mon-Fri
            net = fetch_participant_oi_for_date(check_dt)
            if net is not None:
                collected.append({"date": check_dt.strftime("%Y-%m-%d"), "net": net})
        days_back += 1
        
    if collected:
        # Sort by date ascending (oldest first, newest last)
        collected.sort(key=lambda x: x["date"])
        state_set("FII_NET_INDEX_FUTURES", json.dumps(collected))
        log.info(f"Updated FII 5-day drift: {collected}")
    else:
        log.warning("Could not fetch any recent FII data.")

if __name__ == "__main__":
    main()
