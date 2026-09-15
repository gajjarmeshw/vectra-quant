import pandas as pd
from typing import List, Dict, Any
from vectra_quant.data.candles import Candle

def compute_indicators(candles: List[Candle], indicators: List[str]) -> List[Candle]:
    """
    Computes requested technical indicators and attaches them to the candles' `indicators` dict.
    Example indicators: ["RSI_14", "MACD_12_26_9", "EMA_20"]
    """
    if not candles or not indicators:
        return candles

    df = pd.DataFrame([{
        "ts": getattr(c, "ts", getattr(c, "timestamp", None)),
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": c.volume
    } for c in candles])

    for ind in indicators:
        parts = ind.upper().split('_')
        name = parts[0]
        
        if name == "RSI":
            period = int(parts[1]) if len(parts) > 1 else 14
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
            rs = gain / loss
            df[ind] = 100 - (100 / (1 + rs))

        elif name == "EMA":
            period = int(parts[1]) if len(parts) > 1 else 20
            df[ind] = df['close'].ewm(span=period, adjust=False).mean()
            
        elif name == "SMA":
            period = int(parts[1]) if len(parts) > 1 else 20
            df[ind] = df['close'].rolling(window=period).mean()

        elif name == "MACD":
            fast = int(parts[1]) if len(parts) > 1 else 12
            slow = int(parts[2]) if len(parts) > 2 else 26
            signal = int(parts[3]) if len(parts) > 3 else 9
            ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
            ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
            macd = ema_fast - ema_slow
            df[f"MACD_LINE"] = macd
            df[f"MACD_SIGNAL"] = macd.ewm(span=signal, adjust=False).mean()
            df[f"MACD_HIST"] = df[f"MACD_LINE"] - df[f"MACD_SIGNAL"]

        elif name == "ATR":
            period = int(parts[1]) if len(parts) > 1 else 14
            high_low = df['high'] - df['low']
            high_close = (df['high'] - df['close'].shift()).abs()
            low_close = (df['low'] - df['close'].shift()).abs()
            ranges = pd.concat([high_low, high_close, low_close], axis=1)
            true_range = ranges.max(axis=1)
            df[ind] = true_range.rolling(window=period).mean()

        elif name == "VMA":
            period = int(parts[1]) if len(parts) > 1 else 20
            df[ind] = df['volume'].rolling(window=period).mean()

    # Attach computed values back to candles
    for i, row in df.iterrows():
        # Ensure candle has an indicators dict
        if not hasattr(candles[i], "indicators") or candles[i].indicators is None:
            candles[i].indicators = {}
        
        for ind in indicators:
            if ind.startswith("MACD"):
                candles[i].indicators["MACD_LINE"] = row.get("MACD_LINE")
                candles[i].indicators["MACD_SIGNAL"] = row.get("MACD_SIGNAL")
                candles[i].indicators["MACD_HIST"] = row.get("MACD_HIST")
            else:
                candles[i].indicators[ind] = row.get(ind)

    return candles
