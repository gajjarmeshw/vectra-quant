import os
import sys
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

sys.path.append(os.path.join(os.path.dirname(__file__), "backend"))
load_dotenv()

from vectra_quant.brokers.dhan import DhanAdapter
from vectra_quant.data.orchestrator import DataOrchestrator
from vectra_quant.strategies.nifty_5d_breakout import Nifty5DayBreakout

def run_event_backtest():
    print("Initializing Dhan Broker for Historical Data...")
    dhan = DhanAdapter(
        client_id=os.getenv("DHAN_CLIENT_ID", ""),
        access_token=os.getenv("DHAN_ACCESS_TOKEN", "")
    )
    
    orchestrator = DataOrchestrator(broker=dhan)
    strategy = Nifty5DayBreakout()
    
    # We patch the strategy's emit_signal just to print the trades during backtest
    def mock_emit(direction, action, entry_price, thesis):
        print(f"💰 TRADE FIRED! [{action} {direction}] @ {entry_price} | Reason: {thesis}")
        # Stop trading for the day
        strategy.has_traded_today = True
        
    strategy._emit_signal = mock_emit
    
    orchestrator.register_strategy(strategy)
    
    # 1. Provide the daily candles for the 5-day lookback
    print("Fetching Daily Candles for 5-Day Warmup...")
    daily_candles = dhan.get_candles("NIFTY", tf="1d", span=10)
    for c in daily_candles[-6:-1]: # Feed the last 5 completed days
        strategy.on_candle("NIFTY", "1d", c)
        
    print(f"5-Day Range Set: High {strategy.five_day_high}, Low {strategy.five_day_low}")
    print(f"ATR Set: {strategy.current_atr}")
    
    # 2. Fetch the "Live" 1-minute candles for today
    print("Fetching Intraday 1m Data for Execution Simulation...")
    intraday_candles = dhan.get_candles("NIFTY", tf="1m", span=375)
    
    print("\nStarting Simulation...")
    for candle in intraday_candles:
        # Simulate the close of a 1m candle (which sets the Volume MA filter)
        strategy.on_candle("NIFTY", "1m", candle)
        
        # Simulate sub-second tick stream passing through the 1m bar
        # We simulate the tick hitting the high and the low of the candle
        ticks = [
            {"last_price": candle.open},
            {"last_price": candle.low},
            {"last_price": candle.high},
            {"last_price": candle.close}
        ]
        
        for tick in ticks:
            # Provide dummy sector breadth (forces the trade to pass)
            strategy.banknifty_open = 100
            strategy.banknifty_ltp = 105 
            
            # Fire the simulated tick
            strategy.on_tick("NIFTY", tick)

if __name__ == "__main__":
    run_event_backtest()
