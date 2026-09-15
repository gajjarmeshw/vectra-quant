"""
Dynamic Renko Strategy supporting Nifty, BankNifty, FinNifty, and Sensex.
Builds Renko bricks dynamically (or hardcoded 12.5 for NIFTY), applies EMA trend filters,
and emits StrategySignal for Sentinel.
"""
import math
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from collections import deque
from datetime import datetime, time

import pandas as pd

from sentinel.data.candles import Candle
from sentinel.strategies.base import BaseStrategy, StrategyContext, StrategySignal

log = logging.getLogger("strategies.renko_strategy")

RENKO_COLUMNS = ["timestamp", "open", "high", "low", "close", "color"]
MAX_RENKO_BRICKS_PER_BUILD = 5000


@dataclass
class RenkoPositionContext:
    direction: str
    entry_underlying: float
    stop_underlying: float
    rr_armed: bool = False


@dataclass
class RenkoDecision:
    action: str = "HOLD"
    entry_underlying: float = 0.0
    stop_underlying: float = 0.0
    exit_reason: str = ""
    rr_armed: bool = False
    signal_triggered: bool = False


class RenkoSignalEngine:
    def __init__(self):
        self.ce_pullback_ema5 = False
        self.ce_pullback_ema21 = False
        self.pe_pullback_ema5 = False
        self.pe_pullback_ema21 = False
        self.previous_trade_direction = ""

    def update_previous_trade_direction(self, direction: str) -> None:
        direction_txt = str(direction).strip().upper()
        if direction_txt in ("LONG", "SHORT"):
            self.previous_trade_direction = direction_txt

    def reset_reentry_flags(self) -> None:
        self.ce_pullback_ema5 = False
        self.ce_pullback_ema21 = False
        self.pe_pullback_ema5 = False
        self.pe_pullback_ema21 = False

    def _update_reentry_flags(self, c: float, ema5: float, ema21: float, has_active_position: bool) -> None:
        if has_active_position:
            return
        if c < ema5: self.ce_pullback_ema5 = True
        if c < ema21: self.ce_pullback_ema21 = True
        if c > ema5: self.pe_pullback_ema5 = True
        if c > ema21: self.pe_pullback_ema21 = True

    @staticmethod
    def _hold(rr_armed: bool = False) -> RenkoDecision:
        return RenkoDecision(action="HOLD", rr_armed=rr_armed)

    def _evaluate_exit(self, position: RenkoPositionContext, c: float, color: str, ema5: float, ema21: float, ema44: float) -> RenkoDecision:
        rr_armed = bool(position.rr_armed)

        if position.direction == "LONG":
            risk = float(position.entry_underlying) - float(position.stop_underlying)
            rr = (c - float(position.entry_underlying)) / risk if risk > 0 else -999.0
            if rr >= 1.5:
                rr_armed = True

            stop_hit = c <= float(position.stop_underlying)
            trend_exit = c < ema5 and c < ema21 and c < ema44
            rr_exit = rr_armed and color == "red"
            if stop_hit or trend_exit or rr_exit:
                reason = "STOP" if stop_hit else ("EMA_EXIT" if trend_exit else "RR_RED_CANDLE")
                return RenkoDecision(action="EXIT", exit_reason=reason, rr_armed=rr_armed)
            return self._hold(rr_armed=rr_armed)

        if position.direction == "SHORT":
            risk = float(position.stop_underlying) - float(position.entry_underlying)
            rr = (float(position.entry_underlying) - c) / risk if risk > 0 else -999.0
            if rr >= 1.5:
                rr_armed = True

            stop_hit = c >= float(position.stop_underlying)
            trend_exit = c > ema5 and c > ema21 and c > ema44
            rr_exit = rr_armed and color == "green"
            if stop_hit or trend_exit or rr_exit:
                reason = "STOP" if stop_hit else ("EMA_EXIT" if trend_exit else "RR_GREEN_CANDLE")
                return RenkoDecision(action="EXIT", exit_reason=reason, rr_armed=rr_armed)
            return self._hold(rr_armed=rr_armed)

        return self._hold(rr_armed=rr_armed)

    def evaluate_candle(self, renko: pd.DataFrame, position: RenkoPositionContext | None = None) -> RenkoDecision:
        if renko is None or len(renko) < 3:
            return self._hold(rr_armed=bool(position.rr_armed) if position else False)

        cur = renko.iloc[-1]
        prev2 = renko.iloc[-2]

        c = float(cur["close"])
        color = str(cur["color"])
        ema5 = float(cur["ema5"])
        ema21 = float(cur["ema21"])
        ema44 = float(cur["ema44"])

        self._update_reentry_flags(c, ema5, ema21, has_active_position=position is not None)

        if position is not None:
            return self._evaluate_exit(position, c, color, ema5, ema21, ema44)

        ce_fresh = color == "green" and c > ema5 and c > ema21 and c > ema44
        pe_fresh = color == "red" and c < ema5 and c < ema21 and c < ema44

        ce_re_ema5 = self.ce_pullback_ema5 and color == "green" and c > ema5
        ce_re_ema21 = self.ce_pullback_ema21 and color == "green" and c > ema21
        pe_re_ema5 = self.pe_pullback_ema5 and color == "red" and c < ema5
        pe_re_ema21 = self.pe_pullback_ema21 and color == "red" and c < ema21

        long_reentry = ce_re_ema5 or ce_re_ema21
        short_reentry = pe_re_ema5 or pe_re_ema21

        allow_long_reentry = long_reentry and self.previous_trade_direction in ("", "LONG")
        allow_short_reentry = short_reentry and self.previous_trade_direction in ("", "SHORT")

        long_entry_trigger = ce_fresh or allow_long_reentry
        short_entry_trigger = pe_fresh or allow_short_reentry

        if long_entry_trigger:
            stop = float(prev2["low"])
            if stop < c:
                self.ce_pullback_ema5 = False
                self.ce_pullback_ema21 = False
                return RenkoDecision(action="ENTER_LONG", entry_underlying=c, stop_underlying=stop, signal_triggered=True)
            return RenkoDecision(signal_triggered=True)

        if short_entry_trigger:
            stop = float(prev2["high"])
            if stop > c:
                self.pe_pullback_ema5 = False
                self.pe_pullback_ema21 = False
                return RenkoDecision(action="ENTER_SHORT", entry_underlying=c, stop_underlying=stop, signal_triggered=True)
            return RenkoDecision(signal_triggered=True)

        return self._hold()


def build_renko_from_close(df: pd.DataFrame, box_size: float) -> pd.DataFrame:
    closes = df["close"].tolist()
    times = df["timestamp"].tolist()
    if not closes or box_size <= 0:
        return pd.DataFrame()

    last_brick_open = closes[0]
    last_brick_close = closes[0]
    rows = []

    for i in range(1, len(closes)):
        price = closes[i]
        ts = times[i]

        while True:
            prev_high = max(last_brick_open, last_brick_close)
            prev_low = min(last_brick_open, last_brick_close)
            up_trigger = prev_high + box_size
            down_trigger = prev_low - box_size

            if price >= up_trigger:
                if len(rows) >= MAX_RENKO_BRICKS_PER_BUILD:
                    return pd.DataFrame(columns=RENKO_COLUMNS)
                brick_open = prev_high
                brick_close = prev_high + box_size
                rows.append({"timestamp": ts, "open": brick_open, "high": max(brick_open, brick_close), "low": min(brick_open, brick_close), "close": brick_close, "color": "green"})
                last_brick_open = brick_open
                last_brick_close = brick_close
                continue

            if price <= down_trigger:
                if len(rows) >= MAX_RENKO_BRICKS_PER_BUILD:
                    return pd.DataFrame(columns=RENKO_COLUMNS)
                brick_open = prev_low
                brick_close = prev_low - box_size
                rows.append({"timestamp": ts, "open": brick_open, "high": max(brick_open, brick_close), "low": min(brick_open, brick_close), "close": brick_close, "color": "red"})
                last_brick_open = brick_open
                last_brick_close = brick_close
                continue

            break

    return pd.DataFrame(rows)


class DynamicRenkoStrategy(BaseStrategy):
    name = "renko_strategy"
    description = "Multi-Index Renko Strategy with EMA trend alignment and ATR box sizes."
    
    manifest = {
        "symbols": ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"],
        "timeframes": ["1d", "1m"],
        "indicators": [],
    }

    MIN_WARMUP_DAYS = 2

    def __init__(self, params=None):
        super().__init__(params)
        self.daily_candles: deque[Candle] = deque(maxlen=10)
        self.bars_1m: deque[Candle] = deque(maxlen=15000)
        
        self.trade_date: str = ""
        self.instrument_name: str = "NIFTY"
        
        self.engine = RenkoSignalEngine()
        self.active_trade_context: Optional[RenkoPositionContext] = None

    def on_candle(self, symbol: str, timeframe: str, candle: Candle) -> None:
        self.instrument_name = symbol
        
        if timeframe == "1d":
            self.daily_candles.append(candle)
        elif timeframe == "1m":
            self.bars_1m.append(candle)
            self._evaluate_backtest(symbol, candle)

    def _get_atr(self) -> float:
        if len(self.daily_candles) < 2:
            return 0.0
        
        df = pd.DataFrame([
            {"high": c.high, "low": c.low, "close": c.close} 
            for c in self.daily_candles
        ])
        
        high = df['high']
        low = df['low']
        close = df['close'].shift(1)
        tr1 = high - low
        tr2 = (high - close).abs()
        tr3 = (low - close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=min(len(df), 14), min_periods=1).mean()
        return float(atr.iloc[-1])

    def _build_renko(self) -> pd.DataFrame:
        if len(self.bars_1m) < 100:
            return pd.DataFrame()
            
        df = pd.DataFrame([{
            "timestamp": c.timestamp, 
            "open": c.open, "high": c.high, "low": c.low, "close": c.close
        } for c in self.bars_1m])
        
        if self.instrument_name == "NIFTY":
            box_size = 12.5
        else:
            daily_atr = self._get_atr()
            box_size = max(5.0, daily_atr * 0.10)
        
        renko = build_renko_from_close(df, box_size)
        if renko.empty or len(renko) < 44:
            return pd.DataFrame()
            
        closes_series = renko["close"]
        renko["ema5"] = closes_series.ewm(span=5, min_periods=5, adjust=False).mean()
        renko["ema21"] = closes_series.ewm(span=21, min_periods=21, adjust=False).mean()
        renko["ema44"] = closes_series.ewm(span=44, min_periods=44, adjust=False).mean()
        renko["box_size"] = box_size
        return renko

    def _evaluate_backtest(self, symbol: str, current_candle: Candle) -> None:
        # Time filter (09:20 - 14:30 for new entries, but exits can trigger anytime up to 15:15)
        ts_str = current_candle.timestamp
        try:
            now_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").time()
        except ValueError:
            try:
                now_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:00").time()
            except ValueError:
                now_time = datetime.now().time()
                
        is_entry_window = time(9, 20) <= now_time <= time(14, 30)

        renko = self._build_renko()
        if renko.empty or len(renko) < 3:
            return
            
        decision = self.engine.evaluate_candle(renko, self.active_trade_context)
        
        if self.active_trade_context:
            self.active_trade_context.rr_armed = decision.rr_armed
            
        if decision.action == "EXIT" and self.active_trade_context:
            self._emit_signal(
                direction=self.active_trade_context.direction, 
                action="EXIT", 
                entry_price=current_candle.close, 
                thesis=decision.exit_reason
            )
            self.engine.update_previous_trade_direction(self.active_trade_context.direction)
            self.active_trade_context = None

        elif decision.action in ("ENTER_LONG", "ENTER_SHORT") and not self.active_trade_context and is_entry_window:
            direction = "PE" if decision.action == "ENTER_LONG" else "CE"
            pos_dir = "LONG" if decision.action == "ENTER_LONG" else "SHORT"
            
            self.active_trade_context = RenkoPositionContext(
                direction=pos_dir,
                entry_underlying=decision.entry_underlying,
                stop_underlying=decision.stop_underlying
            )
            
            self._emit_signal(
                direction=direction, 
                action="SELL", 
                entry_price=current_candle.close, 
                thesis=f"Renko {pos_dir} Trend"
            )

    def _emit_signal(self, direction: str, action: str, entry_price: float, thesis: str) -> None:
        pass

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        return None
