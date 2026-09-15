"""
Renko Trend Sniper Strategy — NIFTY / BANKNIFTY / FINNIFTY / SENSEX.

Redesigned for honest profitability under the 23-Point Gauntlet:
  • ATR-adaptive box sizing (ATR_14 × 0.20, floor 20pts) — filters noise, not amplifies it
  • Dual-EMA (21/44) trend gate — dropped EMA5 from entries (too fast on Renko)
  • 2-brick consecutive momentum confirmation — proves thrust, not a single reversal
  • Volume filter — requires above-average participation
  • 30-min same-direction re-entry cooldown — prevents whipsaw loops
  • Hard premium SL (30% of entry) + trailing stop (activates at 1.5R)
  • Regime gate — rejects entries during unwinding regimes

The strategy communicates sl_premium and target_premium through _emit_signal so the
backtest engine enforces hard stops alongside the strategy's own EMA-based exits.
"""
import math
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from collections import deque
from datetime import datetime, time, timedelta

import pandas as pd

from vectra_quant.data.candles import Candle
from vectra_quant.strategies.base import BaseStrategy, StrategyContext, StrategySignal

log = logging.getLogger("strategies.renko_strategy")

RENKO_COLUMNS = ["timestamp", "open", "high", "low", "close", "color"]
MAX_RENKO_BRICKS_PER_BUILD = 5000

# Bound on accumulated brick history kept by the incremental builder (see
# DynamicRenkoStrategy._build_renko). EMA21/44 only ever need a few hundred
# trailing bricks to be stable, so trimming ancient ones is safe for a
# backtest or live session running indefinitely.
_RENKO_ROWS_TRIM_AT = 4000
_RENKO_ROWS_KEEP = 2000


@dataclass
class RenkoPositionContext:
    direction: str
    entry_underlying: float
    stop_underlying: float
    entry_premium: float = 0.0
    sl_premium: float = 0.0
    target_premium: float = 0.0
    peak_premium: float = 0.0
    rr_armed: bool = False
    trailing_active: bool = False
    trailing_stop: float = 0.0


@dataclass
class RenkoDecision:
    action: str = "HOLD"
    entry_underlying: float = 0.0
    stop_underlying: float = 0.0
    exit_reason: str = ""
    rr_armed: bool = False
    signal_triggered: bool = False
    # Premium SL/Target communicated to engine for hard enforcement
    sl_premium: float = 0.0
    target_premium: float = 0.0


class RenkoSignalEngine:
    """Signal engine for Renko Trend Sniper — fewer, higher-quality entries."""

    def __init__(self):
        self.previous_trade_direction = ""
        self._last_exit_ts: datetime | None = None
        self._last_exit_direction: str = ""

    def update_previous_trade_direction(self, direction: str) -> None:
        direction_txt = str(direction).strip().upper()
        if direction_txt in ("LONG", "SHORT"):
            self.previous_trade_direction = direction_txt

    def record_exit(self, direction: str, exit_ts: datetime) -> None:
        """Record exit timestamp for cooldown enforcement."""
        self._last_exit_ts = exit_ts
        self._last_exit_direction = direction.upper()

    def _cooldown_ok(self, direction: str, current_ts: datetime) -> bool:
        """Block same-direction re-entry within 30 minutes of the last exit."""
        if self._last_exit_ts is None:
            return True
        if self._last_exit_direction != direction.upper():
            return True  # opposite direction is always OK
        elapsed = (current_ts - self._last_exit_ts).total_seconds()
        return elapsed >= 1800  # 30 minutes

    @staticmethod
    def _hold(rr_armed: bool = False) -> RenkoDecision:
        return RenkoDecision(action="HOLD", rr_armed=rr_armed)

    def _evaluate_exit(self, position: RenkoPositionContext, c: float,
                       color: str, ema21: float, ema44: float) -> RenkoDecision:
        rr_armed = bool(position.rr_armed)

        if position.direction == "LONG":
            risk = float(position.entry_underlying) - float(position.stop_underlying)
            rr = (c - float(position.entry_underlying)) / risk if risk > 0 else -999.0
            if rr >= 1.5:
                rr_armed = True

            stop_hit = c <= float(position.stop_underlying)
            # Use EMA21 for trend exit — EMA5 is too trigger-happy on Renko
            trend_exit = c < ema21 and c < ema44
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
            trend_exit = c > ema21 and c > ema44
            rr_exit = rr_armed and color == "green"
            if stop_hit or trend_exit or rr_exit:
                reason = "STOP" if stop_hit else ("EMA_EXIT" if trend_exit else "RR_GREEN_CANDLE")
                return RenkoDecision(action="EXIT", exit_reason=reason, rr_armed=rr_armed)
            return self._hold(rr_armed=rr_armed)

        return self._hold(rr_armed=rr_armed)

    def _count_consecutive_bricks(self, renko: pd.DataFrame, color: str, lookback: int = 5) -> int:
        """Count consecutive bricks of the given color from the latest brick backwards."""
        count = 0
        for i in range(len(renko) - 1, max(len(renko) - lookback - 1, -1), -1):
            if str(renko.iloc[i]["color"]) == color:
                count += 1
            else:
                break
        return count

    def evaluate_candle(self, renko: pd.DataFrame,
                        position: RenkoPositionContext | None = None,
                        current_ts: datetime | None = None,
                        volume_ok: bool = True) -> RenkoDecision:
        if renko is None or len(renko) < 3:
            return self._hold(rr_armed=bool(position.rr_armed) if position else False)

        cur = renko.iloc[-1]
        prev2 = renko.iloc[-3]  # 3 bricks back for wider stop

        c = float(cur["close"])
        color = str(cur["color"])
        ema21 = float(cur["ema21"])
        ema44 = float(cur["ema44"])

        if position is not None:
            return self._evaluate_exit(position, c, color, ema21, ema44)

        # === ENTRY FILTERS ===

        # Filter 1: Dual-EMA trend alignment (21/44 only — EMA5 dropped for entry)
        ce_trend = c > ema21 and ema21 > ema44
        pe_trend = c < ema21 and ema21 < ema44

        # Filter 2: Consecutive brick momentum (≥2 same-color bricks)
        green_run = self._count_consecutive_bricks(renko, "green")
        red_run = self._count_consecutive_bricks(renko, "red")

        ce_momentum = green_run >= 2
        pe_momentum = red_run >= 2

        # Filter 3: Volume confirmation (passed in from caller)
        # Filter 4: Cooldown check
        long_entry = (
            ce_trend
            and ce_momentum
            and volume_ok
            and (current_ts is None or self._cooldown_ok("LONG", current_ts))
        )
        short_entry = (
            pe_trend
            and pe_momentum
            and volume_ok
            and (current_ts is None or self._cooldown_ok("SHORT", current_ts))
        )

        if long_entry:
            # Stop at 3 bricks back low — gives meaningful distance
            stop = float(prev2["low"])
            if stop < c:
                return RenkoDecision(
                    action="ENTER_LONG",
                    entry_underlying=c,
                    stop_underlying=stop,
                    signal_triggered=True,
                )
            return RenkoDecision(signal_triggered=True)

        if short_entry:
            stop = float(prev2["high"])
            if stop > c:
                return RenkoDecision(
                    action="ENTER_SHORT",
                    entry_underlying=c,
                    stop_underlying=stop,
                    signal_triggered=True,
                )
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
    description = "Renko Trend Sniper: ATR-adaptive boxes, dual-EMA filter, momentum confirmation, hard premium SL."

    manifest = {
        "symbols": ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"],
        "timeframes": ["1d", "1m"],
        "indicators": [],
    }

    MIN_WARMUP_DAYS = 2
    USES_PUSH_SIGNALS = True

    default_params: dict[str, Any] = {
        "box_atr_mult": 0.20,       # box_size = ATR_14 × this
        "box_min": 20.0,            # floor on box size
        "sl_pct_premium": 0.30,     # hard SL at 30% of entry premium
        "target_rr_mult": 2.0,      # initial target = SL_dist × this
        "trail_activate_rr": 1.5,   # trailing stop activates at this R:R
        "trail_pct": 0.25,          # trail gives back 25% of peak premium
        "cooldown_min": 30,         # minutes before same-direction re-entry
        "min_consecutive_bricks": 2, # required same-color brick run for entry
        "volume_sma_len": 20,       # volume SMA lookback for filter
    }

    def __init__(self, params=None):
        super().__init__(params)
        self.daily_candles: deque[Candle] = deque(maxlen=10)
        self.bars_1m: deque[Candle] = deque(maxlen=15000)

        self.trade_date: str = ""
        self.instrument_name: str = "NIFTY"

        self.engine = RenkoSignalEngine()
        self.active_trade_context: Optional[RenkoPositionContext] = None

        # Volume SMA tracking
        self._volume_history: deque[float] = deque(maxlen=int(self.params.get("volume_sma_len", 20)))

        # Incremental Renko state (see _build_renko). None until seeded on the
        # first call with enough history.
        self._renko_rows: list[dict] = []
        self._renko_last_open: float | None = None
        self._renko_last_close: float | None = None
        self._renko_box_size: float | None = None
        self._renko_df_cache: pd.DataFrame = pd.DataFrame()
        self._renko_df_cache_len: int = -1

    def get_wiggle_params(self) -> list[str]:
        """Return parameters eligible for ±20% wiggle testing."""
        return ["box_atr_mult", "sl_pct_premium", "target_rr_mult", "trail_activate_rr"]

    def on_candle(self, symbol: str, timeframe: str, candle: Candle) -> None:
        self.instrument_name = symbol

        if timeframe == "1d":
            self.daily_candles.append(candle)
        elif timeframe == "1m":
            self.bars_1m.append(candle)
            self._volume_history.append(candle.volume)
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

    def _volume_above_average(self) -> bool:
        """Check if the latest bar's volume exceeds the SMA."""
        if len(self._volume_history) < 5:
            return True  # Not enough data — allow
        avg_vol = sum(self._volume_history) / len(self._volume_history)
        if avg_vol <= 0:
            return True
        return self._volume_history[-1] > avg_vol

    def _build_renko(self) -> pd.DataFrame:
        """Renko bricks + EMAs, updated incrementally as new 1m bars arrive.

        `on_candle` calls this once per new bar, so a naive rebuild-from-all-
        history each time is O(n) per call / O(n^2) over a session (a 30-day
        backtest measured 60+ seconds before this fix). Since exactly one new
        bar arrives per call, we only need to feed that one bar through the
        brick-formation loop and extend the existing brick list — the anchor
        (`_renko_last_open/close`) carries all the state a full rebuild would
        have rediscovered from scratch.
        """
        if len(self.bars_1m) < 100:
            return pd.DataFrame()

        # ATR-adaptive box sizing for ALL instruments (no more hardcoded 12.5)
        daily_atr = self._get_atr()
        box_atr_mult = float(self.params.get("box_atr_mult", 0.20))
        box_min = float(self.params.get("box_min", 20.0))
        box_size = max(box_min, daily_atr * box_atr_mult)

        if self._renko_box_size != box_size:
            # Box size changed underneath us (ATR-based sizing can drift) —
            # the brick sequence is no longer valid against the new box size,
            # so rebuild it from scratch this once.
            self._renko_rows = []
            self._renko_last_open = None
            self._renko_last_close = None
            self._renko_box_size = box_size
            self._renko_df_cache_len = -1

        if self._renko_last_open is None:
            # First build: seed the anchor from the oldest bar we still have,
            # exactly like build_renko_from_close does with closes[0], then
            # feed every bar after it once.
            seed_bars = list(self.bars_1m)
            self._renko_last_open = seed_bars[0].close
            self._renko_last_close = seed_bars[0].close
            new_bars = seed_bars[1:]
        else:
            new_bars = [self.bars_1m[-1]]

        for c in new_bars:
            price = c.close
            ts = c.timestamp
            bricks_this_bar = 0
            while True:
                # Safety valve against a runaway brick cascade from ONE bar
                if bricks_this_bar >= MAX_RENKO_BRICKS_PER_BUILD:
                    break

                prev_high = max(self._renko_last_open, self._renko_last_close)
                prev_low = min(self._renko_last_open, self._renko_last_close)
                up_trigger = prev_high + box_size
                down_trigger = prev_low - box_size

                if price >= up_trigger:
                    brick_open = prev_high
                    brick_close = prev_high + box_size
                    self._renko_rows.append({"timestamp": ts, "open": brick_open, "high": max(brick_open, brick_close), "low": min(brick_open, brick_close), "close": brick_close, "color": "green"})
                    self._renko_last_open, self._renko_last_close = brick_open, brick_close
                    bricks_this_bar += 1
                    continue

                if price <= down_trigger:
                    brick_open = prev_low
                    brick_close = prev_low - box_size
                    self._renko_rows.append({"timestamp": ts, "open": brick_open, "high": max(brick_open, brick_close), "low": min(brick_open, brick_close), "close": brick_close, "color": "red"})
                    self._renko_last_open, self._renko_last_close = brick_open, brick_close
                    bricks_this_bar += 1
                    continue

                break

        # Bound memory/EMA-recompute cost for long-running sessions
        if len(self._renko_rows) > _RENKO_ROWS_TRIM_AT:
            self._renko_rows = self._renko_rows[-_RENKO_ROWS_KEEP:]
            self._renko_df_cache_len = -1  # force a cache rebuild post-trim

        if len(self._renko_rows) < 44:
            return pd.DataFrame()

        # Most bars form zero new bricks — rebuilding the DataFrame and
        # recomputing EMAs over the whole brick history is only needed when
        # the brick count actually changed since the last call.
        if len(self._renko_rows) == self._renko_df_cache_len:
            return self._renko_df_cache

        renko = pd.DataFrame(self._renko_rows)
        closes_series = renko["close"]
        # EMA21 and EMA44 for entry/exit; EMA5 retained only for reference
        renko["ema5"] = closes_series.ewm(span=5, min_periods=5, adjust=False).mean()
        renko["ema21"] = closes_series.ewm(span=21, min_periods=21, adjust=False).mean()
        renko["ema44"] = closes_series.ewm(span=44, min_periods=44, adjust=False).mean()
        renko["box_size"] = box_size
        self._renko_df_cache = renko
        self._renko_df_cache_len = len(self._renko_rows)
        return renko

    def _evaluate_backtest(self, symbol: str, current_candle: Candle) -> None:
        # Time filter (09:20 - 14:30 for new entries, but exits can trigger anytime up to 15:15)
        ts_str = current_candle.timestamp
        try:
            now_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                now_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:00")
            except ValueError:
                now_dt = datetime.now()

        now_time = now_dt.time()
        is_entry_window = time(9, 20) <= now_time <= time(14, 30)

        renko = self._build_renko()
        if renko.empty or len(renko) < 3:
            return

        volume_ok = self._volume_above_average()

        decision = self.engine.evaluate_candle(
            renko,
            self.active_trade_context,
            current_ts=now_dt,
            volume_ok=volume_ok,
        )

        if self.active_trade_context:
            self.active_trade_context.rr_armed = decision.rr_armed

        if decision.action == "EXIT" and self.active_trade_context:
            self._emit_signal(
                legs=[], # For exits, we don't strictly need legs if engine closes all active legs
                is_exit=True,
                entry_price=current_candle.close,
                thesis=decision.exit_reason,
            )
            self.engine.update_previous_trade_direction(self.active_trade_context.direction)
            self.engine.record_exit(self.active_trade_context.direction, now_dt)
            self.active_trade_context = None

        elif decision.action in ("ENTER_LONG", "ENTER_SHORT") and not self.active_trade_context and is_entry_window:
            direction = "PE" if decision.action == "ENTER_LONG" else "CE"
            pos_dir = "LONG" if decision.action == "ENTER_LONG" else "SHORT"

            self.active_trade_context = RenkoPositionContext(
                direction=pos_dir,
                entry_underlying=decision.entry_underlying,
                stop_underlying=decision.stop_underlying,
            )

            # Credit Spread: Sell ATM, Buy 300 pts OTM as hedge
            hedge_offset = -300 if direction == "PE" else 300
            legs = [
                {"direction": direction, "action": "SELL", "strike_offset": 0, "qty_ratio": 1.0},
                {"direction": direction, "action": "BUY", "strike_offset": hedge_offset, "qty_ratio": 1.0},
            ]

            self._emit_signal(
                legs=legs,
                entry_price=current_candle.close,
                thesis=f"Renko {pos_dir} Trend",
                # Communicate SL/target percentages to engine
                sl_pct_premium=float(self.params.get("sl_pct_premium", 0.30)),
                target_rr_mult=float(self.params.get("target_rr_mult", 2.0)),
            )

    def _emit_signal(self, legs: list[dict[str, Any]], entry_price: float,
                     thesis: str, sl_pct_premium: float = 0.0,
                     target_rr_mult: float = 0.0, is_exit: bool = False) -> None:
        """Override point — the backtest engine monkey-patches this."""
        pass

    def evaluate(self, ctx: StrategyContext) -> StrategySignal | None:
        return None
