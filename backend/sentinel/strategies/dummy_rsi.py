from sentinel.strategies.base import BaseStrategy, StrategyContext, StrategySignal
from sentinel.data.candles import Candle
from typing import Any

class DummyRsiStrategy(BaseStrategy):
    name = "dummy_rsi"
    description = "Test strategy for verifying Data Orchestrator indicator pipeline"
    
    manifest = {
        "symbols": ["RELIANCE", "HDFCBANK"],
        "timeframes": ["1m", "5m"],
        "indicators": ["RSI_14", "MACD_12_26_9"]
    }
    
    def on_tick(self, symbol: str, tick: dict[str, Any]) -> None:
        print(f"[{self.name}] Got tick for {symbol}: {tick}")
        
    def on_candle(self, symbol: str, timeframe: str, candle: Candle) -> None:
        inds = getattr(candle, "indicators", {})
        rsi = inds.get("RSI_14", 0)
        macd = inds.get("MACD_LINE", 0)
        
        print(f"[{self.name}] {symbol} {timeframe} Candle closed at {candle.close} | RSI: {rsi:.2f} | MACD: {macd:.2f}")

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        return None
