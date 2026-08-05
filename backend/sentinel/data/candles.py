"""Candle builder: 1-minute and 5-minute bars, ATR, Opening Range, PDH/PDL/PDC."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sentinel.db import Tick1m, session
from sentinel.logging_setup import get

log = get("data.candles")


@dataclass
class Candle:
    symbol: str
    timestamp: str  # YYYY-MM-DD HH:MM:00
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class OpeningRange:
    high: float
    low: float


@dataclass
class PrevDayRange:
    pdh: float
    pdl: float
    pdc: float


class CandleBuilder:
    def __init__(self, maxlen: int = 2000):
        self.maxlen = maxlen
        # symbol -> deque of 1m Candle
        self._bars_1m: dict[str, deque[Candle]] = {}
        # symbol -> current forming bar state
        self._current: dict[str, dict[str, Any]] = {}
        self._dirty_bars: list[Candle] = []

    def on_tick(self, symbol: str, price: float, timestamp: datetime | None = None) -> None:
        if price <= 0:
            return
        ts = timestamp or datetime.now()
        minute_str = ts.strftime("%Y-%m-%d %H:%M:00")

        curr = self._current.get(symbol)
        if curr is None or curr["minute"] != minute_str:
            if curr is not None:
                # finalize previous bar
                c = Candle(
                    symbol=symbol,
                    timestamp=curr["minute"],
                    open=curr["open"],
                    high=curr["high"],
                    low=curr["low"],
                    close=curr["close"],
                )
                self._add_bar(c)

            # start new bar
            self._current[symbol] = {
                "minute": minute_str,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
            }
        else:
            curr["high"] = max(curr["high"], price)
            curr["low"] = min(curr["low"], price)
            curr["close"] = price

    def _add_bar(self, candle: Candle) -> None:
        sym = candle.symbol
        if sym not in self._bars_1m:
            self._bars_1m[sym] = deque(maxlen=self.maxlen)
        self._bars_1m[sym].append(candle)
        self._dirty_bars.append(candle)

    def flush(self) -> None:
        if not self._dirty_bars:
            return
        try:
            with session() as s:
                for c in self._dirty_bars:
                    dt = datetime.strptime(c.timestamp, "%Y-%m-%d %H:%M:%00").replace(tzinfo=UTC)
                    s.add(Tick1m(
                        symbol=c.symbol,
                        ts=dt,
                        open=c.open,
                        high=c.high,
                        low=c.low,
                        close=c.close,
                        volume=c.volume,
                    ))
            self._dirty_bars.clear()
        except Exception as e:
            log.warning("Failed to flush candles to DB: %s", e)

    def backfill(self, symbol: str, candles: list[dict[str, Any]]) -> int:
        if symbol not in self._bars_1m:
            self._bars_1m[symbol] = deque(maxlen=self.maxlen)
        count = 0
        for c in candles:
            bar = Candle(
                symbol=symbol,
                timestamp=str(c.get("timestamp", "")),
                open=float(c.get("open", 0)),
                high=float(c.get("high", 0)),
                low=float(c.get("low", 0)),
                close=float(c.get("close", 0)),
                volume=float(c.get("volume", 0)),
            )
            self._bars_1m[symbol].append(bar)
            count += 1
        return count

    def last_close(self, symbol: str) -> float | None:
        curr = self._current.get(symbol)
        if curr is not None:
            return curr["close"]
        bars = self._bars_1m.get(symbol)
        if bars:
            return bars[-1].close
        return None

    def series(self, symbol: str, timeframe: str = "1m", limit: int = 200) -> list[dict[str, Any]]:
        bars = list(self._bars_1m.get(symbol, []))
        if not bars:
            return []

        if timeframe == "1m":
            selected = bars[-limit:]
            return [
                {
                    "timestamp": c.timestamp,
                    "open": c.open,
                    "high": c.high,
                    "low": c.low,
                    "close": c.close,
                    "volume": c.volume,
                }
                for c in selected
            ]

        if timeframe == "5m":
            # aggregate into 5m
            bars_5m: list[dict[str, Any]] = []
            chunk: list[Candle] = []
            for c in bars:
                chunk.append(c)
                if len(chunk) == 5:
                    bars_5m.append({
                        "timestamp": chunk[0].timestamp,
                        "open": chunk[0].open,
                        "high": max(b.high for b in chunk),
                        "low": min(b.low for b in chunk),
                        "close": chunk[-1].close,
                        "volume": sum(b.volume for b in chunk),
                    })
                    chunk = []
            if chunk:
                bars_5m.append({
                    "timestamp": chunk[0].timestamp,
                    "open": chunk[0].open,
                    "high": max(b.high for b in chunk),
                    "low": min(b.low for b in chunk),
                    "close": chunk[-1].close,
                    "volume": sum(b.volume for b in chunk),
                })
            return bars_5m[-limit:]

        return []

    def atr(self, symbol: str, period: int = 14) -> float:
        bars = list(self._bars_1m.get(symbol, []))
        if len(bars) < period + 1:
            return 0.0
        tr_list: list[float] = []
        for i in range(1, len(bars)):
            h = bars[i].high
            l = bars[i].low
            pc = bars[i - 1].close
            tr = max(h - l, abs(h - pc), abs(l - pc))
            tr_list.append(tr)
        if not tr_list:
            return 0.0
        return sum(tr_list[-period:]) / min(len(tr_list), period)

    def opening_range(self, symbol: str) -> OpeningRange | None:
        bars = [
            b for b in self._bars_1m.get(symbol, [])
            if "09:15:00" <= b.timestamp.split(" ")[-1] <= "09:30:00"
        ]
        if not bars:
            return None
        return OpeningRange(
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
        )

    def prev_day_range(self, symbol: str) -> PrevDayRange | None:
        bars = list(self._bars_1m.get(symbol, []))
        if not bars:
            return None
        today_date = datetime.now().strftime("%Y-%m-%d")
        prev_bars = [b for b in bars if not b.timestamp.startswith(today_date)]
        if not prev_bars:
            return None
        return PrevDayRange(
            pdh=max(b.high for b in prev_bars),
            pdl=min(b.low for b in prev_bars),
            pdc=prev_bars[-1].close,
        )

    def prev_day_close(self, symbol: str) -> float | None:
        pdr = self.prev_day_range(symbol)
        return pdr.pdc if pdr else None
