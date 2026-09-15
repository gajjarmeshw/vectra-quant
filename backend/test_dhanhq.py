import sys, os
sys.path.append(os.path.join(os.getcwd(), "vectra_quant"))
from vectra_quant.config import load_settings
from vectra_quant.brokers.dhan import DhanAdapter
s = load_settings()
d = DhanAdapter(s.secrets.dhan_client_id, s.secrets.dhan_access_token)
print(dir(d.dhan))
