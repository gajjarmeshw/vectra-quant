import os
from dotenv import load_dotenv
load_dotenv(".env")
from sentinel.brokers.dhan import DhanAdapter
broker = DhanAdapter(os.environ["DHAN_CLIENT_ID"], os.environ["DHAN_ACCESS_TOKEN"])
print("VIX Quote:", broker.get_quote(["INDIAVIX"]))
print("VIX LTP:", broker.get_ltp("INDIAVIX"))
