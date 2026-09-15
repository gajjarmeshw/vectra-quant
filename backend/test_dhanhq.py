import sys, os
sys.path.append(os.path.join(os.getcwd(), "sentinel"))
from sentinel.config import load_settings
from sentinel.brokers.dhan import DhanAdapter
s = load_settings()
d = DhanAdapter(s.secrets.dhan_client_id, s.secrets.dhan_access_token)
print(dir(d.dhan))
