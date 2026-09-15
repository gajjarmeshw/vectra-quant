import os
import sys
from dotenv import load_dotenv

sys.path.append(os.path.join(os.path.dirname(__file__), "backend"))
load_dotenv()

from vectra_quant.brokers.dhan import DhanAdapter
from vectra_quant.data.orchestrator import DataOrchestrator
from vectra_quant.strategies.dummy_rsi import DummyRsiStrategy

def test_orchestrator():
    # 1. Init Broker
    print("Initializing Broker...")
    dhan = DhanAdapter(
        client_id=os.getenv("DHAN_CLIENT_ID", ""),
        access_token=os.getenv("DHAN_ACCESS_TOKEN", "")
    )
    
    # Force load instrument master to cache it
    master = dhan.get_instruments(force=True)
    
    # 2. Init Orchestrator
    orchestrator = DataOrchestrator(broker=dhan)
    
    # 3. Register Strategy
    strategy = DummyRsiStrategy()
    orchestrator.register_strategy(strategy)
    
    # 4. Aggregate & Warmup
    print("\nStarting Orchestrator Warmup...")
    orchestrator.aggregate_manifests()
    print("Warmup complete. Check logs for indicator outputs.")
    
if __name__ == "__main__":
    test_orchestrator()
