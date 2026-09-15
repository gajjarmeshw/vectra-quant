import threading
from typing import Dict, List, Set, Any
from collections import defaultdict
from vectra_quant.logging_setup import get
from vectra_quant.strategies.base import BaseStrategy
from vectra_quant.brokers.base import BrokerAdapter, Candle
from vectra_quant.data.indicators import compute_indicators

log = get("data.orchestrator")

class DataOrchestrator:
    """
    Event-driven Data Bus for Dynamic Strategy Execution.
    Reads strategy manifests and provides them with real-time ticks and compiled candles.
    """
    
    def __init__(self, broker: BrokerAdapter):
        self.broker = broker
        self.strategies: List[BaseStrategy] = []
        
        # Aggregated requirements
        self.required_symbols: Set[str] = set()
        
        # Mappings for routing
        # symbol -> list of strategies that want it
        self.tick_routes: Dict[str, List[BaseStrategy]] = defaultdict(list)
        # (symbol, timeframe) -> list of strategies
        self.candle_routes: Dict[tuple[str, str], List[BaseStrategy]] = defaultdict(list)
        
        # State
        self._lock = threading.RLock()
        self._running = False
        
    def register_strategy(self, strategy: BaseStrategy) -> None:
        """Register a strategy and parse its manifest."""
        with self._lock:
            if strategy in self.strategies:
                return
            
            self.strategies.append(strategy)
            manifest = getattr(strategy, "manifest", {})
            symbols = manifest.get("symbols", [])
            timeframes = manifest.get("timeframes", [])
            
            log.info("Registered strategy %s needing symbols: %s, timeframes: %s", 
                     strategy.name, symbols, timeframes)
            
            for sym in symbols:
                self.required_symbols.add(sym)
                self.tick_routes[sym].append(strategy)
                
                for tf in timeframes:
                    self.candle_routes[(sym, tf)].append(strategy)
                    
    def aggregate_manifests(self) -> None:
        """Called once before startup to prepare subscriptions."""
        with self._lock:
            log.info("DataOrchestrator aggregated %d unique symbols across %d strategies.", 
                     len(self.required_symbols), len(self.strategies))
            
            # Here we would initialize historical candles for the required timeframes
            # so that indicators have enough lookback window.
            self._warmup_historical_data()
            
    def _warmup_historical_data(self) -> None:
        """Pre-fetch historical candles so indicators like RSI have data."""
        # This will be called before starting the live feed
        for (sym, tf), strats in self.candle_routes.items():
            try:
                # We fetch enough span for common indicators (e.g., 200 bars)
                candles = self.broker.get_candles(sym, tf=tf, span=200)
                
                # Check what indicators these strategies need
                needed_indicators = set()
                for st in strats:
                    needed_indicators.update(getattr(st, "manifest", {}).get("indicators", []))
                
                if needed_indicators:
                    candles = compute_indicators(candles, list(needed_indicators))
                    
                # Dispatch ALL historical candles so strategies can build their rolling state (e.g. 5-Day Breakout needs 5 days)
                if candles:
                    for st in strats:
                        for c in candles:
                            st.on_candle(sym, tf, c)
                        
            except Exception as e:
                log.error("Failed to warmup data for %s %s: %s", sym, tf, e)

    def dispatch_tick(self, symbol: str, tick: dict[str, Any]) -> None:
        """Route an incoming WebSocket tick to interested strategies."""
        routes = self.tick_routes.get(symbol, [])
        for st in routes:
            try:
                st.on_tick(symbol, tick)
            except Exception as e:
                log.error("Strategy %s failed on_tick: %s", st.name, e)
                
    def dispatch_candle(self, symbol: str, timeframe: str, candle: Candle) -> None:
        """Route a closed candle (possibly with indicators) to interested strategies."""
        routes = self.candle_routes.get((symbol, timeframe), [])
        
        # Keep a rolling cache for indicators (using a simple dict of lists)
        if not hasattr(self, "_live_candle_cache"):
            self._live_candle_cache = defaultdict(lambda: [])
            
        cache_key = (symbol, timeframe)
        self._live_candle_cache[cache_key].append(candle)
        
        # Keep last 100 for indicators
        if len(self._live_candle_cache[cache_key]) > 100:
            self._live_candle_cache[cache_key].pop(0)
            
        # If any strategy needs indicators, compute them
        needed_indicators = set()
        for st in routes:
            needed_indicators.update(getattr(st, "manifest", {}).get("indicators", []))
            
        if needed_indicators:
            compute_indicators(self._live_candle_cache[cache_key], list(needed_indicators))
            # The indicators are now attached to the candle object by reference
        
        for st in routes:
            try:
                st.on_candle(symbol, timeframe, candle)
            except Exception as e:
                log.error("Strategy %s failed on_candle: %s", st.name, e)
