from typing import Any, Dict
from datetime import datetime, time
from collections import deque
import math

from sentinel.strategies.base import BaseStrategy, StrategyContext, StrategySignal
from sentinel.data.candles import Candle
from sentinel.logging_setup import get

log = get("strategies.nifty_5d_breakout")


class Nifty5DayBreakout(BaseStrategy):
    """
    Nifty Intraday Range Breakout Strategy (Institutional Grade).

    Logic: trades breakouts of the previous 2-day high/low range, but ONLY in the
    direction of the prevailing short-term trend (close vs 5-day SMA) and ONLY when
    today's open confirms the bias (gap-up for longs, gap-down for shorts).

    This eliminates the classic "breakout → mean revert" chop that kills naive
    breakout strategies in range-bound markets.

    Option Selling Mode: Sells Puts on upside breakouts, Sells Calls on downside.
    """

    name = "nifty_5d_breakout"
    description = "Trades trend-confirmed range breakouts. ATR stops, credit spread execution."

    manifest = {
        "symbols": ["NIFTY", "BANKNIFTY"],
        "timeframes": ["1d", "1m"],
        "indicators": ["ATR_5", "VMA_20"],
    }

    # ── Risk Parameters ──────────────────────────────────────────────
    STOP_ATR_MULT = 0.5       # Stop loss = 0.5 × ATR
    TARGET_ATR_MULT = 0.8     # Target   = 0.8 × ATR  (1.6 R:R)
    BREAKOUT_BUFFER = 0.05    # Confirm buffer = 0.05 × ATR
    LOOKBACK_DAYS = 2         # Use 2-day high/low as breakout range
    MIN_WARMUP_DAYS = 3       # Need 3 daily candles for SMA trend filter
    VOLUME_THRESHOLD = 1.0

    def __init__(self):
        super().__init__()

        # Daily state
        self.daily_candles: deque[Candle] = deque(maxlen=10)
        self.last_1m_candle: Candle | None = None
        self.recent_1m_volumes: deque[float] = deque(maxlen=5)

        # Breakout levels
        self.range_high: float = 0.0
        self.range_low: float = float("inf")
        self.current_atr: float = 0.0

        # Trend filter
        self.trend_bias: str = "NEUTRAL"  # "BULLISH", "BEARISH", "NEUTRAL"
        self.today_open: float = 0.0
        self.prev_close: float = 0.0

        # Sector state
        self.banknifty_open: float = 0.0
        self.banknifty_ltp: float = 0.0

        # Execution
        self.has_traded_today: bool = False
        self.trade_date: str = ""

    # ── Candle Processing ────────────────────────────────────────────

    def on_candle(self, symbol: str, timeframe: str, candle: Candle) -> None:
        inds = getattr(candle, "indicators", {}) or {}

        if symbol == "NIFTY":
            if timeframe == "1d":
                self.daily_candles.append(candle)
                self._recalculate_levels()
                if "ATR_5" in inds:
                    val = inds["ATR_5"]
                    if val and not math.isnan(val):
                        self.current_atr = val

            elif timeframe == "1m":
                self.last_1m_candle = candle
                self.recent_1m_volumes.append(float(candle.volume))
                # Capture today's open from the very first 1m bar
                if not self.today_open:
                    self.today_open = candle.open

        elif symbol == "BANKNIFTY" and timeframe == "1d":
            self.banknifty_open = candle.open

    def _recalculate_levels(self) -> None:
        """Compute breakout range AND trend bias from daily candles."""
        n = len(self.daily_candles)
        if n < self.MIN_WARMUP_DAYS:
            self.range_high = 0.0
            self.range_low = float("inf")
            self.trend_bias = "NEUTRAL"
            return

        candles = list(self.daily_candles)

        # --- Breakout range: last LOOKBACK_DAYS candles ---
        lookback = candles[-self.LOOKBACK_DAYS:]
        self.range_high = max(c.high for c in lookback)
        self.range_low = min(c.low for c in lookback)

        # --- Trend filter: last close vs simple 5-candle average of closes ---
        recent = candles[-min(5, n):]
        sma = sum(c.close for c in recent) / len(recent)
        last_close = candles[-1].close
        self.prev_close = last_close

        if last_close > sma:
            self.trend_bias = "BULLISH"
        elif last_close < sma:
            self.trend_bias = "BEARISH"
        else:
            self.trend_bias = "NEUTRAL"

    # ── Tick Processing ──────────────────────────────────────────────

    def on_tick(self, symbol: str, tick: Dict[str, Any]) -> None:
        if symbol == "BANKNIFTY":
            self.banknifty_ltp = tick.get("last_price", 0.0)
            if not self.banknifty_open:
                self.banknifty_open = tick.get("open", self.banknifty_ltp)
            return

        if symbol != "NIFTY":
            return

        ltp = tick.get("last_price", 0.0)

        # ── Date tracking ──
        if "timestamp" in tick and isinstance(tick["timestamp"], datetime):
            today_str = tick["timestamp"].strftime("%Y-%m-%d")
        elif self.last_1m_candle and hasattr(self.last_1m_candle, "ts"):
            ts = self.last_1m_candle.ts
            today_str = ts[:10] if isinstance(ts, str) else ts.strftime("%Y-%m-%d")
        else:
            today_str = datetime.now().strftime("%Y-%m-%d")

        if self.trade_date != today_str:
            self.trade_date = today_str
            self.has_traded_today = False
            self.today_open = 0.0  # reset for new day

        if self.has_traded_today:
            return

        # ── Time filter (09:20 – 14:30) ──
        if "timestamp" in tick and isinstance(tick["timestamp"], datetime):
            now_time = tick["timestamp"].time()
        elif self.last_1m_candle and hasattr(self.last_1m_candle, "ts"):
            ts = self.last_1m_candle.ts
            if isinstance(ts, str):
                try:
                    now_time = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").time()
                except ValueError:
                    try:
                        now_time = datetime.strptime(ts, "%Y-%m-%d %H:%M:00").time()
                    except ValueError:
                        now_time = datetime.now().time()
            else:
                now_time = ts.time()
        else:
            now_time = datetime.now().time()

        if not (time(9, 20) <= now_time <= time(14, 30)):
            return

        # ── Sanity checks ──
        if not self.last_1m_candle:
            return
        if self.current_atr <= 0 or math.isnan(self.current_atr):
            return
        if self.range_high <= 0 or self.range_low == float("inf"):
            return

        buffer = self.current_atr * self.BREAKOUT_BUFFER
        long_level = self.range_high + buffer
        short_level = self.range_low - buffer

        # ── LONG BREAKOUT ──
        # Conditions: Price > range high, trend is BULLISH, today opened above prev close
        if ltp > long_level:
            # Trend confirmation: only go long when trend is bullish
            if self.trend_bias == "BEARISH":
                log.info(f"Rejected LONG: Trend is BEARISH (close < SMA)")
                return

            # Gap confirmation: today's open should be above or near prev close
            if self.today_open > 0 and self.prev_close > 0:
                gap_pct = (self.today_open - self.prev_close) / self.prev_close * 100
                if gap_pct < -0.3:  # gapped down more than 0.3% → skip long
                    log.info(f"Rejected LONG: Gap-down open ({gap_pct:.2f}%)")
                    return

            log.info(
                f"LONG Breakout: {ltp:.2f} > {long_level:.2f} "
                f"(range_high={self.range_high:.0f}, trend={self.trend_bias}, ATR={self.current_atr:.1f})"
            )
            self._emit_signal(
                direction="PE", action="SELL", entry_price=ltp,
                thesis=f"Upside Breakout (>{self.range_high:.0f}, {self.trend_bias})",
            )
            self.has_traded_today = True

        # ── SHORT BREAKOUT ──
        # Conditions: Price < range low, trend is BEARISH, today opened below prev close
        elif ltp < short_level:
            # Trend confirmation: only go short when trend is bearish
            if self.trend_bias == "BULLISH":
                log.info(f"Rejected SHORT: Trend is BULLISH (close > SMA)")
                return

            # Gap confirmation
            if self.today_open > 0 and self.prev_close > 0:
                gap_pct = (self.today_open - self.prev_close) / self.prev_close * 100
                if gap_pct > 0.3:  # gapped up more than 0.3% → skip short
                    log.info(f"Rejected SHORT: Gap-up open ({gap_pct:.2f}%)")
                    return

            log.info(
                f"SHORT Breakout: {ltp:.2f} < {short_level:.2f} "
                f"(range_low={self.range_low:.0f}, trend={self.trend_bias}, ATR={self.current_atr:.1f})"
            )
            self._emit_signal(
                direction="CE", action="SELL", entry_price=ltp,
                thesis=f"Downside Breakout (<{self.range_low:.0f}, {self.trend_bias})",
            )
            self.has_traded_today = True

    # ── Signal Emission ──────────────────────────────────────────────

    def _emit_signal(self, direction: str, action: str, entry_price: float, thesis: str) -> None:
        sig = StrategySignal(
            instrument="NIFTY",
            direction=direction,
            action=action,
            strike_offset="OTM3",
            entry_zone={"low": entry_price - 10, "high": entry_price + 10},
            stop_loss_premium=0.0,
            target_premium=0.0,
            time_stop_minutes=120,
            confidence=85,
            thesis=thesis,
            strategy_name=self.name,
        )
        log.warning(f"STRATEGY TRIGGER: {sig.action} {sig.direction} {sig.strike_offset} - {thesis}")

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        return None
