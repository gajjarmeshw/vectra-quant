import sys, os
from datetime import datetime, timedelta
sys.path.append(os.path.join(os.getcwd(), "vectra_quant"))
from vectra_quant.config import load_settings
from vectra_quant.brokers.dhan import DhanAdapter
s = load_settings()
d = DhanAdapter(s.secrets.dhan_client_id, s.secrets.dhan_access_token)

print("Testing historical_minute_data...")
try:
    resp = d.dhan.historical_minute_data(
        security_id="13",
        exchange_segment="IDX_I",
        instrument_type="INDEX",
        from_date=(datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"),
        to_date=(datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d"),
    )
    print(resp)
except Exception as e:
    print("Error:", e)
